"""Geometry check tests (T1.9).

Arc geometry is the component PLAN.md singles out as most likely to be subtly wrong, so the arc
tests check against hand-computed values rather than against whatever the code produces.

The travel-limit tests pay particular attention to the `error` → `warning` downgrade on an unknown
work offset, which is the one place in the verifier where severity depends on profile *data* rather
than on the kind of problem.
"""

import math

import pytest

from conftest import DEFAULT_PROFILE_PATH, diagnose, fixture_text
from foursight.machine.profile import load_profile, load_profile_text
from foursight.machine.state import Position
from foursight.parser.resolver import parse
from foursight.verify.checks.geometry import arc_geometry
from foursight.verify.report import Severity

PREAMBLE = "G21 G90 G17 G94 G54\nG0 Z25.0\nT1 M6\nS8000 M3\n"
POSTAMBLE = "G0 Z25.0\nM5\nM30\n"


@pytest.fixture(scope="module")
def profile():
    return load_profile(DEFAULT_PROFILE_PATH)


def check(text: str, profile):
    return diagnose(text, profile)


def of(diagnostics, rule_id: str):
    return [d for d in diagnostics if d.rule_id == rule_id]


def only(diagnostics, rule_id: str):
    found = of(diagnostics, rule_id)
    assert found, f"{rule_id} did not fire; got {sorted({d.rule_id for d in diagnostics})}"
    return found


def program(body: str) -> str:
    return PREAMBLE + body + POSTAMBLE


# --------------------------------------------------------------------------- arc geometry itself


def test_incremental_ijk_centre_is_relative_to_the_start() -> None:
    """G91.1 is the default: I,J are offsets from the start point."""
    commands = parse("G21 G17 G91.1\nG1 X50 Y10 F100\nG2 X40 Y20 I0 J10\n").commands
    arc = arc_geometry(commands[-1], Position(x=50.0, y=10.0))
    assert arc.centre == (50.0, 20.0)
    assert arc.radius_start == pytest.approx(10.0)
    assert arc.radius_end == pytest.approx(10.0)


def test_absolute_ijk_centre_is_taken_as_given() -> None:
    """G90.1 makes I,J the centre's absolute coordinates — a different arc from the same numbers."""
    commands = parse("G21 G17 G90.1\nG1 X50 Y10 F100\nG2 X40 Y20 I50 J20\n").commands
    arc = arc_geometry(commands[-1], Position(x=50.0, y=10.0))
    assert arc.centre == (50.0, 20.0)


def test_g18_uses_i_and_k_not_j() -> None:
    """G18's IJK mapping is I,K. Reading J here would silently produce a different arc."""
    commands = parse("G21 G18\nG1 X50 Z0 F100\nG2 X40 Z-10 I0 K-10\n").commands
    arc = arc_geometry(commands[-1], Position(x=50.0, z=0.0))
    assert arc.axes == ("X", "Z")
    assert arc.centre == (50.0, -10.0)
    assert arc.radius_start == pytest.approx(10.0)


def test_g19_uses_j_and_k() -> None:
    commands = parse("G21 G19\nG1 Y50 Z0 F100\nG3 Y40 Z-10 J0 K-10\n").commands
    arc = arc_geometry(commands[-1], Position(y=50.0, z=0.0))
    assert arc.axes == ("Y", "Z")
    assert arc.centre == (50.0, -10.0)


def test_a_helical_arc_is_measured_in_plane_only() -> None:
    """Z motion during a G17 arc must not enter the radius comparison."""
    commands = parse("G21 G17\nG1 X50 Y10 Z0 F100\nG2 X40 Y20 Z-5 I0 J10\n").commands
    arc = arc_geometry(commands[-1], Position(x=50.0, y=10.0, z=0.0))
    assert arc.radius_start == pytest.approx(arc.radius_end)


def test_an_omitted_axis_word_means_that_coordinate_does_not_move() -> None:
    """A full circle omits both endpoint words; start and end coincide, which IJK allows."""
    commands = parse("G21 G17\nG1 X50 Y20 F100\nG2 I-10 J0\n").commands
    arc = arc_geometry(commands[-1], Position(x=50.0, y=20.0))
    assert arc.start == arc.end == (50.0, 20.0)
    assert arc.centre == (40.0, 20.0)


def test_r_format_arcs_are_not_treated_as_ijk() -> None:
    commands = parse("G21 G17\nG1 X40 Y20 F100\nG2 X60 Y20 R10\n").commands
    assert arc_geometry(commands[-1], Position(x=40.0, y=20.0)) is None


