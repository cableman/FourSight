"""Timeline tests (T3.5). The model half needs no Qt; the widget half runs offscreen.

Two properties are not arithmetic and carry the weight.

**An incomplete total is never presented as the cycle time.** `sim/timing` records 0.0 *and counts it*
when a move has no usable feed rate, precisely so a timeline can say how much of itself is missing. A
scrubber that showed the partial sum as the program's duration would be inventing a number — and it would
look entirely plausible.

**Zero-duration segments still have a position.** A dwell, a stationary block, or a rapid at an unknown
rate occupies a single instant. Scrubbing there must select something rather than skipping the run.
"""

import os

import numpy as np
import pytest

from conftest import DEFAULT_PROFILE_PATH, fixture_text
from foursight.gui.timeline import (
    build_timeline,
    describe_position,
    format_scrub_time,
)
from foursight.machine.profile import load_profile
from foursight.sim.segments import Kind, SegmentBuilder
from foursight.sim.simulator import simulate_text


@pytest.fixture(scope="module")
def profile():
    return load_profile(DEFAULT_PROFILE_PATH)


def store_with_durations(*durations: float):
    """A store whose segments have exactly the given durations."""
    builder = SegmentBuilder()
    for index, duration in enumerate(durations, start=1):
        points = np.array([[float(index - 1), 0, 0], [float(index), 0, 0]])
        builder.add_polyline(points, Kind.FEED, index, durations=np.array([duration]))
    return builder.finalize()


# --------------------------------------------------------------------------- the model


def test_cumulative_time_accumulates() -> None:
    timeline = build_timeline(store_with_durations(1.0, 2.0, 3.0))
    assert timeline.cumulative.tolist() == [1.0, 3.0, 6.0]
    assert timeline.total == 6.0


def test_index_at_finds_the_segment_in_progress() -> None:
    timeline = build_timeline(store_with_durations(1.0, 2.0, 3.0))
    assert timeline.index_at(0.5) == 0
    assert timeline.index_at(2.0) == 1
    assert timeline.index_at(4.0) == 2


def test_scrubbing_to_zero_selects_the_first_segment() -> None:
    """A position the slider can legitimately reach must not return None."""
    assert build_timeline(store_with_durations(1.0, 2.0)).index_at(0.0) == 0


def test_scrubbing_past_the_end_clamps_to_the_last_segment() -> None:
    timeline = build_timeline(store_with_durations(1.0, 2.0))
    assert timeline.index_at(999.0) == 1


def test_a_negative_position_clamps_to_the_start() -> None:
    assert build_timeline(store_with_durations(1.0)).index_at(-5.0) == 0


def test_a_zero_length_run_keeps_positions_in_range_and_monotonic() -> None:
    """Several segments can share one cumulative time, and no scrub position distinguishes them.

    That is inherent rather than a defect: a zero-duration move occupies no time, so time cannot address
    it — the editor and click-to-pick are how those are reached. What must hold is that every position
    stays in range and the mapping never goes backwards. My first version of this test asserted the
    zero-length segment was selectable, which is not achievable by scrubbing at all.
    """
    timeline = build_timeline(store_with_durations(1.0, 0.0, 0.0, 2.0))
    assert timeline.cumulative.tolist() == [1.0, 1.0, 1.0, 3.0]

    indices = [timeline.index_at(t) for t in (0.0, 0.5, 1.0, 1.5, 3.0, 99.0)]
    assert all(index is not None and 0 <= index < timeline.segments for index in indices)
    assert indices == sorted(indices), "scrub position went backwards over a zero-length run"
    assert timeline.index_at(1.0) == 0, "side='left' returns the earliest segment at that instant"


def test_an_empty_store_has_no_timeline() -> None:
    from foursight.sim.segments import SegmentStore

    timeline = build_timeline(SegmentStore.empty())
    assert timeline.segments == 0
    assert timeline.total == 0.0
    assert timeline.index_at(0.0) is None


def test_time_at_is_bounds_checked() -> None:
    timeline = build_timeline(store_with_durations(1.0, 2.0))
    assert timeline.time_at(1) == 3.0
    assert timeline.time_at(-1) == 0.0
    assert timeline.time_at(99) == 0.0


# --------------------------------------------------------------------------- incomplete totals


def test_unknown_durations_make_the_timeline_incomplete() -> None:
    """The property that stops a partial sum being presented as a cycle time."""
    assert build_timeline(store_with_durations(1.0), unknown=0).is_complete is True
    assert build_timeline(store_with_durations(1.0), unknown=3).is_complete is False


def test_an_incomplete_total_says_so_in_the_readout() -> None:
    timeline = build_timeline(store_with_durations(1.0, 2.0), unknown=2)
    described = describe_position(timeline, 1, 7)
    assert "total is short" in described
    assert "2" in described


def test_a_complete_total_carries_no_caveat() -> None:
    """Otherwise the caveat appears on every program and stops being read."""
    timeline = build_timeline(store_with_durations(1.0, 2.0), unknown=0)
    assert "short" not in describe_position(timeline, 1, 7)


def test_unknown_is_taken_from_the_simulator_not_inferred_from_zeros(profile) -> None:
    """A zero duration means "does not move" *or* "we could not tell", and only the simulator knows.

    A program of stationary blocks has zeros and nothing unknown; one with no feed rate has both.
    """
    stationary, _ = simulate_text("G21 G90 G94\nG1 X0 F600\n", profile)
    assert build_timeline(stationary.store, stationary.unknown_durations).is_complete is True

    no_feed, _ = simulate_text("G21 G90 G94\nG1 X10\nG1 X20\n", profile)
    assert no_feed.unknown_durations > 0
    assert build_timeline(no_feed.store, no_feed.unknown_durations).is_complete is False


