"""Interpolated travel-limit tests (T2.8).

PLAN.md's reason for this task, stated as a test: *an arc can bulge past a limit mid-sweep while both
of its endpoints sit comfortably inside it*, so endpoint-only checking can pass a program that would
crash the machine. Every test here therefore uses geometry where the two checks **disagree** — an
arc that violated at its endpoints would prove nothing about interpolation.
"""

import pytest

from foursight.machine.profile import load_profile_text
from foursight.parser.resolver import parse
from foursight.sim.simulator import simulate
from foursight.verify.report import Severity
from foursight.verify.rules import Program, verify

SMALL_ENVELOPE = """
[machine]
units = "mm"
[axes.x]
min = 0.0
max = 100.0
max_rapid = 5000.0
[axes.y]
min = 0.0
max = 100.0
max_rapid = 5000.0
[axes.z]
min = -50.0
max = 50.0
max_rapid = 3000.0
[offsets]
g54 = [0.0, 0.0, 0.0, 0.0]
"""

# Centre (50, 90) radius 20, endpoints (30, 90) -> (70, 90). G2 sweeps clockwise over the top, so Y
# peaks at 110 while both endpoints sit at 90.
BULGING_ARC = "G21 G90 G17 G54\nS8000 M3\nG0 X30 Y90 Z0\nG2 X70 Y90 I20 J0 F600\nM30\n"


def check(program: str, profile_text: str = SMALL_ENVELOPE, *, interpolated: bool = True):
    profile = load_profile_text(profile_text)
    result = parse(program)
    segments = simulate(result.commands, profile).store if interpolated else None
    return verify(
        Program(
            commands=result.commands,
            profile=profile,
            parse_errors=result.errors,
            segments=segments,
        )
    )


def travel(diagnostics, rule_id: str = "geometry.axis-travel-exceeded"):
    return [d for d in diagnostics if d.rule_id == rule_id]


# --------------------------------------------------------------------------- the point of T2.8


def test_an_arc_bulging_past_a_limit_is_caught_only_with_interpolation() -> None:
    """Both endpoints sit at Y90 inside a Y100 limit; the sweep reaches Y110."""
    assert travel(check(BULGING_ARC, interpolated=False)) == []
    found = travel(check(BULGING_ARC))
    assert len(found) == 1
    assert "reaches 110 mm" in found[0].message
    assert found[0].severity is Severity.ERROR
    assert found[0].line == 4


def test_the_endpoints_really_are_inside_the_limit() -> None:
    """Guards the premise: an arc violating at its endpoints would prove nothing about interpolation."""
    profile = load_profile_text(SMALL_ENVELOPE)
    commands = parse(BULGING_ARC).commands
    store = simulate(commands, profile).store
    arc = store.line == 4
    ys = store.lin[arc][:, :, 1]
    assert ys[0, 0] == pytest.approx(90.0)
    assert ys[-1, 1] == pytest.approx(90.0)
    assert ys.max() > 100.0


def test_a_violation_is_reported_once_per_line_not_once_per_segment() -> None:
    """A long breach would otherwise produce a diagnostic per interpolated point."""
    profile = load_profile_text(SMALL_ENVELOPE)
    commands = parse(BULGING_ARC).commands
    store = simulate(commands, profile).store
    offending = int((store.lin[:, :, 1] > 100.0).sum())
    assert offending > 20, "the premise: many points are outside"
    assert len(travel(check(BULGING_ARC))) == 1


def test_the_worst_point_is_the_one_reported() -> None:
    """ "Reaches 110" is actionable; reporting the first point over the line would understate it."""
    found = travel(check(BULGING_ARC))
    assert "110 mm" in found[0].message


def test_a_compliant_arc_reports_nothing() -> None:
    inside = "G21 G90 G17 G54\nS8000 M3\nG0 X30 Y50 Z0\nG2 X70 Y50 I20 J0 F600\nM30\n"
    assert travel(check(inside)) == []


def test_a_minimum_breach_mid_sweep_is_also_caught() -> None:
    """Centre (50,10) radius 20 swept counter-clockwise dips to Y-10, below the Y0 limit."""
    program = "G21 G90 G17 G54\nS8000 M3\nG0 X30 Y10 Z0\nG3 X70 Y10 I20 J0 F600\nM30\n"
    found = travel(check(program))
    assert len(found) == 1
    assert "minimum" in found[0].message


# --------------------------------------------------------------------------- severity


def test_an_unknown_work_offset_downgrades_the_interpolated_error_too() -> None:
    """The same downgrade the endpoint check applies, per PLAN.md."""
    no_offsets = SMALL_ENVELOPE.replace("[offsets]\ng54 = [0.0, 0.0, 0.0, 0.0]\n", "")
    found = travel(check(BULGING_ARC, no_offsets))
    assert len(found) == 1
    assert found[0].severity is Severity.WARNING
    assert "unconfirmed" in found[0].message