def test_arc_geometry_needs_an_established_start() -> None:
    """No start point, no radius — and therefore no claim about the arc."""
    commands = parse("G21 G17\nG2 X40 Y20 I0 J10\n").commands
    assert arc_geometry(commands[-1], Position()) is None


# --------------------------------------------------------------------------- radius mismatch


def test_radius_mismatch_is_an_error(profile) -> None:
    """Start 10 mm from the centre, end 12 mm: the three do not describe one arc."""
    found = only(
        check(program("G1 X50 Y10 F100\nG2 X40 Y20 I0 J12\n"), profile),
        "geometry.arc-radius-mismatch",
    )
    assert found[0].severity is Severity.ERROR
    assert "fix.recompute-arc-centre" in found[0].fix_ids


def test_a_clean_arc_reports_nothing(profile) -> None:
    assert not of(
        check(program("G1 X50 Y10 F100\nG2 X40 Y20 I0 J10\n"), profile),
        "geometry.arc-radius-mismatch",
    )


def test_mismatch_within_tolerance_is_accepted(profile) -> None:
    """Real CAM output is not exact; the profile decides how much slop is acceptable."""
    body = "G1 X50 Y10 F100\nG2 X40.002 Y20 I0 J10\n"
    assert not of(check(program(body), profile), "geometry.arc-radius-mismatch")


def test_the_tolerance_comes_from_the_profile_not_a_constant() -> None:
    """PLAN.md: the check and the T5.2 fix must not each hard-code this.

    The same arc must be acceptable under a loose profile and rejected under a strict one — if a
    constant were baked in, both would give the same answer.
    """
    body = program("G1 X50 Y10 F100\nG2 X40 Y20 I0 J10.5\n")
    strict = load_profile_text('[machine]\nunits="mm"\n[tolerance]\narc_radius_mismatch = 0.001\n')
    loose = load_profile_text('[machine]\nunits="mm"\n[tolerance]\narc_radius_mismatch = 5.0\n')
    assert of(diagnose(body, strict), "geometry.arc-radius-mismatch")
    assert not of(diagnose(body, loose), "geometry.arc-radius-mismatch")


def test_mismatch_is_measured_in_declared_units() -> None:
    inch = load_profile_text('[machine]\nunits="mm"\n[tolerance]\narc_radius_mismatch = 0.005\n')
    body = "G20 G90 G17 G54\nS8000 M3\nG1 X2 Y0.4 F10\nG2 X1.6 Y0.8 I0 J0.5\nM30\n"
    found = only(diagnose(body, inch), "geometry.arc-radius-mismatch")
    assert " in" in found[0].message
    assert "mm" not in found[0].message


# --------------------------------------------------------------------------- R-format validity


def test_r_arc_with_coincident_endpoints_is_an_error(profile) -> None:
    """R gives no way to say which way round; a full circle must be written in IJK."""
    found = only(
        check(program("G1 X50 Y10 F100\nG2 X50 Y10 R10\n"), profile), "geometry.arc-r-invalid"
    )
    assert found[0].severity is Severity.ERROR
    assert "full circle" in found[0].message


def test_r_arc_with_an_impossible_radius_is_an_error(profile) -> None:
    """No circle of radius 1 touches two points 20 apart."""
    found = only(
        check(program("G1 X40 Y20 F100\nG2 X60 Y20 R1\n"), profile), "geometry.arc-r-invalid"
    )
    assert "too small to span" in found[0].message


def test_r_exactly_half_the_chord_is_accepted(profile) -> None:
    """A semicircle is the limiting valid case, so it must not be rejected."""
    assert not of(
        check(program("G1 X40 Y20 F100\nG2 X60 Y20 R10\n"), profile), "geometry.arc-r-invalid"
    )


@pytest.mark.parametrize("radius", ["R10", "R-10"])
def test_both_r_signs_are_valid_geometry(radius: str, profile) -> None:
    """The sign picks which arc, not whether one exists."""
    body = f"G1 X40 Y20 F100\nG2 X60 Y20 {radius}\n"
    assert not of(check(program(body), profile), "geometry.arc-r-invalid")


def test_an_ijk_full_circle_is_not_flagged(profile) -> None:
    """Expressible in IJK precisely because the centre disambiguates it."""
    body = "G1 X50 Y20 F100\nG2 X50 Y20 I-10 J0\n"
    found = check(program(body), profile)
    assert not of(found, "geometry.arc-r-invalid")
    assert not of(found, "geometry.arc-radius-mismatch")


