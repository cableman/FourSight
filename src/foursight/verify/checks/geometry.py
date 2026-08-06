"""Geometry checks that need no simulation (T1.9).

PLAN.md § Verifier Rules, the Geometry group — minus the parts that genuinely require interpolated
points. **Travel limits here are checked on block endpoints only.** T2.8 re-runs the same check over
interpolated points, because an arc can bulge past a limit mid-sweep while both of its endpoints sit
comfortably inside it. Nothing in this module should be read as proving a program stays in bounds.

Two tolerances-and-coordinates rules that are easy to get subtly wrong:

- **Arc radius comparison happens in programmed coordinates**, deliberately. A work offset is a
  uniform translation, so it cannot change a radius — applying one would add rounding for nothing
  and would make the check depend on data the G-code file does not contain.
- **`tolerance.arc_radius_mismatch` is read from the profile, at one place.** PLAN.md calls this out
  explicitly: the check and the IJK-recompute fix (T5.2) must not each hard-code it, or they will
  disagree about what counts as broken.
"""

import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from foursight.machine.profile import AxisLimits, MachineProfile
from foursight.machine.state import Position, machine_value, walk
from foursight.parser.model import AXIS_LETTERS, Command
from foursight.verify.report import Diagnostic, Severity, format_angle, format_length
from foursight.verify.rules import Program, Rule, register_rule

ARC_MOTIONS = frozenset({"2", "3"})

# Plane → (first in-plane axis, second in-plane axis, first centre offset, second centre offset).
# G17 → I,J; G18 → I,K; G19 → J,K (PLAN.md § Arc Semantics).
_PLANE_AXES: dict[str, tuple[str, str, str, str]] = {
    "17": ("X", "Y", "I", "J"),
    "18": ("X", "Z", "I", "K"),
    "19": ("Y", "Z", "J", "K"),
}

# G90.1 makes IJK absolute centre coordinates; G91.1 (the default) makes them offsets from start.
_ARC_CENTRE_ABSOLUTE = "90.1"


@dataclass(slots=True, frozen=True)
class ArcGeometry:
    """One arc reduced to its plane, for radius comparison."""

    start: tuple[float, float]
    end: tuple[float, float]
    centre: tuple[float, float]
    axes: tuple[str, str]

    @property
    def radius_start(self) -> float:
        return math.dist(self.start, self.centre)

    @property
    def radius_end(self) -> float:
        return math.dist(self.end, self.centre)


def _start_in_plane(position: Position, axes: tuple[str, str]) -> tuple | None:
    """Where the arc starts: the carried position only, never the block's own words."""
    first, second = (position.get(letter) for letter in axes)
    if first is None or second is None:
        return None
    return (float(first), float(second))


def _end_in_plane(position: Position, command: Command, axes: tuple[str, str]) -> tuple | None:
    """Where the arc ends: the block's words where given, otherwise unchanged from the start.

    An arc may omit an axis word, which means that coordinate does not move — so falling back to the
    carried position is correct, not a guess.
    """
    values = []
    for letter in axes:
        value = command.words.get(letter, position.get(letter))
        if value is None:
            return None
        values.append(float(value))
    return (values[0], values[1])


def arc_geometry(command: Command, before: Position) -> ArcGeometry | None:
    """Reduce an IJK arc to plane geometry, or None when it is not one or is under-specified."""
    if command.motion not in ARC_MOTIONS:
        return None
    plane = _PLANE_AXES.get(command.modal_snapshot.plane)
    if plane is None:
        return None
    first, second, off_first, off_second = plane
    if off_first not in command.words and off_second not in command.words:
        return None  # R-format, or malformed — handled by ArcRFormatInvalid

    start = _start_in_plane(before, (first, second))
    end = _end_in_plane(before, command, (first, second))
    if start is None or end is None:
        return None

    delta_first = float(command.words.get(off_first, 0.0))
    delta_second = float(command.words.get(off_second, 0.0))
    if command.modal_snapshot.arc_distance == _ARC_CENTRE_ABSOLUTE:
        centre = (delta_first, delta_second)
    else:
        centre = (start[0] + delta_first, start[1] + delta_second)
    return ArcGeometry(start=start, end=end, centre=centre, axes=(first, second))


