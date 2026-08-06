"""Arc and line interpolation tests (T2.3).

PLAN.md § Testing Strategy asks for hypothesis properties here rather than only examples, because
arcs are the component most likely to be subtly wrong and an example test only covers the case you
thought of. The three properties it names are all asserted below over generated inputs:

- interpolated points equidistant from the centre within 1e-6
- tessellation never exceeds `tolerance.arc_chord`
- R ↔ IJK round-trips where expressible

Plus the G18 direction convention, which gets explicit examples because it is a *sign* error — a
property test over radii would pass happily with the arc swept the wrong way.
"""

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from foursight.machine.profile import Kinematics, Tolerances
from foursight.parser.resolver import parse
from foursight.sim.interpolate import (
    PLANES,
    arc_step_count,
    interpolate,
    rotary_step_count,
)

TOL = Tolerances()
KIN = Kinematics()


def path(program: str, start, end, rot_start: float = 0.0, rot_end: float = 0.0, tol=TOL, kin=KIN):
    command = parse(program).commands[-1]
    return interpolate(
        command,
        np.asarray(start, dtype=np.float64),
        np.asarray(end, dtype=np.float64),
        rot_start,
        rot_end,
        tol,
        kin,
    )


def radii(points: np.ndarray, centre, first: int, second: int) -> np.ndarray:
    return np.hypot(points[:, first] - centre[0], points[:, second] - centre[1])


# --------------------------------------------------------------------------- plane frames


def test_every_plane_frame_is_right_handed() -> None:
    """`first × second == +normal` is what makes "G2 decreases the angle" true in all three planes.

    A left-handed frame silently reverses G2 and G3 — the G18 trap PLAN.md warns about.
    """
    basis = {"X": np.array([1.0, 0, 0]), "Y": np.array([0, 1.0, 0]), "Z": np.array([0, 0, 1.0])}
    for code, spec in PLANES.items():
        cross = np.cross(basis[spec.first], basis[spec.second])
        assert np.allclose(cross, basis[spec.normal]), f"G{code} frame is left-handed"


def test_g18_frame_is_zx_not_xz() -> None:
    """Stated explicitly because (X, Z) is the intuitive choice and it is wrong."""
    assert (PLANES["18"].first, PLANES["18"].second) == ("Z", "X")
    assert (PLANES["18"].first_offset, PLANES["18"].second_offset) == ("K", "I")


def test_offsets_pair_with_their_own_axes() -> None:
    expected = {"17": ("I", "J"), "18": ("K", "I"), "19": ("J", "K")}
    for code, spec in PLANES.items():
        assert (spec.first_offset, spec.second_offset) == expected[code]


# --------------------------------------------------------------------------- direction