def test_a_g53_move_needs_no_offset_to_be_confirmed() -> None:
    """G53 coordinates are already machine-absolute, so the offset is irrelevant to them."""
    no_offsets = SMALL_ENVELOPE.replace("[offsets]\ng54 = [0.0, 0.0, 0.0, 0.0]\n", "")
    program = "G21 G90 G17\nS8000 M3\nG53 G0 X150\nM30\n"
    found = travel(check(program, no_offsets))
    assert found and found[0].severity is Severity.ERROR


# --------------------------------------------------------------------------- rotary


def test_rotary_limits_are_checked_over_interpolated_points_when_not_wrapping() -> None:
    profile_text = (
        '[machine]\nunits = "mm"\n'
        '[axes.a]\ntype = "rotary"\nwrap = false\nmin = -90.0\nmax = 90.0\nmax_rapid = 3600.0\n'
        "[offsets]\ng54 = [0.0, 0.0, 0.0, 0.0]\n"
    )
    program = "G21 G90 G54\nS8000 M3\nG1 A200 F1800\nM30\n"
    found = travel(check(program, profile_text), "geometry.rotary-travel-exceeded")
    assert found
    assert "deg" in found[0].message
    assert "mm" not in found[0].message, "rotary values are degrees, never millimetres"


def test_a_wrapping_rotary_axis_is_still_exempt() -> None:
    profile_text = (
        '[machine]\nunits = "mm"\n'
        '[axes.a]\ntype = "rotary"\nwrap = true\nmin = -90.0\nmax = 90.0\nmax_rapid = 3600.0\n'
        "[offsets]\ng54 = [0.0, 0.0, 0.0, 0.0]\n"
    )
    program = "G21 G90 G54\nS8000 M3\nG1 A3600 F1800\nM30\n"
    assert travel(check(program, profile_text), "geometry.rotary-travel-exceeded") == []


# --------------------------------------------------------------------------- fallback behaviour


def test_the_endpoint_check_still_works_without_a_simulation() -> None:
    """`foursight check --no-simulate` and any headless caller must keep working."""
    over = "G21 G90 G17 G54\nS8000 M3\nG1 X150 F600\nM30\n"
    found = travel(check(over, interpolated=False))
    assert len(found) == 1
    assert found[0].severity is Severity.ERROR


def test_both_paths_agree_when_the_endpoint_is_the_worst_point() -> None:
    """A straight move's extreme *is* its endpoint, so the two checks must not disagree there."""
    over = "G21 G90 G17 G54\nS8000 M3\nG1 X150 F600\nM30\n"
    with_sim = travel(check(over))
    without = travel(check(over, interpolated=False))
    assert len(with_sim) == len(without) == 1
    assert with_sim[0].line == without[0].line


def test_an_empty_segment_store_falls_back_to_endpoints() -> None:
    """A program whose geometry was entirely suppressed must not silently skip the check."""
    profile = load_profile_text(SMALL_ENVELOPE)
    result = parse("G21 G90 G17 G54\nS8000 M3\nG1 X150 F600\nM30\n")
    from foursight.sim.segments import SegmentStore

    found = travel(
        verify(
            Program(
                commands=result.commands,
                profile=profile,
                parse_errors=result.errors,
                segments=SegmentStore.empty(),
            )
        )
    )
    assert len(found) == 1, "an empty store means no interpolation happened, not no violation"


def test_no_duplicate_diagnostics_from_the_two_paths() -> None:
    """One rule, one diagnostic: the interpolated path replaces the endpoint path rather than adding."""
    over = "G21 G90 G17 G54\nS8000 M3\nG1 X150 F600\nM30\n"
    found = travel(check(over))
    assert len({(d.line, d.message) for d in found}) == len(found)


def test_the_baseline_fixture_stays_clean_with_interpolation(baseline_text: str) -> None:
    """The extra precision must not start flagging a program that was previously clean."""
    from conftest import DEFAULT_PROFILE_PATH
    from foursight.machine.profile import load_profile

    profile = load_profile(DEFAULT_PROFILE_PATH)
    result = parse(baseline_text)
    store = simulate(result.commands, profile).store
    found = verify(
        Program(
            commands=result.commands,
            profile=profile,
            parse_errors=result.errors,
            segments=store,
        )
    )
    assert found == [], [d.message for d in found]