@register_rule
class ArcRadiusMismatch(Rule):
    """An IJK arc whose start and end are not the same distance from its centre.

    The centre is over-specified relative to the endpoints, so a mismatch means the three do not
    describe one arc. Beyond tolerance the intent is unknowable and drawing *something* would be
    inventing geometry.
    """

    rule_id = "geometry.arc-radius-mismatch"
    description = "Arc radius mismatch beyond tolerance.arc_radius_mismatch"
    severity = Severity.ERROR

    def check(self, program: Program) -> Iterable[Diagnostic]:
        tolerance = program.profile.tolerance.arc_radius_mismatch
        for command, before, _ in walk(program.commands):
            arc = arc_geometry(command, before)
            if arc is None:
                continue
            mismatch = abs(arc.radius_start - arc.radius_end)
            if mismatch <= tolerance:
                continue
            units = command.modal_snapshot.units
            yield Diagnostic(
                rule_id=self.rule_id,
                severity=Severity.ERROR,
                line=command.ref.line_no,
                message=(
                    f"arc radius mismatch {format_length(mismatch, units)}: start is "
                    f"{format_length(arc.radius_start, units)} from the centre, end is "
                    f"{format_length(arc.radius_end, units)} "
                    f"(tolerance {format_length(tolerance, units)})"
                ),
                fix_ids=("fix.recompute-arc-centre",),
            )


@register_rule
class ArcRFormatInvalid(Rule):
    """An R-format arc that does not describe an arc at all.

    Two cases, both undefined rather than merely imprecise:

    - **Coincident endpoints.** R gives no way to say which way round the circle to go, which is why
      a full circle is expressible in IJK but not in R.
    - **|R| smaller than half the chord.** No circle of that radius touches both endpoints.
    """

    rule_id = "geometry.arc-r-invalid"
    description = "R-format arc with coincident endpoints or an impossible radius"
    severity = Severity.ERROR

    def check(self, program: Program) -> Iterable[Diagnostic]:
        for command, before, _ in walk(program.commands):
            if command.motion not in ARC_MOTIONS or "R" not in command.words:
                continue
            plane = _PLANE_AXES.get(command.modal_snapshot.plane)
            if plane is None:
                continue
            first, second = plane[0], plane[1]
            start = _start_in_plane(before, (first, second))
            end = _end_in_plane(before, command, (first, second))
            if start is None or end is None:
                continue

            chord = math.dist(start, end)
            radius = abs(float(command.words["R"]))
            units = command.modal_snapshot.units
            if chord == 0.0:
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.ERROR,
                    line=command.ref.line_no,
                    message=(
                        "R-format arc with coincident start and end points is undefined; "
                        "a full circle must be written in IJK form"
                    ),
                    fix_ids=("fix.arc-r-to-ijk",),
                )
            elif radius * 2.0 < chord:
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.ERROR,
                    line=command.ref.line_no,
                    message=(
                        f"R-format arc radius {format_length(radius, units)} is too small to span "
                        f"the {format_length(chord, units)} between its endpoints"
                    ),
                )


