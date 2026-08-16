"""The solid carve, checked against closed-form expectations rather than golden hashes.

A golden hash would notice that the carve changed and say nothing about whether it is *right*, and
"right" is the whole question for a picture that looks as authoritative as a shaded solid does. So every
test here asserts a number the geometry can be worked out by hand: a 6 mm slot is 6 mm wide, a ball nose
leaves the profile of a sphere, a full revolution at constant Z leaves a constant radius.
"""

import numpy as np
import pytest

from foursight.machine.profile import (
    Kinematics,
    MachineProfile,
    StockBox,
    StockCylinder,
    Tool,
)
from foursight.sim.segments import Kind, SegmentBuilder, SegmentStore
from foursight.sim.solid import SolidError, bottom_offset, carve

# A 100 x 80 blank, 20 mm thick, top face at Z0 — the shape the shipped profile documents.
BLANK = StockBox(min=(0.0, 0.0, -20.0), max=(100.0, 80.0, 0.0))
# A 6 mm flat end mill, so a slot is 6 mm wide and every wall lands 3 mm off the centreline.
SIX_MM_FLAT = Tool(diameter=6.0)


def store_from(moves, *, rotations=None) -> SegmentStore:
    """Build a store from ``(start, end, kind)`` triples, one segment each."""
    builder = SegmentBuilder()
    for index, (start, end, kind) in enumerate(moves):
        rot_start, rot_end = (0.0, 0.0) if rotations is None else rotations[index]
        builder.add_segment(
            np.asarray(start, dtype=np.float64),
            np.asarray(end, dtype=np.float64),
            kind,
            index + 1,
            rot_start=rot_start,
            rot_end=rot_end,
        )
    return builder.finalize()


def profile_with(stock=BLANK, tool=SIX_MM_FLAT, **kinematics) -> MachineProfile:
    return MachineProfile(stock=stock, tool=tool, kinematics=Kinematics(**kinematics))


def height_at(field, x: float, y: float) -> float:
    """The carved Z nearest world ``(x, y)``."""
    iu = int(round((x - field.origin[0]) / field.cell[0]))
    iv = int(round((y - field.origin[1]) / field.cell[1]))
    return float(field.height[iu, iv])


# ------------------------------------------------------------------------- the cutter's own shape


def test_a_flat_tool_reaches_its_tip_depth_everywhere_inside_the_radius():
    tool = Tool(diameter=6.0, shape="flat")
    distances = np.linspace(0.0, tool.radius, 25)
    assert np.allclose(bottom_offset(distances, tool), 0.0)


def test_a_ball_tool_rises_away_from_the_tip_as_a_sphere():
    tool = Tool(diameter=6.0, shape="ball")
    distances = np.linspace(0.0, tool.radius, 25)
    expected = tool.radius - np.sqrt(tool.radius**2 - distances**2)
    assert np.allclose(bottom_offset(distances, tool), expected)


def test_a_ball_tool_never_produces_a_nan_just_past_its_radius():
    """One NaN in a min-reduction poisons every cell it reaches, and nothing about it looks like an error."""
    tool = Tool(diameter=6.0, shape="ball")
    just_past = np.array([tool.radius + 1e-12, tool.radius * 2, 1e6])
    assert np.all(np.isfinite(bottom_offset(just_past, tool)))


# ---------------------------------------------------------------------------- a single straight cut


def test_a_straight_cut_leaves_a_slot_of_exactly_the_tool_diameter():
    """The load-bearing property: a 6 mm cutter removes a 6 mm slot, not 5 and not 7."""
    store = store_from([((20.0, 40.0, -2.0), (80.0, 40.0, -2.0), Kind.FEED)])
    field = carve(store, profile_with())

    # Floor at the commanded depth, along the middle of the slot.
    assert height_at(field, 50.0, 40.0) == pytest.approx(-2.0, abs=1e-9)
    # Untouched a clear tool radius away.
    assert height_at(field, 50.0, 40.0 + 5.0) == pytest.approx(0.0, abs=1e-9)

    # The wall sits at 3 mm from the centreline, within the resolution of one cell.
    cell = field.cell[1]
    assert height_at(field, 50.0, 40.0 + 3.0 - cell) == pytest.approx(-2.0, abs=1e-9)
    assert height_at(field, 50.0, 40.0 + 3.0 + cell) == pytest.approx(0.0, abs=1e-9)


