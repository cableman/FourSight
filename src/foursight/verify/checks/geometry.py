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

`geometry.rapid-into-stock` (M7) also lives here. It is the one rule in this module that loses nothing
without a simulation — a rapid is a straight line, so its endpoints *are* its path — but it still
prefers the interpolated geometry, which contains rapids the endpoint walker never produces.
"""

import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import numpy as np

from foursight.machine.profile import (
    AxisLimits,
    MachineProfile,
    StockBox,
    StockCylinder,
    StockEnvelope,
)
from foursight.machine.state import (
    Position,
    is_machine_absolute,
    machine_value,
    offsets_rewritten_from,
    offsets_stale_at,
    walk,
)
from foursight.parser.model import AXIS_LETTERS, Command
from foursight.sim.interpolate import PLANES
from foursight.sim.segments import Kind
from foursight.verify.report import Diagnostic, Severity, format_angle, format_length
from foursight.verify.rules import Program, Rule, register_rule

ARC_MOTIONS = frozenset({"2", "3"})

# Plane geometry comes from `sim/interpolate.PLANES` rather than a second table here. That mapping
# is direction-sensitive — G18's frame is (Z, X), not (X, Z) — and a duplicated copy is exactly the
# kind of thing that drifts. These checks only measure *distances*, for which the axis order is
# irrelevant, so borrowing the canonical spec costs nothing and removes the hazard.
_PLANE_AXES: dict[str, tuple[str, str, str, str]] = {
    code: (spec.first, spec.second, spec.first_offset, spec.second_offset)
    for code, spec in PLANES.items()
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
    """A linear axis outside the machine's travel.

    **Checked over interpolated points when a simulation is available** (T2.8), and over block
    endpoints otherwise. The difference is not cosmetic: an arc can bulge well past a limit mid-sweep
    while both of its endpoints sit comfortably inside it, so the endpoint-only form can pass a
    program that would crash the machine.

    Downgraded from `error` to `warning` when the active work offset is unknown, per PLAN.md: without
    the offset the machine position is a guess, and a hard error on a guess is worse than a warning
    that says so.

    Reported **once per source line and axis**, at the most extreme value reached. A 500k-segment
    program breaching a limit would otherwise produce thousands of identical diagnostics.
    """

    rule_id = "geometry.axis-travel-exceeded"
    description = "Axis travel limit exceeded"
    severity = Severity.ERROR

    def check(self, program: Program) -> Iterable[Diagnostic]:
        if program.segments is not None and len(program.segments):
            yield from _interpolated_violations(program, self.rule_id, rotary=False)
            return
        # A G10 rewrote the offset table the profile describes, so no machine coordinate at or after
        # it can be asserted — the same call `_offset_known_by_line` makes for the interpolated path.
        rewritten_from = offsets_rewritten_from(program.commands)
        for command, _, after in walk(program.commands):
            if not command.words:
                continue
            stale = offsets_stale_at(command.ref.line_no, rewritten_from)
            yield from self._for_command(command, after, program.profile, stale=stale)

    def _for_command(
        self, command: Command, after: Position, profile: MachineProfile, *, stale: bool = False
    ) -> Iterator[Diagnostic]:
        for letter in sorted(AXIS_LETTERS & set(command.words)):
            axis = profile.axes.get(letter)
            if axis is None or axis.is_rotary:
                # No limit data, or a rotary axis — rotary is RotaryTravelExceeded's business, and
                # checking it here too would report every violation twice.
                continue
            value, offset_known = machine_value(after.get(letter), letter, command, profile)
            offset_known = offset_known and not stale
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
        if program.segments is not None and len(program.segments):
            yield from _interpolated_violations(program, self.rule_id, rotary=True)
            return
        rewritten_from = offsets_rewritten_from(program.commands)
        for command, _, after in walk(program.commands):
            stale = offsets_stale_at(command.ref.line_no, rewritten_from)
            for letter in sorted(AXIS_LETTERS & set(command.words)):
                axis = program.profile.axes.get(letter)
                if axis is None or not axis.is_rotary or axis.wrap:
                    continue
                value, offset_known = machine_value(
                    after.get(letter), letter, command, program.profile
                )
                offset_known = offset_known and not stale
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


@register_rule
class RapidIntoStock(Rule):
    """A rapid whose straight-line path passes through the `[stock]` envelope.

    The narrow half of "the machine hits the stock" (PLAN.md § Stock and plunge checks): a box says
    where the solid *started*, never what is left of it, and that is enough to catch the traverse
    across the part at cutting depth that causes the crash.

    **A warning, though a real hit would break the machine.** The `error` tier is for claims we can
    stand behind, and this one has a known false positive: a rapid inside the envelope is safe when it
    runs through material an earlier pass removed, which is ordinary output for pocketing. Telling the
    two apart is exactly the material-removal model that is out of scope, so the honest tier is the one
    that says "look at this".

    **Endpoints are exact here, unlike travel limits.** A rapid is a straight line, so there is no
    mid-move bulge for the no-simulation fallback to miss. The interpolated path is still preferred
    when a simulation exists, because it carries moves the endpoint walker does not model — a G28's two
    legs above all, which are rapids that can cross the table.

    **A rotating table refuses a box and accepts a cylinder**, and the asymmetry is the whole reason
    `StockCylinder` exists. A box fixed in machine coordinates stops describing stock that turns with
    the part, and then fails in *both* directions — passing real collisions and inventing imaginary
    ones — so such a program gets one diagnostic saying so and no per-rapid findings. Staying silent
    would be worse still, because a verifier that finds nothing is indistinguishable from a clean
    program. A cylinder concentric with the rotary axis maps onto itself under every A rotation, so
    there is nothing to approximate: the check is exact at every angle and simply runs.
    """

    rule_id = "geometry.rapid-into-stock"
    description = "Rapid whose path passes through the [stock] envelope"
    severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        stock = program.profile.stock
        if stock is None:
            return
        if isinstance(stock, StockBox):
            rotating = _first_rotary_move(program)
            if rotating is not None:
                yield _box_cannot_turn(self.rule_id, rotating)
                return
        paths = _rapid_paths(program)
        if paths is None:
            return
        yield from _stock_breaches(program, stock, *paths)


# --------------------------------------------------------------------------- interpolated checking


def _offset_known_by_line(program: Program) -> dict[int, bool]:
    """Whether the work offset in force was actually configured, per source line.

    ``SegmentStore.lin`` is already machine coordinates, so the interpolated check needs no offset
    arithmetic — but it still needs to know whether those coordinates rest on a configured offset or
    an assumed one, because that is what decides `error` versus `warning`. A program may mix a
    configured G54 with an unconfigured G55, so this is per line rather than program-wide.

    A G10 earlier in the program rewrote the table those offsets come from, so every line at or after
    it is unknown too whatever `[offsets]` says — the same decision `MachineState.offsets_rewritten`
    makes for `sim`, taken from the one helper both call.
    """
    rewritten_from = offsets_rewritten_from(program.commands)
    known: dict[int, bool] = {}
    for command in program.commands:
        code = command.modal_snapshot.offset
        resolved = is_machine_absolute(command) or program.profile.offset(code) is not None
        line = command.ref.line_no
        if rewritten_from is not None and line >= rewritten_from:
            resolved = False
        known[line] = known.get(line, True) and resolved
    return known


def _interpolated_violations(
    program: Program, rule_id: str, *, rotary: bool
) -> Iterator[Diagnostic]:
    """Travel violations found anywhere along the interpolated path.

    Aggregated to one diagnostic per (line, axis) at the most extreme value, so a long breach reports
    once with its worst point rather than once per segment.
    """
    store = program.segments
    if store is None:
        return
    known = _offset_known_by_line(program)
    units_by_line = {
        command.ref.line_no: command.modal_snapshot.units for command in program.commands
    }

    for letter, values in _axis_values(store, rotary=rotary):
        axis = program.profile.axes.get(letter)
        if axis is None or axis.is_rotary != rotary:
            continue
        if rotary and axis.wrap:
            continue  # a wrapping axis has no travel limit to exceed
        for bound, name, outside in _breaches(axis, values):
            if not outside.any():
                continue
            yield from _worst_per_line(
                store,
                values,
                outside,
                letter,
                bound,
                name,
                rule_id,
                known,
                units_by_line,
                rotary=rotary,
            )


def _axis_values(store, *, rotary: bool) -> Iterator[tuple[str, np.ndarray]]:
    """Every interpolated coordinate per axis, flattened over both segment endpoints."""
    if rotary:
        yield "A", store.rot.reshape(-1)
        return
    for index, letter in enumerate(("X", "Y", "Z")):
        yield letter, store.lin[:, :, index].reshape(-1)


def _breaches(axis: AxisLimits, values: np.ndarray) -> Iterator[tuple[float, str, np.ndarray]]:
    if axis.min is not None:
        yield axis.min, "minimum", values < axis.min
    if axis.max is not None:
        yield axis.max, "maximum", values > axis.max


def _worst_per_line(
    store, values, outside, letter, bound, name, rule_id, known, units_by_line, *, rotary: bool
) -> Iterator[Diagnostic]:
    # Both endpoints of a segment share its source line, so the line column is repeated to match the
    # flattened coordinate array.
    lines = np.repeat(store.line, 2)
    for line in sorted({int(value) for value in lines[outside]}):
        selected = outside & (lines == line)
        extreme = values[selected]
        worst = float(extreme.min() if name == "minimum" else extreme.max())
        offset_known = known.get(line, True)
        units = units_by_line.get(line, "mm")

        def render(value: float, units: str = units) -> str:
            """`units` is bound as a default: a closure over the loop variable would use the last."""
            return format_angle(value) if rotary else format_length(value, units)

        yield Diagnostic(
            rule_id=rule_id,
            severity=Severity.ERROR if offset_known else Severity.WARNING,
            line=line,
            message=(
                f"{letter} reaches {render(worst)} along this move, beyond the {name} travel limit "
                f"{render(bound)}"
                + ("" if offset_known else " (assumes zero work offset, so unconfirmed)")
            ),
        )


# --------------------------------------------------------------------------- stock interference

_RAPID_MOTION = "0"
_Z = 2  # the Z column of an (N, 3) point array


def _first_rotary_move(program: Program) -> Command | None:
    """The first block that actually turns A, or None. Only meaningful for a rotating table.

    **A is assumed to start at 0**, matching `geometry.rotary-wrap`. Without that assumption the `A0`
    on a safe-start line reads as rotary motion and would disable the stock check for nearly every
    program that has one.
    """
    if program.profile.kinematics.rotary_mount != "table":
        return None  # a swinging head leaves the stock where it is
    for command, before, after in walk(program.commands):
        if "A" not in command.words or after.a is None:
            continue
        if after.a != (0.0 if before.a is None else before.a):
            return command
    return None


def _rapid_paths(program: Program) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Every rapid as ``(starts, ends, lines)`` in machine coordinates, or None when there are none.

    Prefers the simulated geometry, which includes rapids the endpoint walker cannot produce — the two
    legs of a G28 in particular. The fallback is exact rather than approximate for this rule, because a
    rapid is a straight line.
    """
    store = program.segments
    if store is not None and len(store):
        mask = store.mask(Kind.RAPID) & np.isin(store.line, _lines_with_known_start(program))
        if not mask.any():
            return None
        lin = store.lin[mask]
        return lin[:, 0, :], lin[:, 1, :], store.line[mask]

    starts: list[tuple[float, float, float]] = []
    ends: list[tuple[float, float, float]] = []
    lines: list[int] = []
    for command, before, after in walk(program.commands):
        if command.motion != _RAPID_MOTION or not command.words:
            continue
        start = _machine_point(before, command, program.profile)
        end = _machine_point(after, command, program.profile)
        if start is None or end is None or start == end:
            continue
        starts.append(start)
        ends.append(end)
        lines.append(command.ref.line_no)
    if not lines:
        return None
    return (
        np.array(starts, dtype=np.float64),
        np.array(ends, dtype=np.float64),
        np.array(lines, dtype=np.int32),
    )


