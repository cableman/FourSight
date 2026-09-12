"""Structural checks (T1.7): syntax, modal conflicts, unknown codes, unsupported motion.

This module is where PLAN.md's governing principle is enforced. The taxonomy line it draws is not
cosmetic:

- An unrecognized code that **never touches position** is a ``warning``; the path still renders.
- A recognized code that **changes how motion is interpreted** but is not implemented in v1 is
  ``unsupported``. Under an active G81 a block of bare ``X10 Y10`` is a drill cycle, and drawing a
  straight line through the hole positions is exactly the confidently-wrong output we refuse.

Spans, not blocks. A canned cycle or a compensation region gets **one** diagnostic covering its
extent, because twenty holes should not produce twenty identical complaints. The span boundaries
come from the resolver's already-computed `Command.motion` and `ModalState.cutter_comp` rather than
being re-derived here.
"""

from collections.abc import Iterable, Iterator, Sequence

from foursight.parser.model import (
    CANNED_CYCLE_CODES,
    COORD_TRANSFORM_CODES,
    COORD_TRANSFORM_MODES,
    DATUM_SHIFT_ACTIVATES,
    DATUM_SHIFT_CANCELS,
    DATUM_SHIFT_CONSEQUENCE,
    PROBE_CODES,
    PROBE_CONSEQUENCE,
    SPINDLE_SYNC_CODES,
    SPINDLE_SYNC_CONSEQUENCE,
    Command,
    CoordTransformMode,
    ParseErrorKind,
)
from foursight.verify.report import Diagnostic, Severity
from foursight.verify.rules import Program, Rule, register_rule

# Codes v1 interprets. Anything here produces no diagnostic of its own.
INTERPRETED_GCODES = frozenset({
    "0", "1", "2", "3", "4",
    "15",                                    # polar mode CANCEL; see the transform note below
    "17", "18", "19",
    "20", "21",
    "28", "28.1", "30", "30.1",
    "40",                                    # cutter comp CANCEL is interpreted; 41/42 are not
    "43", "44", "49",
    "50",                                    # scaling CANCEL; see the G50 note below
    "53",
    "54", "55", "56", "57", "58", "59", "59.1", "59.2", "59.3",
    "61", "61.1", "64",                      # path-control modes: see note below
    "69",                                    # rotation CANCEL
    "80",                                    # canned-cycle CANCEL
    "90", "91", "90.1", "91.1",
    "93", "94", "95",
    "92.1", "92.2",                          # datum-shift clear and suspend; both end the span
    "98", "99",                              # canned-cycle return mode; only meaningful with 8x
})  # fmt: skip

# G15/G50/G69 join G40 and G80 as cancels: a cancel ends a refused span rather than being a refused
# construct, so it is interpreted and silent.
#
# **G50 is ambiguous** and is read here as the scaling cancel. On a lathe it means maximum spindle
# speed, or a position-register set. FourSight is a 4-axis *mill* previewer, where the reading is
# unambiguous. The asymmetry settles it: if the guess is right we correctly close a scaling span; if
# a lathe program ever appears, the cost is *silence* — we lose a warning and never say anything
# false. Giving G50 a diagnostic of its own would invert that and print a confidently wrong message.
# Heuristics on a preceding G51 or a trailing S word are deliberately not attempted: a mode that is
# right most of the time and inexplicable the rest is worse than one plain rule.

# G61/G61.1/G64 are accepted silently rather than warned about. They select exact-stop versus
# blending, which changes how the machine *corners* but not the programmed centreline we render, so
# there is nothing about the toolpath to warn on — and G64 appears in nearly every LinuxCNC program,
# so warning would be pure noise. G98/G99 are likewise inert without a canned cycle, and a canned
# cycle already reports itself.

