"""``Diagnostic`` and the three-tier severity taxonomy.

Severity is not decoration — the distinction between ``unsupported`` and ``warning`` is what stops
FourSight drawing a confidently wrong toolpath:

- ``error`` — malformed, or a construct that would break the machine.
- ``unsupported`` — well-formed, recognized, **affects motion**, not interpreted by v1. The affected
  span is marked or suppressed, never drawn as if understood.
- ``warning`` — suspicious, or unrecognized but inert. Rendered normally.

An unrecognized code that never touches position is a ``warning``. One that changes how subsequent
motion is interpreted is ``unsupported``, never a ``warning``.

Positions are reported **in the program's declared units**: "X exceeds 400 mm" against a program
written in inches is not actionable. Geometry is mm internally, so the formatting helpers here
convert on the way out.
"""

from dataclasses import dataclass
from enum import StrEnum

INCH_TO_MM = 25.4


class Severity(StrEnum):
    """Three tiers, per PLAN.md § Diagnostic severity taxonomy.

    ``StrEnum`` so CLI output and comparisons against plain strings work without conversion.
    """

    ERROR = "error"
    UNSUPPORTED = "unsupported"
    WARNING = "warning"


# Sort order for presentation: worst first. Not the enum's definition order by accident — it is
# relied on, so it is written down. **Public**, because the CLI and the GUI diagnostics panel both order
# by it and a second ordering that drifted would make the same program read differently in the two front
# ends.
SEVERITY_RANK = {Severity.ERROR: 0, Severity.UNSUPPORTED: 1, Severity.WARNING: 2}


@dataclass(slots=True, frozen=True)
class Diagnostic:
    """One finding about a program.

    Frozen and hashable on purpose: the fixture strategy (PLAN.md § Testing Strategy) diffs the
    *set* of diagnostics a mutated file produces against its clean baseline, which needs set
    membership to work.

    ``fix_ids`` is plural because several fixes are tied to no diagnostic at all and some
    diagnostics have more than one candidate fix. It is a ``tuple`` rather than a ``list`` so the
    dataclass can stay frozen and hashable; the plurality PLAN.md calls for is what matters.

    ``rule_id`` is not in PLAN.md's sketch but is needed by everything downstream: tests assert on
    it rather than on message text, and any future suppression mechanism keys on it.

    ``offset`` is optional and carried when known — ``ParseError`` already has it, and discarding
    information we hold would cost precise editor highlighting later.
    """

    rule_id: str
    severity: Severity
    line: int  # 1-based source line
    message: str
    fix_ids: tuple[str, ...] = ()
    offset: int | None = None  # absolute character offset, when known

    @property
    def is_error(self) -> bool:
        return self.severity is Severity.ERROR


def sort_key(diagnostic: Diagnostic) -> tuple[int, int, str]:
    """Source order first, then worst-severity, then rule id for a stable result."""
    return (diagnostic.line, SEVERITY_RANK[diagnostic.severity], diagnostic.rule_id)


def sort_diagnostics(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    return sorted(diagnostics, key=sort_key)


def format_length(mm: float, units: str) -> str:
    """Render an internal mm length in the program's declared units.

    Inch gets more decimals than mm: one inch is 25.4 mm, so equal decimal counts would lose
    precision exactly where a machinist is most likely to be checking a number.
    """
    if units == "inch":
        return f"{_trim(mm / INCH_TO_MM, 4)} in"
    return f"{_trim(mm, 3)} mm"


def format_feed(mm_per_min: float, units: str) -> str:
    """Render an internal mm/min feed rate in the program's declared units."""
    if units == "inch":
        return f"{_trim(mm_per_min / INCH_TO_MM, 4)} in/min"
    return f"{_trim(mm_per_min, 3)} mm/min"


def format_angle(degrees: float) -> str:
    """Rotary values are degrees in every unit mode, so this takes no `units` argument.

    Having no parameter to get wrong is the point: converting an angle by 25.4 is the exact class of
    bug the linear/rotary split exists to prevent.
    """
    return f"{_trim(degrees, 3)} deg"


def _trim(value: float, decimals: int) -> str:
    """Fixed precision with trailing zeros removed, so 400.0 reads as '400'.

    A value that rounds to zero renders as '0', never '-0': a tiny negative number such as
    -0.0001 formats to '-0.000', and trimming that leaves a '-0' that reads like a real quantity.
    """
    text = f"{value:.{decimals}f}"
    if float(text) == 0.0:
        return "0"
    return text.rstrip("0").rstrip(".")
