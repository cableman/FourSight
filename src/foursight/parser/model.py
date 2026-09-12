"""Parse-layer dataclasses: ``SourceRef``, ``ModalState``, ``Command``.

All three use ``slots=True``; ``SourceRef`` and ``ModalState`` are additionally frozen so they can
be shared rather than copied. ``SourceRef`` holds offsets, not text — a per-line copy of ``raw``
duplicates the whole file and costs the parse-rate target.

Must not import from ``foursight.machine`` (PLAN.md § Conventions). This module is the bottom of
the dependency order and imports nothing from the rest of the package.
"""

from dataclasses import dataclass
from enum import StrEnum

# PLAN.md § Supported G-code Subset: "Words: X Y Z A I J K R F S T P H D L Q".
# G and M are deliberately absent: a block carries several of each, so they live in
# Command.gcodes / Command.mcodes as strings, never in `words`. N is consumed by the tokenizer.
WORD_LETTERS = frozenset("XYZAIJKRFSTPHDLQ")

# Letters that move the machine. Needed to tell a real motion block from a parameter-only one —
# under an active G81, a block of just `X10 Y10` is a drill cycle, not a linear move.
AXIS_LETTERS = frozenset("XYZA")

# Letters carrying a *length*, and therefore scaled by 25.4 when converting G20 (inch) input to
# internal mm at parse time.
LINEAR_LENGTH_LETTERS = frozenset("XYZIJKR")

# Letters carrying an *angle*, in degrees. Never scaled by a unit conversion, and never mixed into
# a vector norm with the linear letters above (PLAN.md § Segment store).
ROTARY_LETTERS = frozenset("A")

# F is deliberately in neither set. It is a length rate under G94/G95 and so does scale, but under
# G93 (inverse time) it is 1/minutes and must not — the distinction is feed-mode dependent and
# belongs to the resolver, not to a static table here.

# Canned cycles (G73/G76/G81-G89). Defined in the parse layer because both `sim` and `verify` need
# the same set, and `sim` cannot import `verify` without inverting the dependency direction. G80 is
# the *cancel* and is deliberately absent: it ends a cycle rather than being one.
CANNED_CYCLE_CODES = frozenset({"73", "76", "81", "82", "83", "84", "85", "86", "87", "88", "89"})


@dataclass(slots=True, frozen=True)
class CoordTransformMode:
    """A modal coordinate transform v1 refuses rather than models.

    ``field`` is three names at once, and they must stay identical: the ``_GROUPS`` key in the
    resolver, the ``ModalState`` field the resolver writes, and the attribute `sim` and `verify` both
    read. A rename that misses one makes the field silently never update, and every span quietly
    vanishes — so `tests/test_parser.py` pins the correspondence.

    Defined here for the same reason as ``CANNED_CYCLE_CODES``: `sim` cannot import `verify`, so a
    second copy would be a second copy of *the refusal decision*, and the two halves disagreeing is
    exactly how a confidently wrong path gets drawn. ``consequence`` is shared for the same reason —
    one sentence of user-facing text in the parse layer, in exchange for the two layers being unable
    to contradict each other about why the span is not drawn.
    """

    field: str
    activate: str
    cancel: str
    name: str
    consequence: str


# Rotation, scaling and polar mode. All three are Fanuc/Mach3 constructs LinuxCNC has no equivalent
# for, all three change the programmed → machine mapping, and none is interpreted in v1. They are
# *suppressed* rather than drawn-and-marked, unlike cutter comp: comp is wrong by one tool radius and
# G43 by a uniform Z shift, both bounded and mentally correctable, while a rotation about a fixture
# origin displaces the whole path by an unbounded amount, a negative scale factor mirrors it, and
# under G16 the axis words are a radius and an angle so the drawn curve is a different curve.
COORD_TRANSFORM_MODES: tuple[CoordTransformMode, ...] = (
    CoordTransformMode(
        "rotation", "68", "69", "coordinate system rotation",
        "the coordinates in this span are the unrotated ones, so the span is not drawn",
    ),
    CoordTransformMode(
        "scaling", "51", "50", "coordinate system scaling",
        "the coordinates in this span are unscaled, so the span is not drawn",
    ),
    CoordTransformMode(
        "polar", "16", "15", "polar coordinate mode",
        "under G16 X and Y are a radius and an angle rather than cartesian coordinates, so the "
        "span is not drawn",
    ),
)  # fmt: skip

COORD_TRANSFORM_CODES = frozenset(mode.activate for mode in COORD_TRANSFORM_MODES)
COORD_TRANSFORM_CANCELS = frozenset(mode.cancel for mode in COORD_TRANSFORM_MODES)

# Subprogram call and return. v1 does not expand subprograms, so the position after one is genuinely
# unknown and `MachineState` treats it as lost — the same machinery as an undrawable G28, for the
# same reason: we had a position and no longer do.
#
# These are **M**-codes and are tested against `Command.mcodes`. G98/G99 are the canned-cycle return
# modes, are interpreted, and are silent; the tables are keyed on bare digits, so testing the wrong
# list would confuse two unrelated constructs.
SUBPROGRAM_MCODES = frozenset({"98", "99"})