INTERPRETED_MCODES = frozenset({"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "30"})

# Recognized, motion-affecting, and NOT interpreted in v1 → `unsupported`, never `warning`.
# Borrowed from the parse layer so `sim` and `verify` cannot disagree about what a cycle is.
CANNED_CYCLES = CANNED_CYCLE_CODES
CUTTER_COMP = frozenset({"41", "42"})
# Per-occurrence rather than span-based: G10 writes a table entry at one point in the program.
#
# Its *consequence* still spans — after it, `[offsets]` describes a table the program has changed —
# but that is carried as `offset_known=False` from `machine_value` rather than as a second
# diagnostic, so a travel violation downstream is a warning it can stand behind instead of an error
# it cannot. See `machine/state.py::offsets_rewritten_from`.
UNSUPPORTED_ONE_SHOT: dict[str, str] = {
    "10": (
        "G10 writes the offset or tool table, which v1 does not model; the profile's [offsets] are "
        "treated as unknown from here on, so machine-coordinate checks after this line are "
        "reported as warnings rather than errors"
    ),
}

# The M-code counterpart of UNSUPPORTED_ONE_SHOT, and `UnknownCodes`'s M branch **must** consult it.
# Without the exclusion, M98 yields both a warning and an unsupported diagnostic — and the warning's
# text ("assumed inert and the path is drawn as if it were absent") would be a flat lie about a code
# that costs us the machine position entirely.
UNSUPPORTED_MCODES: dict[str, str] = {
    "98": (
        "M98 subprogram call is not expanded in v1: the called blocks are not simulated, and the "
        "machine position is treated as lost from here on, so the following moves are not drawn "
        "until X, Y and Z are all restated in absolute coordinates"
    ),
    "99": (
        "M99 subprogram return is not interpreted in v1: execution does not continue to the next "
        "line, so the machine position is treated as lost from here on"
    ),
}

# ParseError kinds that are genuine malformed input, as opposed to a recognized-but-unsupported
# construct. Split out so the mapping lives in one place rather than in message-text matching.
_SYNTAX_KINDS = frozenset({
    ParseErrorKind.MALFORMED_WORD,
    ParseErrorKind.UNTERMINATED_COMMENT,
    ParseErrorKind.DUPLICATE_WORD,
})  # fmt: skip


@register_rule
class SyntaxErrors(Rule):
    """Malformed words, stray characters, unterminated comments, repeated addresses."""

    rule_id = "structural.syntax-error"
    description = "Malformed word or unparseable text"
    severity = Severity.ERROR

    def check(self, program: Program) -> Iterable[Diagnostic]:
        for error in program.parse_errors:
            if error.kind in _SYNTAX_KINDS:
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.ERROR,
                    line=error.line,
                    message=error.message,
                    offset=error.offset,
                )


@register_rule
class ModalGroupConflicts(Rule):
    """Two codes from one modal group in a single block: the machine cannot be in both states."""

    rule_id = "structural.modal-group-conflict"
    description = "Two codes from the same modal group in one block"
    severity = Severity.ERROR

    def check(self, program: Program) -> Iterable[Diagnostic]:
        for error in program.parse_errors:
            if error.kind is ParseErrorKind.MODAL_GROUP_CONFLICT:
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.ERROR,
                    line=error.line,
                    message=error.message,
                    offset=error.offset,
                )


@register_rule
class UnsupportedOwords(Rule):
    """LinuxCNC O-word flow control.

    ``unsupported`` rather than ``error``: the construct is well-formed, and it decides *which*
    motion executes — so the path we would draw is not the path that runs.
    """

    rule_id = "structural.unsupported-oword"
    description = "O-word flow control is not interpreted in v1"
    severity = Severity.UNSUPPORTED

    def check(self, program: Program) -> Iterable[Diagnostic]:
        for error in program.parse_errors:
            if error.kind is ParseErrorKind.UNSUPPORTED_OWORD:
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.UNSUPPORTED,
                    line=error.line,
                    message=error.message,
                    offset=error.offset,
                )