def test_the_slot_extends_a_tool_radius_beyond_each_end_of_the_move():
    """The cutter is a disc, not a point: it removes material past where its axis stopped."""
    store = store_from([((20.0, 40.0, -2.0), (80.0, 40.0, -2.0), Kind.FEED)])
    field = carve(store, profile_with())
    cell = field.cell[0]

    assert height_at(field, 80.0 + 3.0 - cell, 40.0) == pytest.approx(-2.0, abs=1e-9)
    assert height_at(field, 80.0 + 3.0 + cell, 40.0) == pytest.approx(0.0, abs=1e-9)


def test_a_ball_nose_leaves_the_cross_section_of_a_sphere():
    store = store_from([((20.0, 40.0, -2.0), (80.0, 40.0, -2.0), Kind.FEED)])
    field = carve(store, profile_with(tool=Tool(diameter=6.0, shape="ball")))

    for offset in (0.0, 1.0, 2.0, 2.5):
        expected = -2.0 + (3.0 - np.sqrt(9.0 - offset**2))
        assert height_at(field, 50.0, 40.0 + offset) == pytest.approx(expected, abs=field.cell[1])


def test_a_diagonal_move_leaves_no_ridges_of_uncut_material():
    """Undersampling the axis path shows up here first, as cells the cutter should have reached."""
    store = store_from([((10.0, 10.0, -1.0), (90.0, 70.0, -1.0), Kind.FEED)])
    field = carve(store, profile_with())

    for fraction in np.linspace(0.05, 0.95, 40):
        x = 10.0 + fraction * 80.0
        y = 10.0 + fraction * 60.0
        assert height_at(field, x, y) == pytest.approx(-1.0, abs=1e-9)


# ----------------------------------------------------------------------- what is allowed to cut


def test_rapids_never_cut():
    store = store_from([((20.0, 40.0, -5.0), (80.0, 40.0, -5.0), Kind.RAPID)])
    field = carve(store, profile_with())
    assert not field.carved
    assert np.all(field.height == 0.0)


def test_unverified_spans_never_cut_and_the_field_says_so():
    """A cutter-compensated span is the programmed centreline, not where the tool goes."""
    store = store_from([((20.0, 40.0, -2.0), (80.0, 40.0, -2.0), Kind.FEED)])
    untrusted = np.array([True])
    field = carve(store, profile_with(), untrusted=untrusted)

    assert not field.carved
    assert any("unverified" in note for note in field.notes)


def test_a_wrong_length_untrusted_mask_is_refused_rather_than_broadcast():
    store = store_from([((20.0, 40.0, -2.0), (80.0, 40.0, -2.0), Kind.FEED)])
    with pytest.raises(SolidError, match="one entry per segment"):
        carve(store, profile_with(), untrusted=np.array([True, False, True]))


def test_more_than_one_tool_number_becomes_a_note():
    store = store_from([((20.0, 40.0, -2.0), (80.0, 40.0, -2.0), Kind.FEED)])
    field = carve(store, profile_with(), tool_numbers=(1, 1, 4))
    assert any("T1" in note and "T4" in note for note in field.notes)


def test_one_tool_number_is_not_a_note():
    store = store_from([((20.0, 40.0, -2.0), (80.0, 40.0, -2.0), Kind.FEED)])
    field = carve(store, profile_with(), tool_numbers=(3, 3))
    assert field.notes == ()


# --------------------------------------------------------------------------------- the refusals


def test_no_tool_is_refused_rather_than_defaulted():
    store = store_from([((20.0, 40.0, -2.0), (80.0, 40.0, -2.0), Kind.FEED)])
    with pytest.raises(SolidError, match="no \\[tool\\]"):
        carve(store, profile_with(tool=None))


def test_no_stock_is_refused_rather_than_invented():
    store = store_from([((20.0, 40.0, -2.0), (80.0, 40.0, -2.0), Kind.FEED)])
    with pytest.raises(SolidError, match="no \\[stock\\]"):
        carve(store, profile_with(stock=None))