def _lines_with_known_start(program: Program) -> np.ndarray:
    """Source lines whose move begins from a position we actually know.

    The endpoint path gets this for free — `_machine_point` returns None on an unestablished axis — but
    the `SegmentStore` does not, and cannot: the simulator deliberately draws a program's opening
    rapid from the machine reference so the picture is not missing its approach move. That assumption
    is fine for a *picture* and not fine for a *diagnostic*, which would otherwise announce a collision
    between the stock and a position nobody established. Filtering here keeps the two paths in
    agreement as well, which `test_the_two_paths_agree_on_an_ordinary_program` pins.
    """
    known = [
        command.ref.line_no
        for command, before, _ in walk(program.commands)
        if before.x is not None and before.y is not None and before.z is not None
    ]
    return np.array(sorted(set(known)), dtype=np.int32)


def _machine_point(
    position: Position, command: Command, profile: MachineProfile
) -> tuple[float, float, float] | None:
    """XYZ in machine coordinates, or None when any of the three was never established.

    Whether the work offset was *known* is deliberately ignored here and recovered per line by
    `_offset_known_by_line`, so both this path and the simulated one carry the caveat the same way.
    """
    values = []
    for letter in ("X", "Y", "Z"):
        value, _ = machine_value(position.get(letter), letter, command, profile)
        if value is None:
            return None
        values.append(float(value))
    return (values[0], values[1], values[2])


