"""Kinematics tests (T4.1–T4.4). No Qt.

PLAN.md § Testing Strategy: *"compare transformed paths against closed-form expectations (helix on a
cylinder), not against previously-generated output."* That distinction is the point of this file. A golden
would happily lock in a mirrored wrap or a sign error — it only knows the output changed, never that it was
ever right. Everything here is checked against geometry computed independently.

The two failures worth engineering against, both of which look entirely plausible on screen:

- **A sign error on table mount.** The part turns by +A, so a feature fixed in the part appears to the tool
  as though the tool turned by −A. Flip it and you get a mirror-image wrap.
- **Endpoint-only transformation.** A wrapped helix transformed from its two endpoints alone becomes a
  straight chord. `test_a_wrapped_helix_is_not_a_straight_chord` measures the difference rather than
  assuming the per-step path is used.
"""

import numpy as np
import pytest

from foursight.machine.kinematics import (
    KinematicsError,
    apply_display_transform,
    head_tip_coordinates,
    part_coordinates,
    rotate_about_axis,
    table_part_coordinates,
)
from foursight.machine.profile import Kinematics, load_profile_text
from foursight.sim.segments import Kind, SegmentBuilder, SegmentStore
from foursight.sim.simulator import simulate_text

TABLE = Kinematics(rotary_mount="table", rotary_axis="x", centerline_offset=(0.0, 0.0, 0.0))
TABLE_OFFSET = Kinematics(rotary_mount="table", rotary_axis="x", centerline_offset=(0.0, 0.0, 50.0))
HEAD = Kinematics(rotary_mount="head", rotary_axis="x", pivot_to_tip=120.0)


def store_of(points, rotations, line_no: int = 1) -> SegmentStore:
    """A store whose segment endpoints are exactly `points`, with `rotations` degrees at each."""
    builder = SegmentBuilder()
    builder.add_polyline(
        np.asarray(points, dtype=np.float64),
        Kind.FEED,
        line_no,
        rotations=np.asarray(rotations, dtype=np.float64),
    )
    return builder.finalize()


# --------------------------------------------------------------------------- the rotation itself


@pytest.mark.parametrize(
    ("axis", "point", "degrees", "expected"),
    [
        ("x", (0.0, 10.0, 0.0), 90.0, (0.0, 0.0, 10.0)),
        ("x", (0.0, 0.0, 10.0), 90.0, (0.0, -10.0, 0.0)),
        ("y", (0.0, 0.0, 10.0), 90.0, (10.0, 0.0, 0.0)),
        ("z", (10.0, 0.0, 0.0), 90.0, (0.0, 10.0, 0.0)),
    ],
)
def test_rotation_is_right_handed_about_each_axis(axis, point, degrees, expected) -> None:
    """Hand-computed quarter turns. A left-handed rotation mirrors every wrapped path."""
    result = rotate_about_axis(np.array([point]), axis, degrees)
    assert result[0] == pytest.approx(expected, abs=1e-12)


def test_rotation_preserves_distance_from_the_axis() -> None:
    """A rotation is rigid; if it is not, the transform is doing something other than rotating."""
    points = np.random.default_rng(0).normal(size=(200, 3)) * 40.0
    rotated = rotate_about_axis(points, "x", np.linspace(0, 720, 200))
    assert np.linalg.norm(rotated[:, 1:], axis=1) == pytest.approx(
        np.linalg.norm(points[:, 1:], axis=1)
    )
    assert rotated[:, 0] == pytest.approx(points[:, 0]), "the axis component must not move"


def test_a_full_turn_is_the_identity() -> None:
    points = np.array([[3.0, 4.0, 5.0], [-7.0, 1.0, 0.0]])
    assert rotate_about_axis(points, "x", 360.0) == pytest.approx(points, abs=1e-12)


def test_the_angle_may_differ_per_point() -> None:
    """The property that makes the transform per-step rather than per-block."""
    points = np.array([[0.0, 10.0, 0.0], [0.0, 10.0, 0.0]])
    rotated = rotate_about_axis(points, "x", np.array([0.0, 90.0]))
    assert rotated[0] == pytest.approx([0.0, 10.0, 0.0], abs=1e-12)
    assert rotated[1] == pytest.approx([0.0, 0.0, 10.0], abs=1e-12)


def test_an_unknown_axis_is_refused() -> None:
    with pytest.raises(KinematicsError, match="rotary_axis"):
        rotate_about_axis(np.array([[1.0, 0.0, 0.0]]), "w", 90.0)


# --------------------------------------------------------------------------- table mount (T4.1)


def test_table_mount_rotates_the_opposite_way_to_the_axis() -> None:
    """The sign that distinguishes a correct wrap from its mirror image.

    The part turns by +A, so a point fixed in the part appears to the tool at −A. Checked against a
    hand-computed quarter turn rather than against recorded output.
    """
    tool = np.array([[0.0, 10.0, 0.0]])
    part = table_part_coordinates(tool, np.array([90.0]), TABLE)
    assert part[0] == pytest.approx([0.0, 0.0, -10.0], abs=1e-12)
    # And the opposite sign gives the mirror, which is what a sign error would produce.
    assert rotate_about_axis(tool, "x", 90.0)[0] == pytest.approx([0.0, 0.0, 10.0], abs=1e-12)