def test_a_box_is_refused_once_a_table_mounted_program_moves_a():
    """A fixed box has stopped describing stock that turns with the part."""
    store = store_from(
        [((20.0, 40.0, -2.0), (80.0, 40.0, -2.0), Kind.FEED)], rotations=[(0.0, 90.0)]
    )
    with pytest.raises(SolidError, match="cannot be carved once the program moves A"):
        carve(store, profile_with(rotary_mount="table"))


def test_the_deepest_cut_is_clamped_to_the_bottom_of_the_blank():
    """A tool driven below the blank leaves a floor at the blank's underside, not a bottomless hole."""
    store = store_from([((20.0, 40.0, -500.0), (80.0, 40.0, -500.0), Kind.FEED)])
    field = carve(store, profile_with())
    assert height_at(field, 50.0, 40.0) == pytest.approx(-20.0, abs=1e-9)
    assert field.height.min() >= -20.0


# -------------------------------------------------------------------------- resolution behaviour


def test_an_absurdly_large_tool_coarsens_the_grid_instead_of_stalling():
    """The kernel grows with the square of the radius in cells, so the cell size has to absorb it."""
    store = store_from([((20.0, 40.0, -2.0), (80.0, 40.0, -2.0), Kind.FEED)])
    field = carve(store, profile_with(tool=Tool(diameter=90.0)))

    radius_in_cells = 45.0 / field.cell[0]
    assert radius_in_cells <= 13.0
    assert field.height.size < 10_000


def test_a_facing_pass_that_runs_off_the_edge_still_clears_the_corner():
    """Dropping axis positions outside the blank leaves a rim that looks like a real feature."""
    store = store_from([((-20.0, 40.0, -1.0), (120.0, 40.0, -1.0), Kind.FEED)])
    field = carve(store, profile_with())
    assert height_at(field, 0.0, 40.0) == pytest.approx(-1.0, abs=1e-9)
    assert height_at(field, 100.0, 40.0) == pytest.approx(-1.0, abs=1e-9)


# ------------------------------------------------------------------------------ cylinder carving


CYLINDER = StockCylinder(diameter=52.0, length=150.0, axis_min=0.0)

#: A store is a polyline, so a circle in part coordinates is a ring of chords and the carved radius dips
#: by the sagitta between samples. At 720 steps and r = 24 that is r*(1 - cos(0.25 deg)) = 2.3e-4 mm.
#: Asserting tighter than this would be asserting that tessellation does not happen; asserting much
#: looser would stop noticing a genuinely wrong frame. `tolerance.rotary_chord` defaults to 0.01 mm, so
#: this is the real budget with an order of magnitude to spare.
CHORD_TOLERANCE_MM = 1e-3


def rotary_profile(**overrides) -> MachineProfile:
    kinematics = {
        "rotary_mount": "table",
        "rotary_axis": "y",
        "centerline_offset": (0.0, 0.0, -26.0),
    }
    kinematics.update(overrides)
    return MachineProfile(
        stock=CYLINDER, tool=Tool(diameter=6.0), kinematics=Kinematics(**kinematics)
    )


def wrapped_store(depth: float, *, turns: int = 1, steps: int = 720) -> SegmentStore:
    """One full revolution at a constant machine Z, tessellated the way `sim.interpolate` would.

    ``rotary_axis = "y"``, so Y is the axial coordinate and the tool sits at the middle of the 150 mm
    blank. X = 0 puts it over top dead centre, where its distance from the centreline is its depth.
    """
    builder = SegmentBuilder()
    angles = np.linspace(0.0, 360.0 * turns, steps + 1)
    for index in range(steps):
        point = np.array([0.0, 75.0, depth], dtype=np.float64)
        builder.add_segment(
            point, point.copy(), Kind.FEED, 1, rot_start=angles[index], rot_end=angles[index + 1]
        )
    return builder.finalize()