# --------------------------------------------------------------------------- travel limits


def test_travel_limit_exceeded_is_an_error_when_the_offset_is_known(profile) -> None:
    found = only(check(program("G1 X500 F100\n"), profile), "geometry.axis-travel-exceeded")
    assert found[0].severity is Severity.ERROR
    assert "400 mm" in found[0].message


def test_travel_limit_downgrades_to_a_warning_when_the_offset_is_unknown() -> None:
    """PLAN.md: without the work offset the machine position is a guess, and a hard error on a
    guess is worse than a warning that says so."""
    no_offsets = load_profile_text('[machine]\nunits="mm"\n[axes.x]\nmin=0.0\nmax=400.0\n')
    body = "G21 G90 G54\nS8000 M3\nG1 X500 F100\nM30\n"
    found = only(diagnose(body, no_offsets), "geometry.axis-travel-exceeded")
    assert found[0].severity is Severity.WARNING
    assert "unconfirmed" in found[0].message


def test_a_known_offset_shifts_where_the_limit_bites() -> None:
    """X380 is inside a 400 limit until a +50 work offset puts the machine at 430."""
    shifted = load_profile_text(
        '[machine]\nunits="mm"\n[axes.x]\nmin=0.0\nmax=400.0\n'
        "[offsets]\ng54 = [50.0, 0.0, 0.0, 0.0]\n"
    )
    body = "G21 G90 G54\nS8000 M3\nG1 X380 F100\nM30\n"
    found = only(diagnose(body, shifted), "geometry.axis-travel-exceeded")
    assert found[0].severity is Severity.ERROR
    assert "430 mm" in found[0].message


def test_below_minimum_is_also_caught(profile) -> None:
    found = only(check(program("G1 X-10 F100\n"), profile), "geometry.axis-travel-exceeded")
    assert "minimum" in found[0].message


def test_an_axis_without_limits_is_not_checked() -> None:
    """Absence means unknown; no rule invents a bound."""
    bare = load_profile_text('[machine]\nunits="mm"\n')
    body = "G21 G90 G54\nS8000 M3\nG1 X99999 F100\nM30\n"
    assert not of(diagnose(body, bare), "geometry.axis-travel-exceeded")


def test_the_linear_rule_does_not_also_report_rotary(profile) -> None:
    """Rotary belongs to RotaryTravelExceeded; both reporting would double every violation."""
    no_wrap = load_profile_text(
        '[machine]\nunits="mm"\n[axes.a]\ntype="rotary"\nwrap=false\nmin=-90.0\nmax=90.0\n'
    )
    body = "G21 G90 G54\nS8000 M3\nG1 A200 F100\nM30\n"
    found = diagnose(body, no_wrap)
    assert not of(found, "geometry.axis-travel-exceeded")
    assert of(found, "geometry.rotary-travel-exceeded")


# --------------------------------------------------------------------------- rotary


def test_a_wrapping_axis_has_no_travel_limit(profile) -> None:
    """The default profile wraps A, so 3600 degrees is legitimate multi-turn motion."""
    found = check(program("G1 A3600 F1800\n"), profile)
    assert not of(found, "geometry.rotary-travel-exceeded")
    assert not of(found, "geometry.axis-travel-exceeded")


def test_a_non_wrapping_axis_enforces_min_and_max() -> None:
    no_wrap = load_profile_text(
        '[machine]\nunits="mm"\n[axes.a]\ntype="rotary"\nwrap=false\nmin=-90.0\nmax=90.0\n'
    )
    body = "G21 G90 G54\nS8000 M3\nG1 A200 F100\nM30\n"
    found = only(diagnose(body, no_wrap), "geometry.rotary-travel-exceeded")
    assert found[0].severity is Severity.ERROR
    assert "does not wrap" in found[0].message
    assert "deg" in found[0].message, "rotary values are degrees, never millimetres"


def test_rotary_limits_stay_an_error_when_the_offset_is_unknown() -> None:
    """Deliberately unlike the linear rule.

    PLAN.md attaches the error-to-warning downgrade to the linear travel bullet only, and a non-zero
    rotary work offset is rare. The severity follows the plan; the assumption is stated in the
    message so the uncertainty is still visible.
    """
    no_wrap = load_profile_text(
        '[machine]\nunits="mm"\n[axes.a]\ntype="rotary"\nwrap=false\nmin=-90.0\nmax=90.0\n'
    )
    body = "G21 G90 G54\nS8000 M3\nG1 A200 F100\nM30\n"
    found = only(diagnose(body, no_wrap), "geometry.rotary-travel-exceeded")
    assert found[0].severity is Severity.ERROR
    assert "assuming a zero rotary work offset" in found[0].message


