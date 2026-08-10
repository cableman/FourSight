"""Words → ``Command``, with modal groups resolved.

Four things this module owns, each of which is a silent-wrongness risk if got wrong:

1. **G-codes canonicalize to strings.** ``G01``, ``G1`` and ``G1.0`` all become ``'1'``, while
   ``G90.1`` stays ``'90.1'``. Never floats — ``words['G'] == 90.1`` float comparison is exactly
   the bug the string form exists to prevent.
2. **Modal carry-over.** A block of bare axis words inherits the active motion mode, so
   ``X10 Y10`` after a ``G1`` is a feed move — and after a ``G81`` is a drill cycle, which is why
   ``motion`` must be resolved here rather than guessed downstream.
3. **G20 inch → mm at parse time**, using ``LINEAR_LENGTH_LETTERS``. Rotary words are in degrees
   and must never be scaled; ``F`` scales only under G94/G95, because under G93 it is 1/minutes.
4. **``ModalState`` is copy-on-write.** A new instance is emitted only when a field actually
   changes, so consecutive commands share one object and the parse stays inside its time budget.

Errors are reported, never raised (see ``ParseError``); severity is attached later by the verifier.
"""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from functools import lru_cache

from foursight.parser.dialect import LINUXCNC, Dialect
from foursight.parser.model import (
    COORD_TRANSFORM_MODES,
    LINEAR_LENGTH_LETTERS,
    Command,
    ModalState,
    ParseError,
    ParseErrorKind,
    SourceRef,
    TokenizedLine,
    Word,
)
from foursight.parser.tokenizer import tokenize

INCH_TO_MM = 25.4

# Modal groups, LinuxCNC numbering, restricted to what v1 recognizes. Two codes from one group in
# a single block is an error: the machine cannot be in both states at once.
#
# Group 1 includes the canned cycles. They are not *interpreted* in v1, but they must be resolved
# as motion modes, because that is what makes a following bare `X10 Y10` a drill cycle rather than
# a straight line — the single most likely way to draw a confidently wrong toolpath.
_GROUPS: dict[str, tuple[str, ...]] = {
    "motion": (
        "0", "1", "2", "3", "33", "38.2", "38.3", "38.4", "38.5", "73", "76",
        "80", "81", "82", "83", "84", "85", "86", "87", "88", "89",
    ),
    "plane": ("17", "18", "19"),
    "distance": ("90", "91"),
    "arc_distance": ("90.1", "91.1"),
    "feed_mode": ("93", "94", "95"),
    "units": ("20", "21"),
    "cutter_comp": ("40", "41", "42"),
    "tool_length": ("43", "44", "49"),
    "coord_system": ("54", "55", "56", "57", "58", "59", "59.1", "59.2", "59.3"),
    "return_mode": ("98", "99"),
    "control_mode": ("61", "61.1", "64"),
    # Refused coordinate transforms (COORD_TRANSFORM_MODES). Written out literally rather than
    # spliced from the table, to keep this a readable literal — the correspondence between the group
    # name, the ModalState field and `mode.field` is pinned by a test instead.
    #
    # They belong in modal groups for two reasons beyond routing: Fanuc genuinely puts each pair in
    # one group, so `G68 G69` in a block is a conflict the machine cannot honour; and being here
    # does NOT disturb `_resolve_motion`, which reacts only to group == "motion", so `G68 G1 X10`
    # correctly keeps G1 as the carried motion mode.
    "rotation": ("68", "69"),
    "scaling": ("50", "51"),
    "polar": ("15", "16"),
}  # fmt: skip

# group name → the cancel code for that group, for the transform groups only.
_TRANSFORM_CANCEL_OF: dict[str, str] = {mode.field: mode.cancel for mode in COORD_TRANSFORM_MODES}