def test_a_full_revolution_at_constant_z_leaves_a_constant_radius():
    """Rotation invariance is what the cylinder frame exists for, and a wrong frame breaks it first."""
    from foursight.machine.kinematics import apply_display_transform

    profile = rotary_profile()
    # The centreline sits at Z = -26 with the blank 52 mm across, so its surface is at machine Z = 0.
    # Cutting at Z = -2 leaves a 24 mm radius everywhere.
    store = wrapped_store(-2.0)
    apply_display_transform(store, profile.kinematics)
    field = carve(store, profile)

    assert field.kind == "cylinder"
    assert field.part_coordinates and field.wraps_u

    middle = field.height[:, field.height.shape[1] // 2]
    assert np.allclose(middle, 24.0, rtol=0.0, atol=CHORD_TOLERANCE_MM)


def test_the_cutter_spreads_across_the_wrap_seam():
    """A cut at A = 0 must remove material on **both** sides of the 360/0 boundary.

    Deliberately a single plunge rather than a full revolution: a revolution carves every cell directly,
    so it would pass with the seam left as +inf and prove nothing. Here the only way angle 359 gets
    touched is if the erosion sees cell 0 as its neighbour.
    """
    from foursight.machine.kinematics import apply_display_transform

    profile = rotary_profile()
    builder = SegmentBuilder()
    point = np.array([0.0, 75.0, -2.0], dtype=np.float64)
    builder.add_segment(point, point.copy(), Kind.FEED, 1)
    store = builder.finalize()
    apply_display_transform(store, profile.kinematics)
    field = carve(store, profile)

    middle = field.height.shape[1] // 2
    # The tool is 6 mm across on a 52 mm blank, so it reaches about +/- 6.6 degrees, or +/- 10 cells.
    assert field.height[0, middle] == pytest.approx(24.0, abs=CHORD_TOLERANCE_MM)
    assert field.height[5, middle] < CYLINDER.radius
    assert field.height[-5, middle] < CYLINDER.radius, "the seam left an uncut stripe"


def test_a_cylinder_carve_without_part_coordinates_is_refused():
    from foursight.machine.kinematics import KinematicsError

    store = wrapped_store(-2.0)
    with pytest.raises(KinematicsError, match="part coordinates"):
        carve(store, rotary_profile())


def test_a_cylinder_radius_is_never_negative():
    from foursight.machine.kinematics import apply_display_transform

    profile = rotary_profile()
    store = wrapped_store(-100.0)  # straight through the centreline and out the far side
    apply_display_transform(store, profile.kinematics)
    field = carve(store, profile)
    assert field.height.min() >= 0.0


def test_an_untouched_cylinder_keeps_the_blank_radius():
    from foursight.machine.kinematics import apply_display_transform

    profile = rotary_profile()
    store = wrapped_store(50.0)  # well clear of the blank
    apply_display_transform(store, profile.kinematics)
    field = carve(store, profile)

    assert not field.carved
    assert np.allclose(field.height, 26.0)


# ---------------------------------------------------------------------------- back out to world


def test_box_points_place_the_grid_where_the_blank_is():
    store = store_from([((20.0, 40.0, -2.0), (80.0, 40.0, -2.0), Kind.FEED)])
    field = carve(store, profile_with())
    points = field.points()

    assert points.shape == (*field.height.shape, 3)
    assert points[0, 0, 0] == pytest.approx(0.0)
    assert points[0, 0, 1] == pytest.approx(0.0)
    assert points[-1, -1, 0] >= 100.0 - field.cell[0]
    assert np.allclose(points[..., 2], field.height)


def test_cylinder_points_lie_on_the_carved_radius_about_the_centreline():
    from foursight.machine.kinematics import apply_display_transform

    profile = rotary_profile()
    store = wrapped_store(-2.0)
    apply_display_transform(store, profile.kinematics)
    field = carve(store, profile)
    points = field.points()

    # rotary_axis = "y", so X and Z are the radial pair about the centreline at (0, 0, -26).
    radius = np.hypot(points[..., 0] - 0.0, points[..., 2] - (-26.0))
    assert np.allclose(radius, field.height, atol=1e-9)
    # And the axial coordinate spans the blank.
    assert points[..., 1].min() == pytest.approx(0.0)
    assert points[..., 1].max() >= 150.0 - field.cell[1]