def test_interpolated_checking_scales_to_many_segments() -> None:
    """Aggregation must hold at scale: a long violating path is still one diagnostic per line."""
    profile = load_profile_text(SMALL_ENVELOPE)
    program = "G21 G90 G17 G54\nS8000 M3\nG0 X30 Y90 Z0\n" + "G2 X70 Y90 I20 J0 F600\n" * 20
    result = parse(program)
    store = simulate(result.commands, profile).store
    assert len(store) > 500
    found = travel(
        verify(
            Program(
                commands=result.commands,
                profile=profile,
                parse_errors=result.errors,
                segments=store,
            )
        )
    )
    # Aggregation is per (line, axis), not per line: a single arc can leave the envelope on X *and*
    # on Y, and a user needs to be told about both. What must never happen is a diagnostic per
    # interpolated point.
    keys = [(d.line, d.message.split()[0]) for d in found]
    assert len(keys) == len(set(keys)), "one diagnostic per line and axis"
    assert len(found) <= 20 * 3, "at most one per line per linear axis"
    assert len(found) < len(store) / 10, "nowhere near one per segment"


# ------------------------------------------------------- the stock envelope, simulated (M7)
#
# `geometry.rapid-into-stock` is the one rule here that loses nothing without a simulation — a rapid
# is a straight line, so its endpoints *are* its path. What the interpolated path adds is rapids the
# endpoint walker never produces at all, a G28's two legs above everything else.

STOCK_ENVELOPE = """
[machine]
units = "mm"
[stock]
min = [0.0, 0.0, -20.0]
max = [100.0, 80.0, 0.0]
[offsets]
g54 = [0.0, 0.0, 0.0, 0.0]
[axes.x]
max_rapid = 5000.0
home = 0.0
[axes.y]
max_rapid = 5000.0
home = 0.0
[axes.z]
max_rapid = 3000.0
home = 100.0
[axes.a]
type = "rotary"
wrap = true
max_rapid = 3600.0
home = 0.0
"""

STOCK_RULE = "geometry.rapid-into-stock"

TRAVERSE_AT_DEPTH = """G21 G90 G17 G54
S8000 M3
G0 X50 Y40 Z10
G1 Z-3 F200
G0 X90 Y70
G0 Z25
M30
"""

# G28 with axis words traverses to the intermediate point and then to the reference point. Both legs
# are rapids and both share one source line, and neither is reachable from block endpoints: the
# modal motion here is G1, so the endpoint walker does not treat this block as a rapid at all.
REFERENCE_RETURN = """G21 G90 G17 G54
S8000 M3
G0 X50 Y40 Z10
G1 Z-3 F200
G28 X90 Y70
M30
"""


def _stock(program: str, *, interpolated: bool = True):
    return [
        d
        for d in check(program, STOCK_ENVELOPE, interpolated=interpolated)
        if d.rule_id == STOCK_RULE
    ]


def test_the_simulated_path_finds_the_traverse_at_depth() -> None:
    found = _stock(TRAVERSE_AT_DEPTH)
    assert len(found) == 1
    assert found[0].line == 5
    assert "-3 mm" in found[0].message


def test_the_two_paths_agree_on_an_ordinary_program() -> None:
    """The endpoint fallback is exact for this rule, so the two must not merely be similar."""
    simulated = _stock(TRAVERSE_AT_DEPTH)
    endpoints = _stock(TRAVERSE_AT_DEPTH, interpolated=False)
    assert [(d.line, d.message) for d in simulated] == [(d.line, d.message) for d in endpoints]


def test_only_the_simulated_path_sees_a_reference_return_crossing_the_stock() -> None:
    """A G28 leg is a rapid that exists nowhere in the block's own words."""
    assert _stock(REFERENCE_RETURN, interpolated=False) == []
    found = _stock(REFERENCE_RETURN)
    assert len(found) == 1
    assert found[0].line == 5


def test_a_multi_leg_rapid_is_one_diagnostic_for_its_line() -> None:
    """Both G28 legs cross the envelope; the user needs telling once, about line 5."""
    profile = load_profile_text(STOCK_ENVELOPE)
    result = parse(REFERENCE_RETURN)
    store = simulate(result.commands, profile).store
    legs = int((store.line == 5).sum())
    assert legs == 2, "the fixture must really produce two legs, or this proves nothing"
    assert len(_stock(REFERENCE_RETURN)) == 1


def test_a_rapid_from_an_unestablished_position_is_not_judged() -> None:
    """The simulator draws the opening rapid from the machine reference so the picture keeps its
    approach move. That assumption is fine for a picture and not fine for a diagnostic: reporting it
    would announce a collision with a position nobody established.
    """
    opening_rapid_through_the_stock = "G21 G90 G17 G54\nS8000 M3\nG0 X50 Y40 Z-3\nM30\n"
    profile = load_profile_text(STOCK_ENVELOPE)
    result = parse(opening_rapid_through_the_stock)
    store = simulate(result.commands, profile).store
    drawn = store.lin[0, 0, :]
    assert list(drawn) == [0.0, 0.0, 0.0], (
        "the fixture depends on the simulator assuming the origin"
    )
    assert _stock(opening_rapid_through_the_stock) == []