# M-code groups. LinuxCNC puts M7/M8/M9 in one group, so `M7 M8` is a conflict even though mist
# plus flood is physically meaningful; the normative dialect wins (PLAN.md § Reference dialect).
_M_GROUPS: dict[str, tuple[str, ...]] = {
    "stopping": ("0", "1", "2", "30", "60"),
    "toolchange": ("6",),  # trailing comma matters: ("6") is the string, not a tuple
    "spindle": ("3", "4", "5"),
    "coolant": ("7", "8", "9"),
}

_GROUP_OF: dict[str, str] = {code: name for name, codes in _GROUPS.items() for code in codes}
_M_GROUP_OF: dict[str, str] = {code: name for name, codes in _M_GROUPS.items() for code in codes}

# Non-modal codes (group 0 — G4, G10, G28, G30, G53, G92) need no table here: they are simply
# absent from _GROUPS, so they never match a modal group and never disturb the carried motion mode.
# T2.2 gives them behaviour when the simulator needs it.

# G80 cancels motion mode outright rather than selecting one.
_MOTION_CANCEL = "80"

#: The normative units mapping. A dialect may add spellings (Mach3's G70/G71) but never remove one.
_UNITS_OF: dict[str, str] = {"20": "inch", "21": "mm"}


@dataclass(slots=True, frozen=True)
class _ModalTables:
    """The modal lookups for one dialect, resolved once per parse rather than per block.

    A dialect can only *add* codes, never remove one: G20/G21 stay valid under Mach3 alongside
    G70/G71, because a post is free to emit either and files routinely contain both.
    """

    group_of: dict[str, str]
    units_of: dict[str, str]
    cycle_cancel_conflicts: bool


@lru_cache(maxsize=8)
def _tables_for(dialect: Dialect) -> _ModalTables:
    """Cached on the dialect, which is frozen and hashable, and of which there are two."""
    group_of = dict(_GROUP_OF)
    units_of = dict(_UNITS_OF)
    for code, unit in dialect.unit_aliases:
        group_of[code] = "units"
        units_of[code] = unit
    return _ModalTables(
        group_of=group_of,
        units_of=units_of,
        cycle_cancel_conflicts=dialect.cycle_cancel_conflicts,
    )


@dataclass(slots=True)
class ParseResult:
    """Everything a whole-file parse produced.

    Errors from tokenizing and from modal resolution land in one list, in source order, so the CLI
    and the verifier have a single place to look.
    """

    commands: list[Command]
    errors: list[ParseError]


def canonical_code(value: float) -> str:
    """`1.0` → `'1'`, `90.1` → `'90.1'`, `01` → `'1'`.

    Canonicalizing from the numeric value rather than the source text handles leading zeros for
    free. Formatting is fixed-precision then trimmed, not ``%g``, which would switch to exponent
    notation on large values.
    """
    if value == int(value):
        return str(int(value))
    return f"{value:.4f}".rstrip("0")


def parse(text: str, *, block_delete: bool = False, dialect: Dialect = LINUXCNC) -> ParseResult:
    """Tokenize and resolve a whole document. The convenience entry point for the CLI."""
    return resolve(tokenize(text), block_delete=block_delete, dialect=dialect)


def resolve(
    lines: Iterable[TokenizedLine], *, block_delete: bool = False, dialect: Dialect = LINUXCNC
) -> ParseResult:
    """Resolve tokenized lines into commands.

    ``block_delete`` mirrors the control-panel switch and defaults to **off**, meaning deleted
    blocks *execute* — the common default, and the verifier must run against the active mode
    (PLAN.md § Dialect Divergences).

    ``dialect`` supplies the starting conditions the program cannot state for itself, and defaults
    to LinuxCNC so the parser is usable with no configuration at all. In v1 that is one field:
    ``ModalState.arc_distance``, which Mach3 makes a controller setting rather than a G-code. It is
    injected here, at the one place a ``ModalState`` is ever constructed, so every consumer keeps
    reading the *resolved* snapshot and needs no notion of a dialect.
    """
    result = ParseResult(commands=[], errors=[])
    tables = _tables_for(dialect)
    state = ModalState(arc_distance=dialect.arc_distance)
    motion: str | None = None

    for line in lines:
        if line.errors:
            result.errors.extend(line.errors)
        if block_delete and line.block_delete:
            continue
        if not line.words:
            continue
        state, motion = _resolve_line(line, state, motion, result, tables)
    return result