@register_rule
class UnknownCodes(Rule):
    """Codes v1 neither interprets nor recognizes as motion-affecting.

    A ``warning``, and the path still renders: per PLAN.md's taxonomy an unrecognized code that
    never touches position stays a warning. The motion-affecting cases are enumerated in the rules
    below rather than guessed at here.
    """

    rule_id = "structural.unknown-code"
    description = "Unknown or unsupported inert G/M code"
    severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        # Report each distinct code once per line, but a code repeated across a 100k-line file
        # should not yield 100k warnings either — so report the first occurrence of each code.
        unsupported = _all_unsupported()  # built once, not once per G-word in the file
        # Codes the selected dialect interprets and the normative subset does not — Mach3's G70/G71.
        # Read from the profile, so a program checked under LinuxCNC still hears about them.
        interpreted = INTERPRETED_GCODES | program.profile.parser_dialect.extra_gcodes
        seen: set[str] = set()
        for command in program.commands:
            for code in command.gcodes:
                if code in interpreted or code in unsupported:
                    continue
                if f"G{code}" not in seen:
                    seen.add(f"G{code}")
                    yield self._unknown(command, "G", code)
            for code in command.mcodes:
                if code in INTERPRETED_MCODES or code in UNSUPPORTED_MCODES:
                    continue
                if f"M{code}" not in seen:
                    seen.add(f"M{code}")
                    yield self._unknown(command, "M", code)

    def _unknown(self, command: Command, letter: str, code: str) -> Diagnostic:
        return Diagnostic(
            rule_id=self.rule_id,
            severity=Severity.WARNING,
            line=command.ref.line_no,
            message=(
                f"{letter}{code} is not recognized; it is assumed inert and the path is drawn "
                f"as if it were absent"
            ),
            offset=command.ref.start,
        )


#: G43/G44 activate a tool length offset; G49 cancels it and needs no diagnostic.
_TOOL_LENGTH_CODES = frozenset({"43", "44"})


@register_rule
class ToolLengthNotModelled(Rule):
    """G43/G44 is active, so drawn Z is the spindle position rather than the tool tip.

    **Owed since M1 and recorded in PLAN.md until now.** The simulator has always emitted a *note* about
    this, but a note only ever reached the status bar — so once M3 built the diagnostics panel, the one
    modelling caveat that changes what Z *means* was the only one absent from it.

    `unsupported`, not `warning`, and the distinction is the taxonomy's whole point: G43 affects how
    subsequent motion is interpreted and v1 does not interpret it. PLAN.md § Supported G-code Subset makes
    the deliberate trade — G43 appears in nearly every real program, so refusing to draw them all would
    make the previewer useless, and the offset shifts the Z datum uniformly without changing the path's
    *shape*. So the path is drawn and this says what it is: Z relative to the spindle, not the tip.

    One diagnostic per activation, not per affected line. A program that cuts 40,000 lines under one G43
    has one thing wrong with it, not 40,000.
    """

    rule_id = "structural.tool-length-not-modelled"
    description = "G43/G44 tool length offset is not modelled; Z is relative to the spindle"
    severity = Severity.UNSUPPORTED

    def check(self, program: Program) -> Iterable[Diagnostic]:
        for command in program.commands:
            active = [code for code in command.gcodes if code in _TOOL_LENGTH_CODES]
            if not active:
                continue
            offset = command.words.get("H")
            names = "/".join(f"G{code}" for code in active)
            suffix = "" if offset is None else f" (H{offset:g})"
            yield Diagnostic(
                rule_id=self.rule_id,
                severity=Severity.UNSUPPORTED,
                line=command.ref.line_no,
                message=(
                    f"{names}{suffix} tool length offset is not modelled: there is no tool table, so "
                    "the drawn Z is the spindle position, not the tool tip. The path's shape is correct; "
                    "its Z datum is shifted by the offset."
                ),
            )


