"""Lines and arcs → point sequences, ready for ``SegmentBuilder.add_polyline``.

PLAN.md calls arcs the component most likely to be subtly wrong, so the two things most likely to be
wrong are settled explicitly here.

**Plane frames are right-handed, which is not the obvious choice.** "G2 is clockwise viewed from
+normal" only means "the angle decreases" in a frame where ``first × second = +normal``. For G18
that frame is **(Z, X)**, not (X, Z): ``X × Z = -Y``, so an (X, Z) frame is left-handed and silently
*reverses* G2 and G3. That is precisely the "in the XZ plane, G2 appears counter-clockwise viewed
from +Y" trap the plan warns about. The offsets follow the frame, so G18's first offset is **K**.

**Tessellation is adaptive on chord height, never a fixed step count.** For a sagitta ``h`` at radius
``r`` over a step of ``Δθ``, ``h = r(1 - cos(Δθ/2))``, so the largest permissible step is
``2·arccos(1 - tol/r)``. The consequence is why a fixed count cannot work: at
``tolerance.arc_chord = 0.01 mm`` a full circle needs ~16 segments at r = 0.5 mm and ~497 at
r = 500 mm.

Interpolation is **per-step from the start**, including the rotary axis, so M4's kinematics is a
transform over existing points rather than a rewrite. Endpoint-only interpolation would render a
wrapped or helical path as a straight chord.
"""

import math
from dataclasses import dataclass

import numpy as np

from foursight.machine.profile import Kinematics, Tolerances
from foursight.parser.model import Command

ARC_MOTIONS = frozenset({"2", "3"})
CLOCKWISE = "2"

# Guard against a degenerate radius producing an unbounded step count. At sane tolerances the maths
# is already gentle (~497 segments for a 500 mm full circle at 0.01 mm), so this only ever trips on
# absurd input, and tripping it is reported rather than silently truncating the arc.
MAX_STEPS_PER_ARC = 100_000


@dataclass(slots=True, frozen=True)
class PlaneSpec:
    """A right-handed 2D frame plus its normal axis, and the offset words that address it.

    ``first × second == +normal`` holds for all three planes, which is what makes "G2 decreases the
    angle" true in every one of them.
    """

    first: str
    second: str
    normal: str
    first_offset: str
    second_offset: str

    @property
    def axis_pair(self) -> frozenset[str]:
        """The two in-plane axes, order discarded — for callers that only need distances."""
        return frozenset({self.first, self.second})


# The canonical mapping. `verify/checks/geometry.py` derives its (order-irrelevant) axis pair from
# here rather than keeping a second table, because a duplicated direction-sensitive mapping is
# exactly the kind of thing that drifts.
PLANES: dict[str, PlaneSpec] = {
    "17": PlaneSpec("X", "Y", "Z", "I", "J"),
    "18": PlaneSpec("Z", "X", "Y", "K", "I"),
    "19": PlaneSpec("Y", "Z", "X", "J", "K"),
}

_AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2}


@dataclass(slots=True, frozen=True)
class Path:
    """An interpolated move.

    ``points`` is ``(M, 3)`` XYZ and ``rotations`` is ``(M,)`` degrees — separate arrays, never a
    single ``(M, 4)``, so no norm can be taken across millimetres and degrees.

    ``error`` is set when the block could not be interpolated; ``points`` is then empty, because the
    alternative is drawing a plausible-looking line through geometry we did not understand.
    """

    points: np.ndarray
    rotations: np.ndarray
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def failed(cls, reason: str) -> "Path":
        return cls(
            points=np.empty((0, 3), dtype=np.float64),
            rotations=np.empty(0, dtype=np.float64),
            error=reason,
        )


def arc_step_count(radius: float, sweep: float, chord_tolerance: float) -> int:
    """Steps needed to keep the chord's sagitta within ``chord_tolerance``.

    ``h = r(1 - cos(Δθ/2))`` inverted for Δθ. A radius at or below the tolerance cannot deviate by
    more than the tolerance however it is cut, so one step suffices.
    """
    sweep = abs(sweep)
    if sweep <= 0.0 or radius <= 0.0 or chord_tolerance <= 0.0:
        return 1
    ratio = chord_tolerance / radius
    if ratio >= 2.0:
        return 1
    max_step = 2.0 * math.acos(1.0 - ratio)
    if max_step <= 0.0:
        return MAX_STEPS_PER_ARC
    return max(1, math.ceil(sweep / max_step))