@register_rule
class AxisTravelExceeded(Rule):
    """A block endpoint outside the machine's travel.

    **Endpoints only** — T2.8 extends this to interpolated points, where an arc bulging past a limit
    mid-sweep is caught. Downgraded from `error` to `warning` when the active work offset is unknown,
    per PLAN.md: without the offset the machine position is a guess, and a hard error on a guess is
    worse than a warning that says so.
    """

    rule_id = "geometry.axis-travel-exceeded"
    description = "Axis travel limit exceeded at a block endpoint"
    severity = Severity.ERROR

    def check(self, program: Program) -> Iterable[Diagnostic]:
        for command, _, after in walk(program.commands):
            if not command.words:
                continue
            yield from self._for_command(command, after, program.profile)

    def _for_command(
        self, command: Command, after: Position, profile: MachineProfile
    ) -> Iterator[Diagnostic]:
        for letter in sorted(AXIS_LETTERS & set(command.words)):
            axis = profile.axes.get(letter)
            if axis is None or axis.is_rotary:
                # No limit data, or a rotary axis — rotary is RotaryTravelExceeded's business, and
                # checking it here too would report every violation twice.
                continue
            value, offset_known = machine_value(after.get(letter), letter, command, profile)
            if value is None:
                continue
            violated = self._violation(axis, value)
            if violated is None:
                continue
            bound, name = violated
            units = command.modal_snapshot.units
            yield Diagnostic(
                rule_id=self.rule_id,
                severity=Severity.ERROR if offset_known else Severity.WARNING,
                line=command.ref.line_no,
                message=(
                    f"{letter} {format_length(value, units)} is beyond the {name} travel limit "
                    f"{format_length(bound, units)}"
                    + ("" if offset_known else " (assumes zero work offset, so unconfirmed)")
                ),
            )

    def _violation(self, axis: AxisLimits, value: float) -> tuple[float, str] | None:
        if axis.min is not None and value < axis.min:
            return axis.min, "minimum"
        if axis.max is not None and value > axis.max:
            return axis.max, "maximum"
        return None


@register_rule
class RotaryWrapWarning(Rule):
    """A single block commanding more rotary travel than `limits.rotary_wrap_warn`.

    Multi-turn wrapping is legitimate, which is why this is a tunable warning rather than an error.

    When A has not been established yet, 0 is assumed and the message says so — rotary axes are
    homed to zero in practice, and refusing to judge would skip the very first block, which is
    often the largest move in a wrapping program.
    """

    rule_id = "geometry.rotary-wrap"
    description = "Rotary move exceeds limits.rotary_wrap_warn in one block"
    severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        threshold = program.profile.limits.rotary_wrap_warn
        for command, before, after in walk(program.commands):
            if "A" not in command.words:
                continue
            assumed = before.a is None
            start = 0.0 if assumed else before.a
            end = after.a
            if end is None:
                continue
            travel = abs(end - start)
            if travel <= threshold:
                continue
            yield Diagnostic(
                rule_id=self.rule_id,
                severity=Severity.WARNING,
                line=command.ref.line_no,
                message=(
                    f"rotary move of {format_angle(travel)} in one block exceeds "
                    f"{format_angle(threshold)}" + (" (assuming A started at 0)" if assumed else "")
                ),
            )


@register_rule
class RotaryTravelExceeded(Rule):
    """Rotary travel beyond min/max on a **non-wrapping** axis.

    A wrapping axis has no travel limit to exceed, so the profile's min/max are only enforced when
    `wrap = false` (PLAN.md § Machine Profile).

    Unlike the linear travel rule, an unknown work offset does **not** downgrade this to a warning.
    PLAN.md attaches that downgrade specifically to the linear travel bullet, and a non-zero rotary
    work offset is rare in practice. The assumption is still stated in the message, so the severity
    follows the plan while the uncertainty stays visible.
    """

    rule_id = "geometry.rotary-travel-exceeded"
    description = "Rotary travel limit exceeded when axes.a.wrap = false"
    severity = Severity.ERROR

    def check(self, program: Program) -> Iterable[Diagnostic]:
        for command, _, after in walk(program.commands):
            for letter in sorted(AXIS_LETTERS & set(command.words)):
                axis = program.profile.axes.get(letter)
                if axis is None or not axis.is_rotary or axis.wrap:
                    continue
                value, offset_known = machine_value(
                    after.get(letter), letter, command, program.profile
                )
                if value is None:
                    continue
                if axis.min is not None and value < axis.min:
                    bound, name = axis.min, "minimum"
                elif axis.max is not None and value > axis.max:
                    bound, name = axis.max, "maximum"
                else:
                    continue
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.ERROR,
                    line=command.ref.line_no,
                    message=(
                        f"{letter} {format_angle(value)} is beyond the {name} rotary limit "
                        f"{format_angle(bound)} and the axis does not wrap"
                        + ("" if offset_known else " (assuming a zero rotary work offset)")
                    ),
                )