def test_g17_g2_is_clockwise_viewed_from_plus_z() -> None:
    """From (50,10) about centre (50,20): clockwise reaches (40,20), counter-clockwise (60,20)."""
    cw = path("G21 G17\nG2 X40 Y20 I0 J10\n", (50, 10, 0), (40, 20, 0))
    ccw = path("G21 G17\nG3 X60 Y20 I0 J10\n", (50, 10, 0), (60, 20, 0))
    assert cw.ok and ccw.ok
    assert cw.points[len(cw.points) // 2][0] < 50.0, "CW must bulge towards -X"
    assert ccw.points[len(ccw.points) // 2][0] > 50.0, "CCW must bulge towards +X"


def test_g18_g2_sweeps_the_short_way_in_the_right_handed_frame() -> None:
    """The direction fixture PLAN.md asks for by name.

    Start (X50, Z0), centre (X50, Z-10), end (X40, Z-10) — a quarter turn. Under the correct (Z, X)
    frame this is a 90° sweep; under an (X, Z) frame the same words would sweep 270° the other way.
    """
    result = path("G21 G18\nG2 X40 Z-10 I0 K-10\n", (50, 0, 0), (40, 0, -10))
    assert result.ok
    assert len(result.points) < 30, "a 90 degree arc, not the 270 degree one an XZ frame would give"
    assert np.allclose(radii(result.points, (50.0, -10.0), 0, 2), 10.0)
    assert np.allclose(result.points[:, 1], 0.0), "Y is the normal axis and must not move"
    # Every point stays in the quadrant between start and end.
    assert result.points[:, 0].min() >= 40.0 - 1e-9
    assert result.points[:, 2].max() <= 0.0 + 1e-9


def test_g19_arc_stays_in_the_yz_plane() -> None:
    result = path("G21 G19\nG3 Y40 Z-10 J0 K-10\n", (0, 50, 0), (0, 40, -10))
    assert result.ok
    assert np.allclose(result.points[:, 0], 0.0)
    assert np.allclose(radii(result.points, (50.0, -10.0), 1, 2), 10.0)


# --------------------------------------------------------------------------- R-format


def test_positive_r_selects_the_minor_arc() -> None:
    result = path("G21 G17\nG2 X60 Y20 R15\n", (40, 20, 0), (60, 20, 0))
    assert result.ok
    bulge = result.points[:, 1].max() - 20.0
    assert bulge < 15.0, "a minor arc bulges less than its radius"


def test_negative_r_selects_the_major_arc() -> None:
    result = path("G21 G17\nG2 X60 Y20 R-15\n", (40, 20, 0), (60, 20, 0))
    assert result.ok
    bulge = result.points[:, 1].max() - 20.0
    assert bulge > 15.0, "a major arc sweeps past the centre"


def test_a_semicircle_is_the_same_arc_for_either_sign() -> None:
    """When 2R equals the chord both candidate centres coincide, so the sign cannot matter.

    Worth pinning down: an earlier smoke test used exactly this case and appeared to show the sign
    convention was broken.
    """
    minor = path("G21 G17\nG2 X60 Y20 R10\n", (40, 20, 0), (60, 20, 0))
    major = path("G21 G17\nG2 X60 Y20 R-10\n", (40, 20, 0), (60, 20, 0))
    assert np.allclose(minor.points, major.points)


def test_r_format_with_coincident_endpoints_is_refused() -> None:
    """A full circle is expressible in IJK and not in R; drawing *something* would invent geometry."""
    result = path("G21 G17\nG2 X50 Y20 R10\n", (50, 20, 0), (50, 20, 0))
    assert not result.ok
    assert "full circle" in result.error
    assert len(result.points) == 0


def test_r_format_with_an_impossible_radius_is_refused() -> None:
    result = path("G21 G17\nG2 X60 Y20 R1\n", (40, 20, 0), (60, 20, 0))
    assert not result.ok
    assert "too small" in result.error


def test_an_arc_with_neither_ijk_nor_r_is_refused() -> None:
    result = path("G21 G17\nG2 X60 Y20\n", (40, 20, 0), (60, 20, 0))
    assert not result.ok
    assert "undefined" in result.error


# --------------------------------------------------------------------------- IJK


def test_incremental_ijk_is_relative_to_the_start() -> None:
    result = path("G21 G17 G91.1\nG2 X40 Y20 I0 J10\n", (50, 10, 0), (40, 20, 0))
    assert np.allclose(radii(result.points, (50.0, 20.0), 0, 1), 10.0)


def test_absolute_ijk_is_the_centre_itself() -> None:
    result = path("G21 G17 G90.1\nG2 X40 Y20 I50 J20\n", (50, 10, 0), (40, 20, 0))
    assert np.allclose(radii(result.points, (50.0, 20.0), 0, 1), 10.0)


def test_a_full_circle_needs_start_equal_to_end() -> None:
    """Coincident endpoints mean a full turn, not a zero-length arc."""
    result = path("G21 G17\nG2 X50 Y20 I-10 J0\n", (50, 20, 0), (50, 20, 0))
    assert result.ok
    assert np.allclose(radii(result.points, (40.0, 20.0), 0, 1), 10.0)
    assert len(result.points) > 8
    assert np.allclose(result.points[0], result.points[-1]), "a full circle returns to its start"


def test_endpoints_are_exactly_the_commanded_ones() -> None:
    """The path must start and end on the commanded points, not on recomputed trigonometry.

    The discriminating case is real CAM output, which rounds X/Y and I/J *independently* — so the
    commanded endpoint sits a few microns off the circle the centre defines. A consistent arc would
    not show this: the trigonometry reproduces its endpoint bit-exactly, so an earlier version of
    this test passed even with the endpoint snapping removed.

    Without snapping, each arc would finish at its computed point and leave a gap before the next
    block began at the commanded one.
    """
    start = (10.0, 0.0, 0.0)
    end = (0.0, 9.999, 0.0)  # radius 9.999 against a centre implying 10.0
    result = path("G21 G17\nG3 X0 Y9.999 I-10 J0\n", start, end)
    assert result.ok
    assert result.points[0].tolist() == list(start), (
        "must begin exactly where the previous block ended"
    )
    assert result.points[-1].tolist() == list(end), "must end exactly on the commanded point"


def test_consecutive_arcs_leave_no_gap() -> None:
    """The consequence of the above, stated as the property that actually matters."""
    first_end = (0.0, 9.999, 0.0)
    first = path("G21 G17\nG3 X0 Y9.999 I-10 J0\n", (10.0, 0.0, 0.0), first_end)
    second = path("G21 G17\nG3 X-9.998 Y0 I0 J-9.999\n", first_end, (-9.998, 0.0, 0.0))
    assert first.ok and second.ok
    assert first.points[-1].tolist() == second.points[0].tolist()


# --------------------------------------------------------------------------- helical


def test_the_plane_normal_axis_interpolates_linearly_across_the_sweep() -> None:
    result = path("G21 G17\nG2 X40 Y20 Z-5 I0 J10\n", (50, 10, 0), (40, 20, -5))
    assert result.ok
    z = result.points[:, 2]
    assert z[0] == 0.0 and z[-1] == -5.0
    assert np.allclose(np.diff(z), np.diff(z)[0]), "Z must advance in equal increments"
    assert np.allclose(radii(result.points, (50.0, 20.0), 0, 1), 10.0), "still circular in plane"


def test_rotary_interpolates_alongside_a_helical_arc() -> None:
    result = path("G21 G17\nG2 X40 Y20 Z-5 A45 I0 J10\n", (50, 10, 0), (40, 20, -5), 0.0, 45.0)
    assert result.ok
    assert result.rotations[0] == 0.0
    assert result.rotations[-1] == pytest.approx(45.0)
    assert len(result.rotations) == len(result.points)


def test_rotations_are_a_separate_array_from_points() -> None:
    """Never an (M, 4): no norm can then be taken across millimetres and degrees."""
    result = path("G21 G17\nG1 X10 A90\n", (0, 0, 0), (10, 0, 0), 0.0, 90.0)
    assert result.points.shape[1] == 3
    assert result.rotations.ndim == 1


# --------------------------------------------------------------------------- straight moves


def test_a_plain_line_is_a_single_segment() -> None:
    result = path("G21 G17\nG1 X10\n", (0, 0, 0), (10, 0, 0))
    assert len(result.points) == 2


def test_a_line_with_rotary_motion_is_subdivided() -> None:
    """Under a rotary table the *part* sees an arc; endpoint-only would draw it as a chord.

    This is why per-step interpolation exists from M2 rather than being retrofitted in M4.
    """
    kin = Kinematics(rotary_axis="x", centerline_offset=(0.0, 0.0, -50.0))
    result = path("G21 G17\nG1 X10 A90\n", (0, 0, 0), (10, 0, 0), 0.0, 90.0, kin=kin)
    assert len(result.points) > 2
    assert result.rotations[-1] == pytest.approx(90.0)


def test_a_path_on_the_rotary_centerline_needs_no_subdivision() -> None:
    """Zero radius sweeps through no distance, so there is no chord error to bound."""
    kin = Kinematics(rotary_axis="x", centerline_offset=(0.0, 0.0, 0.0))
    result = path("G21 G17\nG1 X10 A90\n", (0, 0, 0), (10, 0, 0), 0.0, 90.0, kin=kin)
    assert len(result.points) == 2


# --------------------------------------------------------------------------- step counts


def test_step_count_scales_with_radius_not_a_fixed_number() -> None:
    """PLAN.md: a 500 mm arc and a 0.5 mm arc need wildly different counts."""
    counts = [arc_step_count(r, 2 * math.pi, 0.01) for r in (0.5, 5.0, 50.0, 500.0)]
    assert counts == sorted(counts)
    assert counts[0] < 20 < counts[-1]


def test_a_radius_below_the_tolerance_needs_one_step() -> None:
    assert arc_step_count(0.001, 2 * math.pi, 0.01) == 1


def test_tighter_tolerance_means_more_steps() -> None:
    assert arc_step_count(50.0, math.pi, 0.001) > arc_step_count(50.0, math.pi, 0.1)


def test_rotary_step_count_uses_the_distance_from_the_centerline() -> None:
    kin = Kinematics(rotary_axis="x", centerline_offset=(0.0, 0.0, 0.0))
    near = rotary_step_count(90.0, np.array([0.0, 0, 1.0]), np.array([0.0, 0, 1.0]), kin, 0.01)
    far = rotary_step_count(90.0, np.array([0.0, 0, 100.0]), np.array([0.0, 0, 100.0]), kin, 0.01)
    assert far > near


# --------------------------------------------------------------------------- properties


ARC_RADII = st.floats(min_value=0.5, max_value=500.0, allow_nan=False, allow_infinity=False)
SWEEPS = st.floats(min_value=0.05, max_value=2 * math.pi, allow_nan=False)
CHORDS = st.sampled_from([0.001, 0.005, 0.01, 0.05, 0.2])


@given(radius=ARC_RADII, sweep=SWEEPS, chord=CHORDS)
@settings(max_examples=200, deadline=None)
def test_property_chord_deviation_never_exceeds_the_tolerance(
    radius: float, sweep: float, chord: float
) -> None:
    """The sagitta of every step must stay inside `tolerance.arc_chord`.

    This is the property that makes adaptive tessellation meaningful; a fixed step count would fail
    it at large radii and waste segments at small ones.
    """
    steps = arc_step_count(radius, sweep, chord)
    sagitta = radius * (1.0 - math.cos((sweep / steps) / 2.0))
    assert sagitta <= chord + 1e-9, f"sagitta {sagitta} exceeds tolerance {chord}"


@given(
    radius=st.floats(min_value=1.0, max_value=200.0),
    sweep_degrees=st.floats(min_value=5.0, max_value=350.0),
    clockwise=st.booleans(),
)
@settings(max_examples=150, deadline=None)
def test_property_interpolated_points_are_equidistant_from_the_centre(
    radius: float, sweep_degrees: float, clockwise: bool
) -> None:
    """Every point on a circular arc is exactly one radius from the centre, within 1e-6."""
    sweep = math.radians(sweep_degrees)
    centre = np.array([0.0, 0.0])
    start = np.array([radius, 0.0, 0.0])
    angle = -sweep if clockwise else sweep
    end = np.array([radius * math.cos(angle), radius * math.sin(angle), 0.0])
    code = "2" if clockwise else "3"
    program = f"G21 G17\nG{code} X{end[0]:.10f} Y{end[1]:.10f} I{-radius:.10f} J0\n"
    result = path(program, start, end)
    assert result.ok, result.error
    distances = radii(result.points, centre, 0, 1)
    assert np.allclose(distances, radius, atol=1e-6), (
        f"radius drift {np.abs(distances - radius).max()}"
    )


@given(
    radius=st.floats(min_value=2.0, max_value=100.0),
    sweep_degrees=st.floats(min_value=10.0, max_value=170.0),
    clockwise=st.booleans(),
)
@settings(max_examples=150, deadline=None)
def test_property_r_and_ijk_agree_where_both_are_expressible(
    radius: float, sweep_degrees: float, clockwise: bool
) -> None:
    """R ↔ IJK round-trip. Restricted to minor arcs, which is exactly where positive R applies.

    A full circle is deliberately excluded: it is expressible in IJK and not in R, which is the whole
    reason the IJK→R fix must refuse on it.
    """
    sweep = math.radians(sweep_degrees)
    start = np.array([radius, 0.0, 0.0])
    angle = -sweep if clockwise else sweep
    end = np.array([radius * math.cos(angle), radius * math.sin(angle), 0.0])
    code = "2" if clockwise else "3"

    via_ijk = path(
        f"G21 G17\nG{code} X{end[0]:.10f} Y{end[1]:.10f} I{-radius:.10f} J0\n", start, end
    )
    via_r = path(f"G21 G17\nG{code} X{end[0]:.10f} Y{end[1]:.10f} R{radius:.10f}\n", start, end)
    assert via_ijk.ok and via_r.ok, (via_ijk.error, via_r.error)
    assert len(via_ijk.points) == len(via_r.points)
    assert np.allclose(via_ijk.points, via_r.points, atol=1e-6)


@given(
    start_a=st.floats(min_value=-360.0, max_value=360.0),
    end_a=st.floats(min_value=-360.0, max_value=360.0),
)
@settings(max_examples=100, deadline=None)
def test_property_rotation_endpoints_are_always_honoured(start_a: float, end_a: float) -> None:
    """However a move is subdivided, it must begin and end at the commanded A."""
    kin = Kinematics(rotary_axis="x", centerline_offset=(0.0, 0.0, -25.0))
    result = path("G21 G17\nG1 X10 A0\n", (0, 0, 0), (10, 0, 0), start_a, end_a, kin=kin)
    assert result.ok
    assert result.rotations[0] == pytest.approx(start_a)
    assert result.rotations[-1] == pytest.approx(end_a)
    assert len(result.rotations) == len(result.points)


# --------------------------------------------------------------------------- fixtures


def test_the_g18_fixture_interpolates_cleanly(baseline_text: str) -> None:
    """Uses the committed fixture rather than an inline program, so the two cannot drift."""
    from conftest import fixture_text

    commands = parse(fixture_text("arc_g18_direction.nc")).commands
    arcs = [c for c in commands if c.motion in {"2", "3"}]
    assert arcs, "fixture has no arcs"
    for command in arcs:
        assert command.modal_snapshot.plane == "18"
        assert "K" in command.words and "J" not in command.words