def test_table_mount_at_zero_degrees_is_the_identity() -> None:
    tool = np.array([[10.0, 20.0, 30.0]])
    assert table_part_coordinates(tool, np.array([0.0]), TABLE_OFFSET) == pytest.approx(tool)


def test_a_point_on_the_centerline_never_moves() -> None:
    """Radius zero means no arc, whatever A does. A moving centreline point means the offset is misapplied."""
    on_axis = np.array([[5.0, 0.0, 50.0], [-3.0, 0.0, 50.0]])
    for degrees in (0.0, 37.0, 180.0, 359.0):
        result = table_part_coordinates(on_axis, np.full(2, degrees), TABLE_OFFSET)
        assert result == pytest.approx(on_axis, abs=1e-12)


def test_the_centerline_offset_is_applied_about_the_right_point() -> None:
    """Rotating about the origin instead of the centreline displaces the whole part."""
    tool = np.array([[0.0, 0.0, 60.0]])  # 10 mm above a centreline at z = 50
    part = table_part_coordinates(tool, np.array([180.0]), TABLE_OFFSET)
    assert part[0] == pytest.approx([0.0, 0.0, 40.0], abs=1e-9), (
        "should land 10 mm below the centreline"
    )


def test_a_wrap_traces_a_circle_of_the_correct_radius() -> None:
    """Closed form: a tool held at radius r while A sweeps must map onto a circle of radius r."""
    radius = 25.0
    degrees = np.linspace(0.0, 360.0, 73)
    tool = np.tile(np.array([0.0, radius, 50.0]), (degrees.size, 1))
    part = table_part_coordinates(tool, degrees, TABLE_OFFSET)
    from_axis = np.linalg.norm(part[:, 1:] - np.array([0.0, 50.0]), axis=1)
    assert from_axis == pytest.approx(np.full(degrees.size, radius), abs=1e-9)


# --------------------------------------------------------------------------- head mount (T4.2)


def test_the_tip_hangs_below_the_pivot_at_zero_degrees() -> None:
    pivot = np.array([[10.0, 20.0, 300.0]])
    tip = head_tip_coordinates(pivot, np.array([0.0]), HEAD)
    assert tip[0] == pytest.approx([10.0, 20.0, 300.0 - 120.0], abs=1e-12)


def test_the_tip_translates_when_the_head_swings_and_xyz_does_not() -> None:
    """The whole content of head mount, and the thing that is *not* the machine XYZ.

    A machine standing still while A sweeps 90° moves its tool tip by the full pivot length. Treating the
    tip as the programmed XYZ would draw a stationary point where the tool sweeps an arc.
    """
    pivot = np.array([[0.0, 0.0, 0.0]])
    at_zero = head_tip_coordinates(pivot, np.array([0.0]), HEAD)[0]
    at_ninety = head_tip_coordinates(pivot, np.array([90.0]), HEAD)[0]

    assert at_zero == pytest.approx([0.0, 0.0, -120.0], abs=1e-12)
    assert at_ninety == pytest.approx([0.0, 120.0, 0.0], abs=1e-12)
    assert np.linalg.norm(at_ninety - at_zero) == pytest.approx(120.0 * np.sqrt(2), abs=1e-9)


def test_the_tip_stays_a_fixed_distance_from_the_pivot() -> None:
    """Closed form: the tip lies on a sphere of radius `pivot_to_tip` about the pivot, always."""
    pivot = np.tile(np.array([5.0, -2.0, 200.0]), (37, 1))
    degrees = np.linspace(-180.0, 180.0, 37)
    tip = head_tip_coordinates(pivot, degrees, HEAD)
    assert np.linalg.norm(tip - pivot, axis=1) == pytest.approx(np.full(37, 120.0), abs=1e-9)


def test_head_mount_without_pivot_to_tip_is_refused() -> None:
    """Absence means unknown. Assuming a length would draw the toolpath in the wrong place."""
    incomplete = Kinematics(rotary_mount="head", rotary_axis="x", pivot_to_tip=None)
    with pytest.raises(KinematicsError, match="pivot_to_tip"):
        head_tip_coordinates(np.array([[0.0, 0.0, 0.0]]), np.array([0.0]), incomplete)


def test_head_mount_ignores_the_centerline_offset() -> None:
    """The pivot *is* the programmed position; a centreline offset is a table-mount concept."""
    with_offset = Kinematics(
        rotary_mount="head", rotary_axis="x", centerline_offset=(9.0, 9.0, 9.0), pivot_to_tip=120.0
    )
    pivot = np.array([[0.0, 0.0, 0.0]])
    assert head_tip_coordinates(pivot, np.array([30.0]), with_offset) == pytest.approx(
        head_tip_coordinates(pivot, np.array([30.0]), HEAD)
    )