@register_rule
class UnsupportedMotionCodes(Rule):
    """Recognized, motion-affecting, not interpreted in v1 — reported per span.

    The two span cases are the ones PLAN.md singles out as the most likely source of a wrong
    picture:

    - **Canned cycles (G73/G76/G81–G89).** Under an active cycle, a block carrying only axis words
      is a full drill cycle, not a linear move. The span runs from the cycle code to G80 or the end
      of the program.
    - **Cutter compensation (G41/G42).** While comp is active the real path is offset by the tool
      radius; v1 draws the programmed centreline and says so. The span runs to G40 or program end.
    - **Coordinate transforms (G68 rotation, G51 scaling, G16 polar).** Fanuc/Mach3 constructs that
      change the programmed → machine mapping. Unlike comp, the error is unbounded — a rotation
      about a fixture origin displaces the whole path, a negative scale factor mirrors it, and under
      G16 the axis words are a radius and an angle — so these spans are suppressed, not drawn.

    ``rule_id`` is shared with the one-shot codes and the subprogram calls on purpose. All of them
    say the same thing — the picture is not what the program does — and splitting them across ids
    would fragment that for the panel and for any future suppression mechanism keyed on the id.
    """

    rule_id = "structural.unsupported-motion"
    description = "Motion-affecting code not interpreted in v1"
    severity = Severity.UNSUPPORTED

    def check(self, program: Program) -> Iterable[Diagnostic]:
        commands = program.commands
        yield from self._cycle_spans(commands)
        yield from self._transform_spans(commands)
        yield from self._comp_spans(commands)
        yield from self._one_shots(commands)
        yield from self._datum_spans(commands)
        yield from self._motion_mode_spans(commands)

    def _cycle_spans(self, commands: Sequence[Command]) -> Iterator[Diagnostic]:
        for start, end in _spans(commands, lambda c: (c.motion or "") in CANNED_CYCLES):
            cycle = commands[start].motion
            closed = end + 1 < len(commands) and "80" in commands[end + 1].gcodes
            yield Diagnostic(
                rule_id=self.rule_id,
                severity=Severity.UNSUPPORTED,
                line=commands[start].ref.line_no,
                message=(
                    f"G{cycle} canned cycle active from line {commands[start].ref.line_no} to "
                    f"{commands[end].ref.line_no}"
                    f"{'' if closed else ' (never cancelled by G80)'}"
                    "; motion in this span is not interpreted and is not drawn"
                ),
                offset=commands[start].ref.start,
            )

    def _transform_spans(self, commands: Sequence[Command]) -> Iterator[Diagnostic]:
        """G68/G51/G16 spans, from the same table `sim` uses to decide not to draw them.

        One pass per mode rather than one combined span: two transforms active at once are two
        things wrong with the program, and collapsing them would leave the user guessing which.
        """
        for mode in COORD_TRANSFORM_MODES:
            for start, end in _spans(commands, _transform_active(mode)):
                first = commands[start].ref.line_no
                last = commands[end].ref.line_no
                closed = end + 1 < len(commands) and mode.cancel in commands[end + 1].gcodes
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.UNSUPPORTED,
                    line=first,
                    message=(
                        f"G{mode.activate} {mode.name} active from line {first} to {last}"
                        f"{'' if closed else f' (never cancelled by G{mode.cancel})'}"
                        f"; {mode.consequence}"
                    ),
                    offset=commands[start].ref.start,
                )

    def _datum_spans(self, commands: Sequence[Command]) -> Iterator[Diagnostic]:
        """G92 datum shifts, from the field `sim` reads to decide not to draw them.

        A span rather than one diagnostic per G92, because the consequence is what spans: every block
        until G92.1/G92.2 states its coordinates against a datum that is not in the file.
        """
        for start, end in _spans(commands, lambda c: c.modal_snapshot.datum_shift is not None):
            first = commands[start].ref.line_no
            last = commands[end].ref.line_no
            code = commands[start].modal_snapshot.datum_shift
            closed = end + 1 < len(commands) and bool(
                DATUM_SHIFT_CANCELS & set(commands[end + 1].gcodes)
            )
            yield Diagnostic(
                rule_id=self.rule_id,
                severity=Severity.UNSUPPORTED,
                line=first,
                message=(
                    f"G{code} datum shift active from line {first} to {last}"
                    f"{'' if closed else ' (never cancelled by G92.1/G92.2)'}"
                    f"; {DATUM_SHIFT_CONSEQUENCE}"
                ),
                offset=commands[start].ref.start,
            )

    def _motion_mode_spans(self, commands: Sequence[Command]) -> Iterator[Diagnostic]:
        """Probing and spindle-synchronized motion: modal, so reported per span like a cycle.

        Both are motion modes, so the bare blocks after one are further probes or further threading
        passes — the canned-cycle problem, and the reason neither can be a per-occurrence code.
        """
        for codes, consequence in (
            (PROBE_CODES, PROBE_CONSEQUENCE),
            (SPINDLE_SYNC_CODES, SPINDLE_SYNC_CONSEQUENCE),
        ):
            for start, end in _spans(commands, lambda c, s=codes: (c.motion or "") in s):
                first = commands[start].ref.line_no
                last = commands[end].ref.line_no
                extent = f"line {first}" if first == last else f"lines {first} to {last}"
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.UNSUPPORTED,
                    line=first,
                    message=f"G{commands[start].motion} active at {extent}; {consequence}",
                    offset=commands[start].ref.start,
                )

    def _comp_spans(self, commands: Sequence[Command]) -> Iterator[Diagnostic]:
        active = _spans(commands, lambda c: c.modal_snapshot.cutter_comp is not None)
        for start, end in active:
            code = commands[start].modal_snapshot.cutter_comp
            yield Diagnostic(
                rule_id=self.rule_id,
                severity=Severity.UNSUPPORTED,
                line=commands[start].ref.line_no,
                message=(
                    f"G{code} cutter compensation active from line {commands[start].ref.line_no} "
                    f"to {commands[end].ref.line_no}; the displayed path is the programmed "
                    f"centerline, not the compensated path"
                ),
                offset=commands[start].ref.start,
            )

    def _one_shots(self, commands: Sequence[Command]) -> Iterator[Diagnostic]:
        """Per-occurrence codes: one-shot, or a point event whose *consequence* is what spans.

        M98 is the second kind. Each call is a distinct place the picture breaks, so each is
        reported; the lost-position span that follows is `sim`'s to report, not this rule's.
        """
        for command in commands:
            yield from self._from_table(command, command.gcodes, UNSUPPORTED_ONE_SHOT)
            yield from self._from_table(command, command.mcodes, UNSUPPORTED_MCODES)

    def _from_table(
        self, command: Command, codes: Sequence[str], table: dict[str, str]
    ) -> Iterator[Diagnostic]:
        for code in codes:
            reason = table.get(code)
            if reason is not None:
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.UNSUPPORTED,
                    line=command.ref.line_no,
                    message=reason,
                    offset=command.ref.start,
                )