def _resolve_line(
    line: TokenizedLine,
    state: ModalState,
    motion: str | None,
    result: ParseResult,
    tables: _ModalTables,
) -> tuple[ModalState, str | None]:
    """Resolve one line, returning the modal state and motion mode to carry forward."""
    gcodes, mcodes, words = _split_words(line, result)
    _check_group_conflicts(line.ref, gcodes, tables.group_of, "G", result, tables=tables)
    _check_group_conflicts(line.ref, mcodes, _M_GROUP_OF, "M", result)

    changes = _modal_changes(gcodes, mcodes, words, state, tables)
    units = changes.get("units", state.units)
    feed_mode = changes.get("feed_mode", state.feed_mode)
    words = _to_internal_units(words, units, feed_mode)
    # F is applied here rather than in _modal_changes because it must be unit-converted first, but
    # it still has to pass the same "only if it differs" test. CAM output repeats F on every line,
    # so treating a restated feed as a change would allocate a fresh ModalState per line and defeat
    # copy-on-write on the most common input there is.
    if "F" in words and words["F"] != state.feed:
        changes["feed"] = words["F"]

    # Copy-on-write: only allocate when something actually changed, so runs of identical blocks
    # share one frozen instance.
    new_state = replace(state, **changes) if changes else state
    new_motion = _resolve_motion(gcodes, tables, motion)

    result.commands.append(
        Command(
            ref=line.ref,
            gcodes=gcodes,
            mcodes=mcodes,
            motion=new_motion,
            words=words,
            modal_snapshot=new_state,
        )
    )
    return new_state, new_motion


def _split_words(line: TokenizedLine, result: ParseResult) -> tuple[list[str], list[str], dict]:
    """Separate G and M words (canonical strings) from address words (letter → value)."""
    gcodes: list[str] = []
    mcodes: list[str] = []
    words: dict[str, float] = {}
    for word in line.words:
        if word.letter == "G":
            gcodes.append(canonical_code(word.value))
        elif word.letter == "M":
            mcodes.append(canonical_code(word.value))
        elif word.letter in words:
            # A dict would silently keep the last one, losing the conflict entirely.
            _error(result, line.ref, word, f"'{word.letter}' appears more than once in this block")
        else:
            words[word.letter] = word.value
    return gcodes, mcodes, words


def _check_group_conflicts(
    ref: SourceRef,
    codes: list[str],
    table: dict[str, str],
    letter: str,
    result: ParseResult,
    *,
    tables: _ModalTables | None = None,
) -> None:
    """Two codes from one modal group in one block cannot both take effect.

    One dialect-gated exemption: **G80 alongside a motion code**. LinuxCNC puts the canned-cycle
    cancel in modal group 1 with G0/G1/G2/G3, so `G0 G80` is genuinely an error there. Mach3 accepts
    it, and `G00 G21 G17 G90 G40 G49 G80` is the safe-start line nearly every post emits — it is
    unambiguous, because G80 cancels any cycle while G0 selects the mode. Reporting it as an *error*
    made `foursight check` exit 1 on ordinary, correct Mach3 output.

    Only that pair is exempt. `G1 G2` in one block stays an error under every dialect, because there
    the machine really cannot tell which one you meant.
    """
    lenient_cancel = tables is not None and not tables.cycle_cancel_conflicts
    seen: dict[str, str] = {}
    for code in codes:
        group = table.get(code)
        if group is None:
            continue
        if lenient_cancel and group == "motion" and _MOTION_CANCEL in (code, seen.get(group)):
            # Keep the real motion code in `seen`, so a third group-1 code still conflicts.
            if code != _MOTION_CANCEL:
                seen[group] = code
            continue
        if group in seen:
            result.errors.append(
                ParseError(
                    line=ref.line_no,
                    offset=ref.start,
                    text=f"{letter}{seen[group]} {letter}{code}",
                    message=(
                        f"{letter}{seen[group]} and {letter}{code} are both in modal group "
                        f"'{group}'; only one can be active"
                    ),
                    kind=ParseErrorKind.MODAL_GROUP_CONFLICT,
                )
            )
            continue
        seen[group] = code


