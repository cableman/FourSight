"""Simulator tests (T2.5).

This is the module that decides what gets drawn, so the tests are mostly about *refusal*: a canned
cycle must produce no geometry, an uninterpretable arc must produce none, and a lost position must
not be replaced with a guess. Drawing a plausible line in any of those cases is the failure the whole
project is organised around.

The one place a guess *is* made — an unset axis at program start — is asserted too, together with the
note that records it.
"""

import numpy as np
import pytest

from conftest import DEFAULT_PROFILE_PATH, fixture_text
from foursight.machine.profile import load_profile, load_profile_text
from foursight.parser.resolver import parse
from foursight.sim.segments import Kind
from foursight.sim.simulator import simulate, simulate_text

WITH_OFFSET = """
[machine]
units = "mm"
[limits]
max_feed = 3000.0
[axes.x]
max_rapid = 5000.0
[axes.y]
max_rapid = 5000.0
[axes.z]
max_rapid = 3000.0
[axes.a]
type = "rotary"
wrap = true
max_rapid = 3600.0
[offsets]
g54 = [10.0, 20.0, 30.0, 0.0]
"""

WITH_HOME = WITH_OFFSET + "\n[axes.x]\nhome = 0.0\n"
PREAMBLE = "G21 G90 G17 G94 G54\nG0 Z25.0\nT1 M6\nS8000 M3\n"


@pytest.fixture(scope="module")
def profile():
    return load_profile(DEFAULT_PROFILE_PATH)


@pytest.fixture(scope="module")
def offset_profile():
    return load_profile_text(WITH_OFFSET)


def run(program: str, profile):
    return simulate(parse(program).commands, profile)


def lines_with_segments(sim) -> set[int]:
    return set(sim.store.line.tolist())


# --------------------------------------------------------------------------- machine coordinates


def test_lin_is_written_in_machine_coordinates(offset_profile) -> None:
    """`lin` is what travel-limit verification reads, so it must already include the work offset."""
    sim = run("G21 G90 G54\nS8000 M3\nG1 X100 Y50 Z-5 F600\n", offset_profile)
    assert len(sim.store) >= 1
    end = sim.store.lin[-1, 1]
    assert end.tolist() == [110.0, 70.0, 25.0]


def test_rotary_goes_into_its_own_column(offset_profile) -> None:
    sim = run("G21 G90 G54\nS8000 M3\nG1 X10 A90 F600\n", offset_profile)
    assert sim.store.lin.shape[-1] == 3
    assert sim.store.rot[-1, 1] == pytest.approx(90.0)


def test_the_store_validates_and_every_segment_traces_to_a_line(profile) -> None:
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    sim.store.validate()
    assert int(sim.store.line.min()) >= 1
    assert lines_with_segments(sim) <= set(range(1, 40))


def test_rapids_and_feeds_are_classified(profile) -> None:
    sim = run(PREAMBLE + "G1 X10 F600\nG0 X20\n", profile)
    assert sim.store.mask(Kind.RAPID).sum() >= 1
    assert sim.store.mask(Kind.FEED).sum() >= 1


# --------------------------------------------------------------------------- canned cycles


def test_a_canned_cycle_span_produces_no_geometry(profile) -> None:
    """Under G81 a bare `X10 Y10` is a drill cycle; a line through the holes is the refused output."""
    sim = run(PREAMBLE + "G0 X0 Y0\nG81 Z-5 R2 F300\nX10\nX20\nG80\nG1 X30 F600\n", profile)
    cycle_lines = {6, 7, 8}
    assert cycle_lines & lines_with_segments(sim) == set()
    assert any(not span.drawn and span.contains(6) for span in sim.spans)


def test_the_cycle_span_covers_its_whole_extent(profile) -> None:
    sim = run(PREAMBLE + "G0 X0 Y0\nG81 Z-5 R2 F300\nX10\nX20\nG80\n", profile)
    span = next(span for span in sim.suppressed if span.contains(6))
    assert (span.first_line, span.last_line) == (6, 8)


def test_motion_after_g80_is_drawn_again(profile) -> None:
    sim = run(PREAMBLE + "G0 X0 Y0\nG81 Z-5 R2 F300\nX10\nG80\nG1 X30 F600\n", profile)
    assert 9 in lines_with_segments(sim), "the block after G80 must be drawn"
    assert {6, 7} & lines_with_segments(sim) == set(), "the cycle itself must not be"


def test_an_uncancelled_cycle_suppresses_to_the_end(profile) -> None:
    sim = run(PREAMBLE + "G0 X0 Y0\nG81 Z-5 R2 F300\nX10\nX20\nM30\n", profile)
    assert {6, 7, 8} & lines_with_segments(sim) == set()


# --------------------------------------------------------------------------- cutter compensation


