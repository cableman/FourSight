"""Parse-layer dataclasses: ``SourceRef``, ``ModalState``, ``Command``.

All three use ``slots=True``; ``SourceRef`` and ``ModalState`` are additionally frozen so they can
be shared rather than copied. ``SourceRef`` holds offsets, not text — a per-line copy of ``raw``
duplicates the whole file and costs the parse-rate target.

Must not import from ``foursight.machine`` (PLAN.md § Conventions). This module is the bottom of
the dependency order and imports nothing from the rest of the package.
"""

from dataclasses import dataclass

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
    the verifier warns on motion that occurs while it holds.
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