def test_rotary_wrap_warning_fires_beyond_the_threshold(profile) -> None:
    found = only(check(program("G1 A900 F1800\n"), profile), "geometry.rotary-wrap")
    assert found[0].severity is Severity.WARNING
    assert "900 deg" in found[0].message


def test_a_move_at_the_threshold_is_accepted(profile) -> None:
    """360 degrees is exactly one turn and the default threshold; wrapping is legitimate."""
    assert not of(check(program("G1 A360 F1800\n"), profile), "geometry.rotary-wrap")


def test_the_first_rotary_move_assumes_a_started_at_zero_and_says_so(profile) -> None:
    """Refusing to judge would skip the very first block, often the largest in a wrapping program."""
    found = only(check(program("G1 A900 F1800\n"), profile), "geometry.rotary-wrap")
    assert "assuming A started at 0" in found[0].message


def test_a_later_rotary_move_uses_the_real_delta(profile) -> None:
    """From A350 to A400 is a 50 degree move, not a 400 degree one.

    A400 is the discriminating target: it exceeds the 360 threshold in absolute terms while the
    delta does not, so a rule reading the absolute value would warn here and a correct one stays
    silent. An earlier version used A360, where neither reading exceeds the threshold and the bug
    was invisible.
    """
    body = "G1 A350 F1800\nG1 A400\n"
    found = of(check(program(body), profile), "geometry.rotary-wrap")
    assert found == [], [d.message for d in found]


def test_the_threshold_is_tunable() -> None:
    tight = load_profile_text('[machine]\nunits="mm"\n[limits]\nrotary_wrap_warn = 45.0\n')
    body = "G21 G90 G54\nS8000 M3\nG1 A90 F100\nM30\n"
    assert of(diagnose(body, tight), "geometry.rotary-wrap")


def test_rotary_travel_is_never_measured_in_millimetres(profile) -> None:
    """The mm/degrees split, at the reporting boundary this time."""
    found = only(check(program("G1 A900 F1800\n"), profile), "geometry.rotary-wrap")
    assert "mm" not in found[0].message


# --------------------------------------------------------------------------- fixtures


def test_the_clean_baseline_reports_nothing(profile, baseline_text) -> None:
    assert check(baseline_text, profile) == []


def test_the_g18_fixture_arcs_are_geometrically_valid(profile) -> None:
    """If the G18 IJK mapping were wrong, these arcs would show a radius mismatch."""
    found = check(fixture_text("arc_g18_direction.nc"), profile)
    assert not of(found, "geometry.arc-radius-mismatch")


def test_the_helical_fixture_arcs_are_valid(profile) -> None:
    assert not of(check(fixture_text("arc_helical.nc"), profile), "geometry.arc-radius-mismatch")


def test_the_full_circle_fixture_is_accepted_as_ijk(profile) -> None:
    found = check(fixture_text("arc_full_circle_ijk.nc"), profile)
    assert not of(found, "geometry.arc-r-invalid")
    assert not of(found, "geometry.arc-radius-mismatch")


def test_the_r_format_fixture_is_valid_for_both_signs(profile) -> None:
    assert not of(check(fixture_text("arc_r_format.nc"), profile), "geometry.arc-r-invalid")


def test_the_inch_fixture_arc_is_valid(profile) -> None:
    """Converted to mm at parse time, so the radius comparison happens in mm regardless."""
    assert not of(check(fixture_text("inch_program.nc"), profile), "geometry.arc-radius-mismatch")


def test_arc_radii_in_the_baseline_are_exact(baseline_text) -> None:
    """Independent of the checks: the baseline's arcs really are exact quarter circles.

    Asserted here so a future edit to the baseline cannot quietly introduce slop that the tolerance
    happens to absorb.
    """
    from foursight.machine.state import walk

    arcs = [
        arc_geometry(command, before) for command, before, _ in walk(parse(baseline_text).commands)
    ]
    found = [arc for arc in arcs if arc is not None]
    assert len(found) == 2
    for arc in found:
        assert arc.radius_start == pytest.approx(10.0, abs=1e-12)
        assert arc.radius_end == pytest.approx(10.0, abs=1e-12)
        assert math.isclose(arc.radius_start, arc.radius_end, abs_tol=1e-12)