def test_a_comp_span_is_drawn_but_marked_unverified(profile) -> None:
    """Refusing to draw comp spans would refuse a large share of real programs.

    The centreline is genuinely what was programmed — it just is not where the tool went.
    """
    sim = run(PREAMBLE + "G0 X0 Y0\nG41 D1\nG1 X10 F600\nG1 Y10\nG40\n", profile)
    span = next(span for span in sim.unverified if span.drawn)
    assert span.drawn is True
    assert "centerline" in span.reason
    assert {7, 8} & lines_with_segments(sim), "comp geometry must still be present"


def test_the_unverified_mask_selects_exactly_the_comp_segments(profile) -> None:
    sim = run(PREAMBLE + "G0 X0 Y0\nG41 D1\nG1 X10 F600\nG40\nG1 X20\n", profile)
    mask = sim.unverified_mask()
    assert mask.any(), "some segments must be flagged"
    assert not mask.all(), "the blocks outside the span must not be"
    flagged_lines = set(sim.store.line[mask].tolist())
    outside = set(sim.store.line[~mask].tolist())
    assert flagged_lines and not (flagged_lines & outside)


def test_the_mask_is_empty_when_nothing_is_unverified(profile) -> None:
    sim = run(PREAMBLE + "G1 X10 F600\n", profile)
    assert not sim.unverified_mask().any()


# --------------------------------------------------------------------------- refusals


def test_an_uninterpretable_arc_is_not_drawn(profile) -> None:
    """R-format with coincident endpoints is undefined; drawing something would invent geometry."""
    sim = run(PREAMBLE + "G0 X50 Y20\nG2 X50 Y20 R10 F600\n", profile)
    assert 6 not in lines_with_segments(sim)
    span = next(span for span in sim.suppressed if span.contains(6))
    assert "full circle" in span.reason


def test_a_reference_return_without_a_home_is_not_drawn(profile) -> None:
    """The shipped profile leaves `home` unset, so G28's target is genuinely unknown."""
    sim = run(PREAMBLE + "G1 X10 F600\nG28\n", profile)
    assert any("home" in span.reason for span in sim.suppressed)


def test_a_lost_position_suppresses_later_moves_rather_than_guessing(profile) -> None:
    """The distinction that matters: after an undrawable G28 we no longer know where the machine is.

    Assuming a position here would fabricate the rest of the program's geometry, unlike the
    program-start case where an unset axis can reasonably be taken as the machine reference.
    """
    sim = run(PREAMBLE + "G1 X10 F600\nG28\nG1 X20\nG1 X30\n", profile)
    assert 7 not in lines_with_segments(sim)
    assert 8 not in lines_with_segments(sim)
    assert any("lost" in span.reason for span in sim.suppressed)


def test_a_reference_return_with_a_home_is_drawn() -> None:
    # Written out in full rather than appended to WITH_OFFSET: TOML forbids redefining a table, so
    # a second [axes.x] section makes the whole profile unparseable.
    homed = load_profile_text(
        '[machine]\nunits = "mm"\n'
        "[axes.x]\nmax_rapid = 5000.0\nhome = 0.0\n"
        "[axes.y]\nmax_rapid = 5000.0\nhome = 0.0\n"
        "[axes.z]\nmax_rapid = 3000.0\nhome = 100.0\n"
        '[axes.a]\ntype = "rotary"\nwrap = true\nmax_rapid = 3600.0\nhome = 0.0\n'
        "[offsets]\ng54 = [0.0, 0.0, 0.0, 0.0]\n"
    )
    sim = run("G21 G90 G54\nS8000 M3\nG1 X10 Y10 F600\nG28\n", homed)
    assert 4 in lines_with_segments(sim)
    assert not any("home" in span.reason for span in sim.suppressed)


# --------------------------------------------------------------------------- program start


def test_the_machine_is_assumed_to_start_at_its_reference(profile) -> None:
    """The universal convention, and not noted, because a note on every program says nothing.

    The alternative — refusing to draw until every axis is established — was tried and is worse: the
    first move along each axis becomes undrawable, so a program that never mentions Y renders as
    empty. That loses real geometry to avoid a bounded assumption about one approach move's origin.
    """
    sim = run("G21 G90 G54\nS8000 M3\nG0 Z25.0\nG1 X10 F600\n", profile)
    assert 3 in lines_with_segments(sim), "the opening retract is drawn"
    assert 4 in lines_with_segments(sim)
    assert sim.notes == (), "an always-present note would be noise"
    assert sim.spans == ()


def test_a_program_that_never_mentions_an_axis_still_draws(profile) -> None:
    """The case that ruled out the stricter rule: Y is never commanded, so it never moves."""
    sim = run("G21 G90 G54\nS8000 M3\nG0 X0 Z5\nG1 X10 Z-1 F600\nG1 X20\n", profile)
    assert len(sim.store) >= 3
    assert np.allclose(sim.store.lin[:, :, 1], 0.0), "Y stays at the assumed reference"