# Blocks whose axis words are **parameters, not a destination**: G10 writes an offset or tool-table
# entry, G92 states what the current point is to be called. Neither moves the machine.
#
# They live here, apart from the motion tables, because the damage was never in the drawing decision
# alone: `_advance` consumed `G10 L2 P1 X50 Y50 Z-10` as a move to (50, 50, -10), so `sim` drew a
# feed line across the part *and* every later block inherited the bogus origin — and `verify`, which
# walks the same helper, reported `geometry.axis-travel-exceeded` as an **error** against a
# coordinate the machine never visits. Skipping them is a correctness fix for both layers, and is
# separate from what the datum change then means (`DATUM_SHIFT_*` below).
PARAMETER_ONLY_CODES = frozenset({"10", "92", "92.1", "92.2", "92.3"})

# The G92 family, by what each does to the datum shift. G92.1 clears it and G92.2 merely suspends
# it; the difference is real on the machine and irrelevant here, because both leave it out of force.
# G92.3 restores it, and does so **even with no G92 in this program**: the value then comes from the
# control's persistent variable file, which no file states, so a restore is at least as unknowable as
# a set and is treated identically rather than assumed to be zero.
DATUM_SHIFT_ACTIVATES = frozenset({"92", "92.3"})
DATUM_SHIFT_CANCELS = frozenset({"92.1", "92.2"})
DATUM_SHIFT_FIELD = "datum_shift"
DATUM_SHIFT_CONSEQUENCE = (
    "the coordinates in this span are stated against a datum v1 does not model, so the span is "
    "not drawn"
)

# Probing. G38.2-G38.5 are *motion* modes and are modal, so the bare blocks after one are further
# probes — the canned-cycle problem exactly. They are suppressed rather than drawn because a probe
# stops **at contact**, which is a point the file does not contain: the programmed endpoint is a
# limit on the search, not a destination. For the same reason the position afterwards is lost, the
# way it is after an undrawable G28 or an M98.
PROBE_CODES = frozenset({"38.2", "38.3", "38.4", "38.5"})
PROBE_CONSEQUENCE = (
    "a probe stops where it touches the part, which the program does not state, so the move is not "
    "drawn and the position after it is treated as unknown"
)

# Spindle-synchronized motion (threading). The one refused motion mode that is **drawn**: the path
# is a straight line to the programmed endpoint and v1 gets it exactly right. What is not modelled is
# the feed law — distance per revolution against spindle speed rather than F — so the geometry is
# trustworthy and the time estimate is not. Suppressing it would delete real geometry to avoid a
# timing error, which is the G43 trade, decided the same way.
SPINDLE_SYNC_CODES = frozenset({"33"})
SPINDLE_SYNC_CONSEQUENCE = (
    "the feed is spindle-synchronized rather than set by F, so the path is drawn but the time "
    "estimate for this span is not modelled"
)

# Dialect defaults for a program that never states them, used as ModalState's field defaults.
# PLAN.md § Dialect Divergences pins arc-centre mode: G91.1 default, G90.1 honoured.
DEFAULT_PLANE = "17"
DEFAULT_DISTANCE = "90"
DEFAULT_ARC_DISTANCE = "91.1"
DEFAULT_FEED_MODE = "94"
DEFAULT_UNITS = "mm"


@dataclass(slots=True, frozen=True)
class SourceRef:
    """Where a command came from in the original text.

    Created **once per line** and shared by every ``Command`` and segment derived from it, so the
    cost of tracking provenance is one small frozen object per source line rather than per word.

    ``start``/``end`` are offsets into the source text, not a copy of it: holding the line's text
    here would duplicate the entire file in memory.
    """

    line_no: int  # 1-based line in original file
    start: int  # offset into the source text
    end: int  # ...so we never store a per-line copy of `raw`


@dataclass(slots=True, frozen=True)
class Word:
    """One address word: a letter and its numeric value.

    ``letter`` is always upper-cased, so ``g1`` and ``G1`` tokenize identically. G and M words do
    not appear as ``Word``s in a ``Command`` — a block carries several of each, so the resolver
    moves them into ``Command.gcodes`` / ``Command.mcodes`` as canonical strings.
    """

    letter: str
    value: float


class ParseErrorKind(StrEnum):
    """What sort of problem a ``ParseError`` describes.

    The producer classifies; the verifier maps kind → rule id and severity. Without this the
    verifier would have to pattern-match ``message`` text to tell a malformed word from a
    modal-group conflict, so every wording change would silently re-route a diagnostic.

    ``UNSUPPORTED_OWORD`` is the one that is not an error at all: O-word flow control is
    well-formed and *affects which motion runs*, so it becomes ``unsupported``, never a warning.
    """

    MALFORMED_WORD = "malformed-word"
    UNTERMINATED_COMMENT = "unterminated-comment"
    UNSUPPORTED_OWORD = "unsupported-oword"
    MODAL_GROUP_CONFLICT = "modal-group-conflict"
    DUPLICATE_WORD = "duplicate-word"


