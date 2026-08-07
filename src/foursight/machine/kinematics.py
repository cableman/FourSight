"""Rotary transforms: table mount (part rotates) and head mount (tool swings).

Table — the part turns under a fixed tool, so a tool position maps into part coordinates::

    p_part = R_axis(-A) @ (p_tool - centerline) + centerline

Head — the tool **tip translates as the head swings**; it is not simply the machine XYZ::

    p_tip = p_pivot + R_axis(A) @ (0, 0, -pivot_to_tip)

Writes ``SegmentStore.lin_part`` and **never mutates ``lin``**, which stays in machine coordinates
because that is what travel-limit verification needs. `set_part_coordinates` enforces both the shape and
the separate-array requirement.

**Why this is a second array rather than a camera transform.** The rotation depends on A at every
interpolation step, so along a simultaneous XYZ+A move it is *nonlinear* — no single view matrix can
express it, and it has to be baked into vertex positions.

**Per-step transformation comes for free here, and that is by earlier design.** PLAN.md calls
endpoint-only transformation "the single most likely source of silently wrong output": a wrapped helix
drawn from its endpoints alone becomes a straight chord. This module cannot make that mistake, because
`SegmentStore.rot` already carries a rotary value for **every segment endpoint** — T2.3 built
`rotary_step_count` and tessellated XYZ+A moves per step specifically so M4 would inherit it. The
transform is therefore a vectorized pass over 2N points, each rotated by *its own* A.

Ruff ``N806`` is ignored here because ``R`` for a rotation matrix is correct.
"""

import numpy as np

from foursight.machine.profile import Kinematics
from foursight.sim.segments import SegmentStore

#: Which array index each axis name rotates about.
_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}


class KinematicsError(Exception):
    """The profile does not describe enough to transform coordinates.

    Raised rather than defaulted. A head-mount machine with no `pivot_to_tip` has an unknown tool tip,
    and inventing a distance would draw a confident path through the wrong place — the absence-means-
    unknown rule PLAN.md applies to every other profile value.
    """


def rotate_about_axis(points: np.ndarray, axis: str, degrees: np.ndarray | float) -> np.ndarray:
    """Rotate ``(M, 3)`` points about a principal axis, with a **per-point** angle.

    ``degrees`` may be a scalar or an ``(M,)`` array. The per-point form is the one that matters: along a
    simultaneous XYZ+A move every point has its own A, and a single angle for the whole path is exactly
    the endpoint-only error.

    Applies the rotation in closed form rather than building M 3×3 matrices — same arithmetic, no
    intermediate allocation, and it reads as the rotation it is.
    """
    if axis not in _AXIS_INDEX:
        raise KinematicsError(f"rotary_axis must be one of x, y, z; got {axis!r}")
    theta = np.radians(np.asarray(degrees, dtype=np.float64))
    cos, sin = np.cos(theta), np.sin(theta)

    index = _AXIS_INDEX[axis]
    # The two components that mix, in right-handed order for a rotation about `index`.
    first, second = (index + 1) % 3, (index + 2) % 3

    rotated = np.empty_like(points, dtype=np.float64)
    rotated[:, index] = points[:, index]
    rotated[:, first] = cos * points[:, first] - sin * points[:, second]
    rotated[:, second] = sin * points[:, first] + cos * points[:, second]
    return rotated


def table_part_coordinates(
    points: np.ndarray, degrees: np.ndarray, kinematics: Kinematics
) -> np.ndarray:
    """``p_part = R_axis(-A) @ (p_tool - centerline) + centerline``, per point.

    The **negative** angle is the whole content of table mount: the part turns by +A, so a feature fixed
    in the part appears to the tool as though the tool had turned by −A. Getting the sign wrong produces a
    mirror-image wrap that looks entirely plausible.
    """
    centerline = np.asarray(kinematics.centerline_offset, dtype=np.float64)
    relative = points - centerline
    return rotate_about_axis(relative, kinematics.rotary_axis, -degrees) + centerline


def head_tip_coordinates(
    points: np.ndarray, degrees: np.ndarray, kinematics: Kinematics
) -> np.ndarray:
    """``p_tip = p_pivot + R_axis(A) @ (0, 0, -pivot_to_tip)``, per point.

    ``points`` are **pivot** positions — for a head-mount machine the programmed XYZ moves the pivot, and
    the tip hangs `pivot_to_tip` below it. As the head swings, that offset rotates, so **the tip
    translates even when XYZ does not move.** A machine at rest sweeping A alone traces an arc; treating
    the tip as the machine XYZ would draw a stationary point.
    """
    if kinematics.pivot_to_tip is None:
        raise KinematicsError(
            "rotary_mount = 'head' needs [kinematics].pivot_to_tip: without it the tool tip position "
            "is unknown, and assuming one would draw the toolpath in the wrong place"
        )
    offset = np.zeros((len(points), 3), dtype=np.float64)
    offset[:, 2] = -float(kinematics.pivot_to_tip)
    return points + rotate_about_axis(offset, kinematics.rotary_axis, degrees)


def part_coordinates(store: SegmentStore, kinematics: Kinematics) -> np.ndarray:
    """``(N, 2, 3)`` display coordinates for ``store``, per its mount type.

    Vectorized over all 2N endpoints, each rotated by the A recorded for **that endpoint** — which is what
    makes the transform per-step without any extra machinery.
    """
    if len(store) == 0:
        return np.zeros((0, 2, 3), dtype=np.float64)

    points = store.lin.reshape(-1, 3)
    degrees = store.rot.reshape(-1)

    mount = kinematics.rotary_mount
    if mount == "table":
        transformed = table_part_coordinates(points, degrees, kinematics)
    elif mount == "head":
        transformed = head_tip_coordinates(points, degrees, kinematics)
    else:
        raise KinematicsError(f"rotary_mount must be 'table' or 'head'; got {mount!r}")
    return transformed.reshape(store.lin.shape)


def apply_display_transform(store: SegmentStore, kinematics: Kinematics) -> None:
    """Compute and attach ``lin_part``. ``lin`` is left untouched.

    Costs a second ``(N, 2, 3)`` float64 array — 24 MB at 500k segments, which PLAN.md § Performance
    Requirements already accounts for. It is only computed when part coordinates are actually displayed.
    """
    store.set_part_coordinates(part_coordinates(store, kinematics))