def test_an_assumed_start_is_not_the_same_as_a_lost_one(profile) -> None:
    """Assuming a start position is conventional; assuming one after losing track is fabrication."""
    assumed = run("G21 G90 G54\nS8000 M3\nG1 X10 F600\n", profile)
    lost = run(PREAMBLE + "G1 X10 F600\nG28\nG1 X20\n", profile)
    assert assumed.spans == ()
    assert any("lost" in span.reason for span in lost.suppressed)


def test_a_full_circle_is_drawn_even_though_its_endpoints_coincide(profile) -> None:
    """Regression: "position unchanged" was treated as "no motion", silently dropping every circle.

    No error, no diagnostic — just a missing circle, which is the quietest possible form of the
    failure this project is organised around.
    """
    sim = run("G21 G90 G17 G54\nS8000 M3\nG0 X50 Y20 Z0\nG2 X50 Y20 I-10 J0 F600\n", profile)
    circle = sim.store.line == 4
    assert int(circle.sum()) > 8, "a full circle must tessellate, not vanish"
    points = sim.store.lin[circle][:, 0, :2]
    radii = np.hypot(points[:, 0] - 40.0, points[:, 1] - 20.0)
    assert np.allclose(radii, 10.0)
    assert np.allclose(sim.store.lin[circle][0, 0], sim.store.lin[circle][-1, 1]), "must close"


# --------------------------------------------------------------------------- durations and notes


def test_durations_are_filled_from_the_timing_model(profile) -> None:
    sim = run("G21 G90 G54\nS8000 M3\nG0 X0 Y0 Z0\nG1 X100 F600\n", profile)
    assert sim.duration == pytest.approx(10.0, rel=1e-6)
    assert sim.unknown_durations == 0


def test_a_move_without_a_feed_rate_is_counted_as_unknown(profile) -> None:
    sim = run("G21 G90 G54\nS8000 M3\nG0 X0 Y0 Z0\nG1 X100\n", profile)
    assert sim.unknown_durations >= 1


def test_the_tool_length_caveat_is_recorded(profile) -> None:
    sim = run(PREAMBLE + "G43 H2 Z10\nG1 X10 F600\n", profile)
    assert any("tool length" in note for note in sim.notes)
    assert 6 in lines_with_segments(sim), "the path is still drawn"


def test_no_tool_length_no_caveat(profile) -> None:
    sim = run(PREAMBLE + "G1 X10 F600\n", profile)
    assert not any("tool length" in note for note in sim.notes)


# --------------------------------------------------------------------------- progress


def test_progress_is_reported_and_always_ends_at_the_total(profile) -> None:
    """The seam T2.9 drives from a QThread; the simulator knows nothing about Qt."""
    seen: list[tuple[int, int]] = []
    commands = parse(PREAMBLE + "G1 X10 F600\n" * 50).commands
    simulate(commands, profile, progress=lambda d, n: seen.append((d, n)), progress_interval=10)
    assert seen[-1] == (len(commands), len(commands))
    assert all(done <= total for done, total in seen)


def test_progress_is_optional(profile) -> None:
    assert len(run(PREAMBLE + "G1 X10 F600\n", profile).store) >= 1


# --------------------------------------------------------------------------- fixtures


def test_the_baseline_fixture_simulates_cleanly(profile) -> None:
    sim, errors = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    assert errors == []
    assert sim.suppressed == (), [span.reason for span in sim.suppressed]
    assert sim.unverified == ()
    assert len(sim.store) > 100, "arcs and the rotary move should tessellate"
    assert sim.duration > 0.0
    assert sim.unknown_durations == 0


def test_every_fixture_simulates_without_raising(profile) -> None:
    from conftest import FIXTURES

    for path in sorted(FIXTURES.glob("*.nc")):
        sim, _ = simulate_text(fixture_text(path.name), profile)
        sim.store.validate()


def test_the_helical_fixture_produces_a_climbing_arc(profile) -> None:
    sim, _ = simulate_text(fixture_text("arc_helical.nc"), profile)
    z = sim.store.lin[:, :, 2]
    assert z.max() > z.min(), "a helix must change Z"
    assert len(sim.store) > 50, "arcs must tessellate rather than becoming chords"


def test_the_inch_fixture_is_simulated_in_millimetres(profile) -> None:
    """Geometry is mm internally whatever the program declared."""
    sim, _ = simulate_text(fixture_text("inch_program.nc"), profile)
    assert np.abs(sim.store.lin).max() > 25.0, "inch values must have been scaled"


def test_block_delete_changes_the_geometry(profile) -> None:
    executed, _ = simulate_text(fixture_text("block_delete.nc"), profile)
    skipped, _ = simulate_text(fixture_text("block_delete.nc"), profile, block_delete=True)
    assert len(executed.store) > len(skipped.store)