def _modal_changes(
    gcodes: list[str],
    mcodes: list[str],
    words: dict[str, float],
    state: ModalState,
    tables: _ModalTables,
) -> dict[str, object]:
    """Collect only the fields whose value actually differs, so copy-on-write stays meaningful."""
    pending: dict[str, object] = {}
    for code in gcodes:
        group = tables.group_of.get(code)
        if group == "units":
            # Looked up rather than tested against "20": under Mach3, G70 is inch and G71 is mm, so
            # `"inch" if code == "20" else "mm"` would silently read every G70 as millimetres.
            pending["units"] = tables.units_of[code]
        elif group in {"plane", "distance", "arc_distance", "feed_mode"}:
            pending[group] = code
        elif group == "coord_system":
            pending["offset"] = code
        elif group == "cutter_comp":
            pending["cutter_comp"] = None if code == "40" else code
        elif group in _TRANSFORM_CANCEL_OF:
            # The group name IS the ModalState field name; see CoordTransformMode.field.
            pending[group] = None if code == _TRANSFORM_CANCEL_OF[group] else code
        elif group == "tool_length":
            if code == "49":
                pending["length_offset"] = None
            elif "H" in words:
                pending["length_offset"] = int(words["H"])
            # G43/G44 with no H word keeps whatever offset is active: LinuxCNC falls back to the
            # current tool's offset, so clearing it here would silently drop a real Z offset.
    for code in mcodes:
        if code in {"3", "4"}:
            pending["spindle_on"] = code
        elif code == "5":
            pending["spindle_on"] = None
    if "S" in words:
        pending["spindle_rpm"] = words["S"]
    if "T" in words:
        pending["tool"] = int(words["T"])
    return {key: value for key, value in pending.items() if getattr(state, key) != value}


def _to_internal_units(words: dict[str, float], units: str, feed_mode: str) -> dict[str, float]:
    """Convert an inch program's lengths to mm. Internal geometry is always mm.

    Rotary words are degrees and are never touched. ``F`` is a length rate under G94 (units/min)
    and G95 (units/rev), but under G93 it is inverse time in 1/minutes — scaling that would corrupt
    every feed on the block.
    """
    if units != "inch":
        return words
    converted = dict(words)
    for letter in words:
        if letter in LINEAR_LENGTH_LETTERS:
            converted[letter] = words[letter] * INCH_TO_MM
    if "F" in converted and feed_mode != "93":
        converted["F"] = words["F"] * INCH_TO_MM
    return converted


def _resolve_motion(gcodes: list[str], tables: _ModalTables, motion: str | None) -> str | None:
    """The motion mode in force for this block, carried forward when the block states none.

    A real motion code **wins over G80 regardless of word order**. Scanning in order and returning
    on the first of the two made `G0 G80` and `G80 G0` mean different things — the safe-start line
    resolved to G0 only because the post happened to write G0 first. Order-dependence in a modal
    resolution is the kind of bug that shows up as one wrong rapid in someone else's posted output.
    """
    cancelled = False
    for code in gcodes:
        if code == _MOTION_CANCEL:
            cancelled = True
        elif tables.group_of.get(code) == "motion":
            return code
    if cancelled:
        return None
    # Non-modal codes (G53, G4, G28, ...) and unrecognized ones simply fall through, leaving the
    # carried mode intact: `G53 G0 X0` keeps G0 active, and a mid-contour dwell does not cancel G1.
    return motion


def _error(result: ParseResult, ref: SourceRef, word: Word, message: str) -> None:
    result.errors.append(
        ParseError(
            line=ref.line_no,
            offset=ref.start,
            text=word.letter,
            message=message,
            kind=ParseErrorKind.DUPLICATE_WORD,
        )
    )