def rotary_step_count(
    rotary_sweep_degrees: float,
    start: np.ndarray,
    end: np.ndarray,
    kinematics: Kinematics,
    rotary_chord: float,
) -> int:
    """Steps needed so a rotary sweep stays within ``rotary_chord`` at the path's own radius.

    Measured at the greater of the two endpoints' distances from the rotary centerline: with no stock
    model, the path's radius is the only available proxy for the part's (PLAN.md § Rotary
    Kinematics). A path lying on the centerline sweeps through no distance at all, so one step.

    Implemented here rather than in M4 so that per-step interpolation is real from M2 onward — the
    plan's stated reason for shaping M2 around the 4-axis model.
    """
    sweep = abs(rotary_sweep_degrees)
    if sweep <= 0.0 or rotary_chord <= 0.0:
        return 1
    radius = max(
        _distance_from_centerline(start, kinematics),
        _distance_from_centerline(end, kinematics),
    )
    return arc_step_count(radius, math.radians(sweep), rotary_chord)


def interpolate(
    command: Command,
    start: np.ndarray,
    end: np.ndarray,
    rot_start: float,
    rot_end: float,
    tolerance: Tolerances,
    kinematics: Kinematics,
) -> Path:
    """Turn one move into a point sequence. ``start``/``end`` are ``(3,)`` XYZ in machine mm."""
    start = np.asarray(start, dtype=np.float64)
    end = np.asarray(end, dtype=np.float64)
    rotary_sweep = rot_end - rot_start
    rotary_steps = rotary_step_count(rotary_sweep, start, end, kinematics, tolerance.rotary_chord)

    if command.motion not in ARC_MOTIONS:
        return _straight(start, end, rot_start, rot_end, rotary_steps)
    return _arc(command, start, end, rot_start, rot_end, tolerance, rotary_steps)


# --------------------------------------------------------------------------- straight moves


def _straight(
    start: np.ndarray, end: np.ndarray, rot_start: float, rot_end: float, steps: int
) -> Path:
    """A line needs one segment geometrically, but a simultaneous rotary move needs more.

    Subdividing a straight XYZ move looks wasteful until M4: under a rotary table the *part* sees an
    arc, and endpoint-only interpolation would draw it as a chord.
    """
    fractions = np.linspace(0.0, 1.0, steps + 1)
    points = start + fractions[:, None] * (end - start)
    points[-1] = end
    return Path(points=points, rotations=rot_start + fractions * (rot_end - rot_start))


# --------------------------------------------------------------------------- arcs


def _arc(
    command: Command,
    start: np.ndarray,
    end: np.ndarray,
    rot_start: float,
    rot_end: float,
    tolerance: Tolerances,
    rotary_steps: int,
) -> Path:
    plane = PLANES.get(command.modal_snapshot.plane)
    if plane is None:
        return Path.failed(f"unknown plane G{command.modal_snapshot.plane}")

    first, second = _AXIS_INDEX[plane.first], _AXIS_INDEX[plane.second]
    p0 = np.array([start[first], start[second]])
    p1 = np.array([end[first], end[second]])
    clockwise = command.motion == CLOCKWISE

    centre_result = _centre(command, plane, p0, p1, clockwise)
    if isinstance(centre_result, str):
        return Path.failed(centre_result)
    centre = centre_result

    radius = float(np.hypot(*(p0 - centre)))
    if radius <= 0.0:
        return Path.failed("arc start coincides with its centre, so no radius is defined")
    sweep = _sweep(p0, p1, centre, clockwise)

    steps = max(arc_step_count(radius, sweep, tolerance.arc_chord), rotary_steps)
    if steps >= MAX_STEPS_PER_ARC:
        return Path.failed(
            f"arc needs {steps} segments to hold the {tolerance.arc_chord} mm chord tolerance, "
            "which exceeds the per-arc limit"
        )

    angles = np.linspace(0.0, sweep, steps + 1)
    if clockwise:
        angles = -angles
    start_angle = math.atan2(p0[1] - centre[1], p0[0] - centre[0])

    points = np.empty((steps + 1, 3), dtype=np.float64)
    points[:, first] = centre[0] + radius * np.cos(start_angle + angles)
    points[:, second] = centre[1] + radius * np.sin(start_angle + angles)
    # The plane-normal axis interpolates linearly across the sweep: that is what makes it a helix.
    normal = _AXIS_INDEX[plane.normal]
    fractions = np.linspace(0.0, 1.0, steps + 1)
    points[:, normal] = start[normal] + fractions * (end[normal] - start[normal])
    # Land exactly on the commanded endpoint rather than however the trigonometry rounded.
    points[0] = start
    points[-1] = end
    return Path(points=points, rotations=rot_start + fractions * (rot_end - rot_start))