# --------------------------------------------------------------------------- the readout


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0.0, "0:00.0"), (9.25, "0:09.2"), (61.5, "1:01.5"), (599.9, "9:59.9"), (3661.0, "1:01:01.0")],
)
def test_scrub_time_is_formatted_for_a_moving_readout(seconds: float, expected: str) -> None:
    assert format_scrub_time(seconds) == expected


def test_the_scrub_format_differs_from_the_cycle_time_format() -> None:
    """Two formats because there are two questions, not because one was forgotten.

    A cycle time gets compared against a job sheet ("1h 05m"); a scrub position needs sub-second
    resolution. Asserted so a later tidy-up does not collapse them into one.
    """
    from foursight.gui.session import _duration

    assert format_scrub_time(3900.0) != _duration(3900.0)


def test_the_readout_names_the_line_when_known() -> None:
    timeline = build_timeline(store_with_durations(1.0, 2.0))
    assert "line 12" in describe_position(timeline, 1, 12)


def test_the_readout_omits_the_line_when_unknown() -> None:
    assert "line" not in describe_position(build_timeline(store_with_durations(1.0)), 0, None)


def test_an_empty_timeline_says_there_is_no_geometry() -> None:
    from foursight.sim.segments import SegmentStore

    assert describe_position(build_timeline(SegmentStore.empty()), None, None) == "No geometry"


def test_the_readout_shows_position_over_total() -> None:
    timeline = build_timeline(store_with_durations(1.0, 2.0, 3.0))
    assert describe_position(timeline, 1, None).startswith("0:03.0 / 0:06.0")


# --------------------------------------------------------------------------- against real programs


def test_the_timeline_total_matches_the_simulation_duration(profile) -> None:
    """The scrubber and the status bar must not disagree about how long the program takes."""
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    timeline = build_timeline(sim.store, sim.unknown_durations)
    assert timeline.total == pytest.approx(sim.duration)


def test_every_scrub_position_maps_to_a_valid_segment(profile) -> None:
    """The index is used to look up `store.line`, so it must always be in range."""
    sim, _ = simulate_text(fixture_text("arc_helical.nc"), profile)
    timeline = build_timeline(sim.store, sim.unknown_durations)
    for fraction in np.linspace(0.0, 1.0, 51):
        index = timeline.index_at(fraction * timeline.total)
        assert index is not None
        assert 0 <= index < len(sim.store)


def test_the_timeline_is_monotonic(profile) -> None:
    """A non-monotonic cumulative sum would make `searchsorted` return nonsense."""
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    cumulative = build_timeline(sim.store).cumulative
    assert np.all(np.diff(cumulative) >= 0)


# --------------------------------------------------------------------------- the widget


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="the [gui] extra is not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from foursight.gui.timeline_bar import TICKS, TimelineBar  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bar(qt_app):
    try:
        return TimelineBar()
    except Exception as error:  # pragma: no cover - environment-dependent
        pytest.skip(f"cannot construct the timeline bar: {error}")


def test_the_slider_is_disabled_until_a_program_is_loaded(bar) -> None:
    assert bar.slider.isEnabled() is False


def test_loading_a_program_enables_the_slider(bar, profile) -> None:
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    bar.set_simulation(sim)
    assert bar.slider.isEnabled() is True
    assert bar.timeline is not None


def test_a_program_with_no_usable_time_leaves_the_slider_disabled(bar, profile) -> None:
    """It has segments but a zero total, and a slider that moves without changing anything is worse."""
    sim, _ = simulate_text("G21 G90 G94\nG1 X10\n", profile)
    bar.set_simulation(sim)
    assert sim.duration == 0.0
    assert bar.slider.isEnabled() is False


def test_scrubbing_emits_a_segment_index(bar, profile) -> None:
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    bar.set_simulation(sim)
    received = []
    bar.scrubbed.connect(received.append)
    bar.slider.setValue(TICKS // 2)
    assert received
    assert 0 <= received[-1] < len(sim.store)


def test_the_slider_resolution_is_relative_to_the_program(bar, profile) -> None:
    """Thousandths, not seconds: a 2-second program and a 40-hour one both get 1000 steps."""
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    bar.set_simulation(sim)
    assert bar.slider.maximum() == TICKS
    bar.slider.setValue(TICKS)
    assert bar.seconds == pytest.approx(bar.timeline.total)


def test_loading_a_new_program_resets_the_position_without_emitting(bar, profile) -> None:
    """A reset that emitted would jump the editor on every load."""
    first, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    bar.set_simulation(first)
    bar.slider.setValue(TICKS // 2)

    received = []
    bar.scrubbed.connect(received.append)
    second, _ = simulate_text(fixture_text("arc_helical.nc"), profile)
    bar.set_simulation(second)
    assert bar.slider.value() == 0
    assert received == [], "resetting the slider emitted a scrub"


def test_clearing_disables_and_resets(bar, profile) -> None:
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    bar.set_simulation(sim)
    bar.clear()
    assert bar.timeline is None
    assert bar.slider.isEnabled() is False
    assert bar.readout.text() == "No geometry"