def _transform_active(mode: CoordTransformMode):
    """A predicate for one transform mode.

    A closure rather than a lambda written inline in the loop: `lambda c: getattr(c.modal_snapshot,
    mode.field)` is late-bound over `mode`, so every span would be reported under the last mode.
    Ruff's B023 catches it, but it is worth not writing.
    """
    field = mode.field
    return lambda command: getattr(command.modal_snapshot, field) is not None


def _all_unsupported() -> frozenset[str]:
    """G-codes handled by `UnsupportedMotionCodes`, so `UnknownCodes` stays quiet about them.

    **Every table that rule owns must appear here.** One that does not produces both a warning and
    an unsupported diagnostic for the same code, and the warning's text contradicts the other.
    """
    return (
        CANNED_CYCLES
        | CUTTER_COMP
        | COORD_TRANSFORM_CODES
        | DATUM_SHIFT_ACTIVATES
        | PROBE_CODES
        | SPINDLE_SYNC_CODES
        | frozenset(UNSUPPORTED_ONE_SHOT)
    )


def _spans(commands: Sequence[Command], active) -> list[tuple[int, int]]:
    """Maximal runs of consecutive commands satisfying `active`, as inclusive index pairs.

    One diagnostic per run rather than per block: a 20-hole drilling cycle is one thing the user
    needs to know about, not twenty.
    """
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, command in enumerate(commands):
        if active(command):
            if start is None:
                start = index
        elif start is not None:
            spans.append((start, index - 1))
            start = None
    if start is not None:
        spans.append((start, len(commands) - 1))
    return spans