def _centre(
    command: Command, plane: PlaneSpec, p0: np.ndarray, p1: np.ndarray, clockwise: bool
) -> np.ndarray | str:
    """The arc centre in plane coordinates, or a reason it cannot be determined."""
    words = command.words
    has_ijk = plane.first_offset in words or plane.second_offset in words
    if has_ijk:
        first = float(words.get(plane.first_offset, 0.0))
        second = float(words.get(plane.second_offset, 0.0))
        if command.modal_snapshot.arc_distance == "90.1":
            return np.array([first, second])  # absolute centre
        return p0 + np.array([first, second])  # incremental from the start point
    if "R" in words:
        return _centre_from_radius(float(words["R"]), p0, p1, clockwise)
    return (
        f"arc has neither {plane.first_offset}/{plane.second_offset} offsets nor R, "
        "so its centre is undefined"
    )


def _centre_from_radius(
    radius: float, p0: np.ndarray, p1: np.ndarray, clockwise: bool
) -> np.ndarray | str:
    """R-format centre. Positive R selects the arc ≤ 180°, negative the arc > 180°.

    Both candidate centres are computed and the one whose sweep matches the sign is chosen, rather
    than deriving a sign rule by hand. The sign conventions here are easy to get backwards, and this
    way the code checks itself against the definition.
    """
    chord = p1 - p0
    distance = float(np.hypot(*chord))
    if distance == 0.0:
        return (
            "R-format arc with coincident start and end points is undefined; "
            "a full circle must be written in IJK form"
        )
    magnitude = abs(radius)
    if magnitude * 2.0 < distance:
        return (
            f"R-format arc radius {magnitude} is too small to span the {distance:.6g} "
            "between its endpoints"
        )
    half = distance / 2.0
    height = math.sqrt(max(0.0, magnitude * magnitude - half * half))
    midpoint = (p0 + p1) / 2.0
    perpendicular = np.array([-chord[1], chord[0]]) / distance

    wants_major = radius < 0.0
    for candidate in (midpoint + height * perpendicular, midpoint - height * perpendicular):
        sweep = _sweep(p0, p1, candidate, clockwise)
        is_major = sweep > math.pi
        if is_major == wants_major:
            return candidate
    # Exactly 180°: both candidates give the same sweep, so either is correct.
    return midpoint + height * perpendicular


def _sweep(p0: np.ndarray, p1: np.ndarray, centre: np.ndarray, clockwise: bool) -> float:
    """Swept angle in radians, always positive, measured in the commanded direction.

    Coincident endpoints mean a **full circle**, not a zero-length arc — that is the case IJK can
    express and R cannot.
    """
    a0 = math.atan2(p0[1] - centre[1], p0[0] - centre[0])
    a1 = math.atan2(p1[1] - centre[1], p1[0] - centre[0])
    delta = (a0 - a1) if clockwise else (a1 - a0)
    sweep = delta % (2.0 * math.pi)
    if sweep <= 1e-12 and np.allclose(p0, p1):
        return 2.0 * math.pi
    return sweep


def _distance_from_centerline(point: np.ndarray, kinematics: Kinematics) -> float:
    """Perpendicular distance from the rotary centerline, in the plane it rotates in."""
    axis = kinematics.rotary_axis
    centre = kinematics.centerline_offset
    if axis == "x":
        return float(math.hypot(point[1] - centre[1], point[2] - centre[2]))
    if axis == "y":
        return float(math.hypot(point[0] - centre[0], point[2] - centre[2]))
    return float(math.hypot(point[0] - centre[0], point[1] - centre[1]))