@dataclass(slots=True, frozen=True)
class ParseError:
    """A problem found while tokenizing or resolving one line.

    Reported rather than raised: one bad word must not cost us the rest of the file, and the whole
    point of the verifier is to list every problem at once.

    Deliberately *not* a ``verify.Diagnostic``. The dependency direction is
    parser → machine → sim → verify, so the parse layer cannot name a verify type; the verifier
    converts these into diagnostics with a severity attached (T1.7).

    ``line`` is carried alongside ``offset`` because a ``Diagnostic`` is reported by line, and
    recovering one from the other would mean re-scanning the source.
    """

    line: int  # 1-based source line
    offset: int  # absolute offset into the source text
    text: str  # the offending characters, as written
    message: str
    kind: ParseErrorKind


@dataclass(slots=True)
class TokenizedLine:
    """One source line, split into words with its framing constructs pulled out.

    ``ref`` is created once here and shared onward by every ``Command`` and segment derived from
    this line.

    ``N`` and ``O`` never appear in ``words``: an N-number labels the line rather than addressing
    anything, and a bare ``Oxxxx`` is a Fanuc program number that this dialect consumes silently
    (PLAN.md § Supported G-code Subset).

    ``comments`` and ``errors`` are ``None`` rather than ``[]`` when empty, which is the common
    case: a 100k-line file would otherwise allocate 200k throwaway lists against a ~20 µs/line
    budget. Read them through ``line.comments or ()``, and use ``has_errors`` rather than
    ``len(errors)``.
    """

    ref: SourceRef
    words: list[Word]
    block_delete: bool = False  # line began with '/'
    line_number: float | None = None  # N
    program_number: float | None = None  # Fanuc Oxxxx
    comments: list[str] | None = None
    errors: list[ParseError] | None = None

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


@dataclass(slots=True, frozen=True)
class ModalState:
    """The modal groups in force for one command.

    Frozen and shared: the simulator holds one live ``MachineState`` and emits a new ``ModalState``
    only when something actually changes, so consecutive commands usually reference the same
    instance. Copy-on-write via ``dataclasses.replace``.

    ``units`` records the units the *program* declared, while all stored geometry is already mm.
    Diagnostics are reported in these declared units — "X exceeds 400 mm" against an inch program
    is not actionable. It is never ``None``: a program that states neither G20 nor G21 still has an
    effective unit. "Never explicitly set" is a property of the whole program, not of a modal
    group, so the verifier detects it by looking for G20/G21 across the command stream.

    ``offset`` *is* optional, because "no work offset selected yet" is a genuine modal state and
    the verifier warns on motion that occurs while it holds. ``rotation``, ``scaling`` and ``polar``
    read the same way as ``cutter_comp``: ``None`` means "not in force", and a code means the span
    it opened is still open.
    """

    units: str = DEFAULT_UNITS  # 'mm' | 'inch' — what the program declared
    plane: str = DEFAULT_PLANE  # '17' | '18' | '19'
    distance: str = DEFAULT_DISTANCE  # '90' | '91'
    arc_distance: str = DEFAULT_ARC_DISTANCE  # '90.1' | '91.1'
    feed_mode: str = DEFAULT_FEED_MODE  # '93' | '94' | '95'
    offset: str | None = None  # '54'..'59', or None if never set
    feed: float | None = None
    spindle_rpm: float | None = None
    spindle_on: str | None = None  # '3' | '4' | None
    tool: int | None = None
    length_offset: int | None = None  # active H number under G43, None under G49
    cutter_comp: str | None = None  # '41' | '42' | None
    # Refused coordinate transforms; see COORD_TRANSFORM_MODES. Each holds the activating code while
    # in force and None once cancelled.
    rotation: str | None = None  # '68' under G68, None under G69
    scaling: str | None = None  # '51' under G51, None under G50
    polar: str | None = None  # '16' under G16, None under G15
    # The G92 datum shift; see DATUM_SHIFT_ACTIVATES. Not a modal *group* — G92 is non-modal, and
    # putting it in one would make `G92 G92.1` a group conflict rather than the set-then-clear pair
    # a control honours — but its *consequence* spans exactly like a transform, so it is snapshotted
    # here and read by `sim` and `verify` the same way.
    datum_shift: str | None = None  # '92'/'92.3' while in force, None after G92.1/G92.2


@dataclass(slots=True)
class Command:
    """One G-code block, with its modal context resolved.

    Mutable and unhashable by design — unlike the two types above, a ``Command`` is produced once
    per block and never shared or used as a key.
    """

    ref: SourceRef
    gcodes: list[str]  # ['90', '21', '17', '54'] — a block carries several
    mcodes: list[str]  # ['3', '8'] — likewise
    motion: str | None  # resolved modal motion: '0'|'1'|'2'|'3'|None
    words: dict[str, float]  # axis/parameter letters only: {'X': 1.0, 'A': 90.0, 'F': 200.0}
    modal_snapshot: ModalState