def _box_overlap(
    starts: np.ndarray, ends: np.ndarray, low: np.ndarray, high: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """The parameter interval of each segment that lies inside the box, clamped to ``[0, 1]``.

    The slab method: each axis contributes the interval over which the segment is between that axis's
    two faces, and the box interior is their intersection. A segment is inside somewhere iff
    ``enter <= leave`` in the returned pair.

    An axis with no motion has no crossing parameter at all — the division would be ±inf or, when the
    start sits exactly on a face, 0/0. Rather than lean on how numpy signs those, such an axis is
    replaced outright: it either constrains nothing (the segment is within that slab for its whole
    length) or rules the segment out entirely.
    """
    direction = ends - starts
    with np.errstate(divide="ignore", invalid="ignore"):
        to_low = (low - starts) / direction
        to_high = (high - starts) / direction
    near = np.minimum(to_low, to_high)
    far = np.maximum(to_low, to_high)

    still = direction == 0.0
    within = (starts >= low) & (starts <= high)
    near = np.where(still, np.where(within, -np.inf, np.inf), near)
    far = np.where(still, np.where(within, np.inf, -np.inf), far)

    enter = np.maximum(near.max(axis=1), 0.0)
    leave = np.minimum(far.min(axis=1), 1.0)
    return enter, leave


def _box_cannot_turn(rule_id: str, rotating: Command) -> Diagnostic:
    return Diagnostic(
        rule_id=rule_id,
        severity=Severity.WARNING,
        line=rotating.ref.line_no,
        message=(
            "stock envelope not checked: this program moves A and kinematics.rotary_mount is "
            '"table", so the stock turns with the part and a box fixed in machine coordinates no '
            'longer describes where it is. Declare the blank as shape = "cylinder" and the check '
            "holds at every angle"
        ),
    )


def _withdrawing_from_box(starts: np.ndarray, ends: np.ndarray) -> np.ndarray:
    """Rapids that only move **up**, along the tool axis, holding X and Y.

    Excluded from the check unconditionally, and this exclusion is what makes the rule usable rather
    than an academic exercise: every cut ends with a retract from inside the material, so a plain
    box-intersection test would report the `G0 Z25` at the end of every pass. Withdrawing along the
    tool axis cannot hit anything, whatever the stock is made of or how much of it is left.

    Strictly vertical only. A rapid that climbs *and* moves in X or Y sweeps laterally through material
    on its way out and is reported like any other traverse. Soundness rests on the box being convex and
    the direction constant: a straight line leaving it upward never re-enters.
    """
    return (
        (starts[:, 0] == ends[:, 0]) & (starts[:, 1] == ends[:, 1]) & (ends[:, _Z] > starts[:, _Z])
    )


def _withdrawing_from_cylinder(
    radial: np.ndarray, radial_end: np.ndarray, axial: np.ndarray, axial_end: np.ndarray
) -> np.ndarray:
    """The cylinder's equivalent of a vertical retract: moving **radially outward**, no axial motion.

    "Up" is the wrong test for a round blank, and dangerously so. With the rotary axis along X, a tool
    working the *underside* of the part retracts in **−Z**, and a `+Z is always safe` rule would both
    miss that and — worse — exempt a `+Z` move from below the centreline, which drives straight through
    the middle of the stock. Radially outward is the property that actually makes a withdrawal safe, and
    it reduces to the vertical case when the tool is above the axis.

    Monotonically outward, not merely ending further out: radial distance along a straight line is
    convex, so a segment can end further from the axis than it started while dipping closer in between.
    The closest approach is at ``t = -(p0·d)/|d|²``, so demanding ``p0·d >= 0`` puts it at or before the
    start and makes the distance non-decreasing across the whole segment.
    """
    direction = radial_end - radial
    moving = (direction * direction).sum(axis=1)
    outward = (radial * direction).sum(axis=1)
    return (axial == axial_end) & (moving > 0.0) & (outward >= 0.0)


def _radial_overlap(
    radial: np.ndarray, radial_end: np.ndarray, radius: float
) -> tuple[np.ndarray, np.ndarray]:
    """Where each segment is within ``radius`` of the axis, as a parameter interval.

    ``radial`` holds each point's offset from the axis in the two axes perpendicular to it, so this is
    a line-versus-circle problem: solve ``|p + t·d|² = r²``. A segment with no radial motion has no
    crossing parameter at all and is handled outright, exactly as a stationary axis is in the box case.
    """
    direction = radial_end - radial
    a = (direction * direction).sum(axis=1)
    b = 2.0 * (radial * direction).sum(axis=1)
    c = (radial * radial).sum(axis=1) - radius * radius

    moving = a > 0.0
    discriminant = b * b - 4.0 * a * c
    with np.errstate(divide="ignore", invalid="ignore"):
        root = np.sqrt(np.maximum(discriminant, 0.0))
        near = (-b - root) / (2.0 * a)
        far = (-b + root) / (2.0 * a)

    # No radial motion: the whole segment is inside the circle, or none of it is.
    within = c <= 0.0
    near = np.where(moving, near, np.where(within, -np.inf, np.inf))
    far = np.where(moving, far, np.where(within, np.inf, -np.inf))
    # Radially moving but missing the circle entirely.
    missed = moving & (discriminant < 0.0)
    return np.where(missed, np.inf, near), np.where(missed, -np.inf, far)


def _closest_radius(
    radial: np.ndarray, radial_end: np.ndarray, enter: np.ndarray, leave: np.ndarray
) -> np.ndarray:
    """How near the axis each segment gets *within* its inside interval.

    The reportable number for a round blank, the way the lowest Z is for a box: a machinist compares it
    against the stock radius. Unlike Z it is not linear in the parameter, so the extreme is at the
    perpendicular foot ``t = -(p0·d)/|d|²`` clamped into the interval, not at an endpoint.
    """
    direction = radial_end - radial
    moving = (direction * direction).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        foot = np.where(moving > 0.0, -(radial * direction).sum(axis=1) / moving, 0.0)
    at = np.clip(foot, enter, leave)
    closest = radial + at[:, None] * direction
    return np.hypot(closest[:, 0], closest[:, 1])


#: Which coordinate is along the rotary axis, and which two are across it.
_AXIS_COLUMNS: dict[str, tuple[int, tuple[int, int]]] = {
    "x": (0, (1, 2)),
    "y": (1, (0, 2)),
    "z": (2, (0, 1)),
}


def _breach(
    profile: MachineProfile, stock: StockEnvelope, starts: np.ndarray, ends: np.ndarray
) -> tuple[np.ndarray, np.ndarray, str]:
    """Which segments enter the stock, how far in each reaches, and the bound to quote against it."""
    if isinstance(stock, StockBox):
        return _box_breach(stock, starts, ends)
    return _cylinder_breach(profile, stock, starts, ends)


def _box_breach(
    stock: StockBox, starts: np.ndarray, ends: np.ndarray
) -> tuple[np.ndarray, np.ndarray, str]:
    low = np.asarray(stock.min, dtype=np.float64)
    high = np.asarray(stock.max, dtype=np.float64)
    enter, leave = _box_overlap(starts, ends, low, high)
    inside = (enter <= leave) & ~_withdrawing_from_box(starts, ends)
    # Narrowed to the intersecting segments *before* the arithmetic, not after. A segment ruled out by
    # a stationary axis outside its slab carries an infinite bound, and `inf * 0` for a Z that does not
    # move is a NaN — which would both warn and, if one ever reached the aggregation, silently win a
    # `min()`. Every `enter`/`leave` that survives the mask is inside [0, 1] and therefore finite.
    z_start, z_end = starts[inside, _Z], ends[inside, _Z]
    span = z_end - z_start
    deepest = np.minimum(z_start + enter[inside] * span, z_start + leave[inside] * span)
    return inside, deepest, "top"


def _cylinder_breach(
    profile: MachineProfile, stock: StockCylinder, starts: np.ndarray, ends: np.ndarray
) -> tuple[np.ndarray, np.ndarray, str]:
    """A round blank on the rotary axis, so the test is a line against a capped cylinder.

    The axis comes from `[kinematics]` rather than from `[stock]`, which is what makes this exact under
    rotation: a cylinder concentric with the rotary axis is unchanged by any A, so there is no angle at
    which the answer differs.
    """
    axial_column, radial_columns = _AXIS_COLUMNS[profile.kinematics.rotary_axis]
    centre = np.asarray(profile.kinematics.centerline_offset, dtype=np.float64)[
        list(radial_columns)
    ]
    radial = starts[:, radial_columns] - centre
    radial_end = ends[:, radial_columns] - centre
    axial = starts[:, axial_column]
    axial_end = ends[:, axial_column]

    radial_near, radial_far = _radial_overlap(radial, radial_end, stock.radius)
    # The flat ends, as a one-axis slab — the same machinery the box uses, over a single column.
    axial_near, axial_far = _box_overlap(
        axial[:, None],
        axial_end[:, None],
        np.array([stock.axis_min]),
        np.array([stock.axis_max]),
    )
    enter = np.maximum(np.maximum(radial_near, axial_near), 0.0)
    leave = np.minimum(np.minimum(radial_far, axial_far), 1.0)
    inside = (enter <= leave) & ~_withdrawing_from_cylinder(radial, radial_end, axial, axial_end)
    closest = _closest_radius(radial[inside], radial_end[inside], enter[inside], leave[inside])
    return inside, closest, "radius"


def _stock_breaches(
    program: Program,
    stock: StockEnvelope,
    starts: np.ndarray,
    ends: np.ndarray,
    lines: np.ndarray,
) -> Iterator[Diagnostic]:
    """One diagnostic per source line, at the furthest into the stock that line's rapids reach.

    "Furthest in" is the lowest Z for a box and the closest approach to the axis for a cylinder. Both
    are the number a machinist checks against the blank, and both are always meaningful: no intersection
    is possible without the move getting past the face or inside the radius.
    """
    inside, reach, bound = _breach(program.profile, stock, starts, ends)
    if not inside.any():
        return
    breached_lines = lines[inside]
    known = _offset_known_by_line(program)
    units_by_line = {
        command.ref.line_no: command.modal_snapshot.units for command in program.commands
    }

    for line in sorted({int(value) for value in breached_lines}):
        worst = float(reach[breached_lines == line].min())
        units = units_by_line.get(line, "mm")
        caveat = "" if known.get(line, True) else " (assumes zero work offset)"
        if bound == "radius":
            detail = (
                f"reaching {format_length(worst, units)} from the rotary axis while the stock "
                f"radius is {format_length(stock.radius, units)}"
            )
        else:
            detail = (
                f"reaching Z {format_length(worst, units)} while the stock top is Z "
                f"{format_length(stock.max[_Z], units)}"
            )
        yield Diagnostic(
            rule_id="geometry.rapid-into-stock",
            severity=Severity.WARNING,
            line=line,
            message=f"rapid passes through the stock envelope, {detail}{caveat}",
        )