# --------------------------------------------------------------------------- per step, not per endpoint


def test_a_wrapped_helix_is_not_a_straight_chord() -> None:
    """PLAN.md's "single most likely source of silently wrong output", measured rather than assumed.

    A tool at constant radius while X advances and A sweeps 180° traces a helix on a cylinder. Transformed
    per step it stays at the cylinder's radius throughout; transformed from its endpoints alone it becomes
    a chord that cuts through the cylinder. The two are compared directly.
    """
    radius, steps = 30.0, 180
    degrees = np.linspace(0.0, 180.0, steps + 1)
    tool = np.stack(
        [np.linspace(0.0, 60.0, steps + 1), np.full(steps + 1, radius), np.zeros(steps + 1)], axis=1
    )

    per_step = table_part_coordinates(tool, degrees, TABLE)
    from_axis = np.linalg.norm(per_step[:, 1:], axis=1)
    assert from_axis == pytest.approx(np.full(steps + 1, radius), abs=1e-9), (
        "not a helix on the cylinder"
    )

    # The endpoint-only alternative: transform both ends, then draw a straight line between them.
    ends = table_part_coordinates(tool[[0, -1]], degrees[[0, -1]], TABLE)
    chord_mid = ends.mean(axis=0)
    chord_radius = float(np.linalg.norm(chord_mid[1:]))
    assert chord_radius < radius * 0.5, (
        "the endpoint-only chord should cut deep inside the cylinder; if it does not, this test no "
        "longer demonstrates the failure it exists to demonstrate"
    )


def test_the_transform_uses_the_rotation_recorded_for_each_endpoint() -> None:
    """Per-step correctness comes from `SegmentStore.rot` being per endpoint (T2.3), not from extra work."""
    store = store_of([[0.0, 10.0, 0.0], [10.0, 10.0, 0.0], [20.0, 10.0, 0.0]], [0.0, 90.0, 180.0])
    part = part_coordinates(store, TABLE)
    # Endpoint rotations are 0, 90, 90, 180 across the two segments' (start, end) pairs.
    assert part[0, 0] == pytest.approx([0.0, 10.0, 0.0], abs=1e-9)
    assert part[0, 1] == pytest.approx([10.0, 0.0, -10.0], abs=1e-9)
    assert part[1, 1] == pytest.approx([20.0, -10.0, 0.0], abs=1e-9)


# --------------------------------------------------------------------------- the store contract


def test_the_transform_never_mutates_machine_coordinates() -> None:
    """`lin` is what travel-limit verification reads. Mutating it destroys the ability to verify."""
    store = store_of([[0.0, 10.0, 0.0], [10.0, 10.0, 0.0]], [0.0, 90.0])
    before = store.lin.copy()
    apply_display_transform(store, TABLE)
    assert np.array_equal(store.lin, before)
    assert store.lin_part is not None
    assert not np.array_equal(store.lin_part, store.lin), "the transform did nothing"


def test_lin_part_has_the_same_shape_as_lin() -> None:
    store = store_of([[0.0, 10.0, 0.0], [10.0, 10.0, 0.0], [20.0, 10.0, 0.0]], [0.0, 45.0, 90.0])
    apply_display_transform(store, TABLE)
    assert store.lin_part.shape == store.lin.shape
    assert store.lin_part.dtype == np.float64


def test_an_empty_store_transforms_to_an_empty_array() -> None:
    assert part_coordinates(SegmentStore.empty(), TABLE).shape == (0, 2, 3)


def test_an_unknown_mount_is_refused() -> None:
    store = store_of([[0.0, 10.0, 0.0], [10.0, 10.0, 0.0]], [0.0, 90.0])
    bogus = Kinematics(rotary_mount="turret")
    with pytest.raises(KinematicsError, match="rotary_mount"):
        part_coordinates(store, bogus)


# --------------------------------------------------------------------------- against a real program


def test_a_wrapping_program_lands_on_a_cylinder() -> None:
    """End to end through the simulator: G1 X.. A.. at constant radius must wrap onto a cylinder.

    The radius is checked against the profile's own centreline offset, computed independently of the
    transform, so this is a closed-form check rather than a recorded one.
    """
    profile = load_profile_text(
        """
        [machine]
        units = "mm"
        [tolerance]
        rotary_chord = 0.01
        [kinematics]
        rotary_mount = "table"
        rotary_axis = "x"
        centerline_offset = [0.0, 0.0, 0.0]
        [axes.a]
        type = "rotary"
        max_rapid = 3600.0
        """
    )
    sim, errors = simulate_text("G21 G90 G94\nG0 Y25 Z0\nG1 X60 A180 F600\n", profile)
    assert not list(errors)
    apply_display_transform(sim.store, profile.kinematics)

    wrapped = sim.store.line == 3
    assert wrapped.sum() > 50, "the sweep did not tessellate into steps"
    points = sim.store.lin_part[wrapped].reshape(-1, 3)
    assert np.linalg.norm(points[:, 1:], axis=1) == pytest.approx(
        np.full(len(points), 25.0), abs=1e-6
    )
