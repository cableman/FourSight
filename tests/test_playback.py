"""Playback tests (T10.1/T10.2). The clock and the interpolation need no Qt; the widget half is in
`test_timeline.py`'s company at the bottom of this file once the transport exists.

Three properties carry the weight, and none of them is arithmetic.

**Playback stops itself at the end.** A player that runs off the end of the program and keeps consuming
frames looks identical to one that has hung, and the difference only shows up in a profiler.

**A program with no usable feed rate refuses to play rather than playing instantly.** `sim/timing`
records 0.0 for a move it cannot time, so such a program has geometry and a zero total. Playing it
would sweep the marker to the end in one frame and present that as a preview.

**The marker never leaves the toolpath.** It is interpolated inside the segment in progress, and a
segment is a straight chord by the time it reaches the store. Anything that put the marker off the
chord — interpolating in the wrong array, rotating by the wrong angle — would draw a tool position that
is confidently, plausibly wrong, which is the failure PLAN.md exists to prevent.
"""

import os

import numpy as np
import pytest

from conftest import DEFAULT_PROFILE_PATH, fixture_text
from foursight.gui.playback import SPEEDS, Playback, marker_point
from foursight.gui.timeline import build_timeline, describe_position
from foursight.machine.profile import load_profile
from foursight.sim.segments import Kind, SegmentBuilder, SegmentStore
from foursight.sim.simulator import simulate_text


@pytest.fixture(scope="module")
def profile():
    return load_profile(DEFAULT_PROFILE_PATH)


def polyline_store(points, durations, line: int = 1) -> SegmentStore:
    """A store of one polyline with the given per-segment durations."""
    builder = SegmentBuilder()
    builder.add_polyline(
        np.asarray(points, dtype=np.float64),
        Kind.FEED,
        line,
        durations=np.asarray(durations, dtype=np.float64),
    )
    return builder.finalize()


# --------------------------------------------------------------------------- the clock


def test_advancing_accumulates_program_time() -> None:
    clock = Playback(total=10.0)
    clock.play()
    clock.advance(0.5)
    assert clock.advance(0.5) == pytest.approx(1.0)


def test_speed_multiplies_wall_time() -> None:
    """The whole point of the multiplier: a 40-hour job is not previewable at 1×."""
    clock = Playback(total=100.0, speed=10.0)
    clock.play()
    assert clock.advance(0.5) == pytest.approx(5.0)


def test_changing_speed_does_not_move_the_position() -> None:
    """It changes the rate of the next frame, not where we are — otherwise the marker teleports."""
    clock = Playback(total=100.0)
    clock.play()
    clock.advance(1.0)
    clock.set_speed(100.0)
    assert clock.seconds == pytest.approx(1.0)


def test_a_non_positive_speed_is_refused() -> None:
    """Zero would freeze playback while `playing` stayed True; negative would run it backwards."""
    clock = Playback(total=10.0)
    with pytest.raises(ValueError, match="positive"):
        clock.set_speed(0.0)


def test_advancing_while_paused_changes_nothing() -> None:
    clock = Playback(total=10.0)
    clock.seek(3.0)
    assert clock.advance(1.0) == pytest.approx(3.0)
    assert clock.playing is False


def test_playback_pauses_itself_at_the_end() -> None:
    """Otherwise the timer keeps firing against a pinned position — a hang that looks like a still."""
    clock = Playback(total=2.0)
    clock.play()
    clock.advance(5.0)
    assert clock.seconds == pytest.approx(2.0)
    assert clock.playing is False


def test_playing_from_the_end_rewinds() -> None:
    """Pressing play on a finished program must do something visible."""
    clock = Playback(total=2.0)
    clock.play()
    clock.advance(5.0)
    clock.play()
    assert clock.seconds == 0.0
    assert clock.playing is True


def test_a_program_with_no_usable_time_refuses_to_play() -> None:
    """Segments but a zero total. A play button that lights up and does nothing reads as broken."""
    clock = Playback(total=0.0)
    clock.play()
    assert clock.playing is False
    assert clock.advance(1.0) == 0.0


def test_seeking_clamps_to_the_program() -> None:
    clock = Playback(total=10.0)
    clock.seek(-5.0)
    assert clock.seconds == 0.0
    clock.seek(50.0)
    assert clock.seconds == pytest.approx(10.0)


def test_seeking_does_not_stop_playback() -> None:
    """Dragging the handle mid-run is a seek, not a stop."""
    clock = Playback(total=10.0)
    clock.play()
    clock.seek(4.0)
    assert clock.playing is True
    assert clock.advance(1.0) == pytest.approx(5.0)


def test_reset_rewinds_and_stops_where_pause_only_stops() -> None:
    clock = Playback(total=10.0)
    clock.play()
    clock.advance(3.0)
    clock.pause()
    assert clock.seconds == pytest.approx(3.0)
    clock.reset()
    assert (clock.seconds, clock.playing) == (0.0, False)


def test_toggle_flips_the_transport() -> None:
    clock = Playback(total=10.0)
    clock.toggle()
    assert clock.playing is True
    clock.toggle()
    assert clock.playing is False


def test_a_negative_wall_step_never_runs_the_tool_backwards() -> None:
    """`QElapsedTimer` is monotonic, so this should be unreachable — but the failure mode if it were
    not is a tool walking backwards through a program that only moves forwards."""
    clock = Playback(total=10.0)
    clock.play()
    clock.advance(2.0)
    assert clock.advance(-5.0) == pytest.approx(2.0)


def test_the_speeds_are_offered_in_increasing_order() -> None:
    assert list(SPEEDS) == sorted(SPEEDS)
    assert SPEEDS[0] == 1.0, "real time must be available"


# --------------------------------------------------------------------------- the marker


def test_the_marker_interpolates_inside_a_segment() -> None:
    """Snapping to endpoints is what makes a marker crawl in jerks on a coarse program."""
    store = polyline_store([[0, 0, 0], [10, 0, 0]], [2.0])
    timeline = build_timeline(store)
    assert marker_point(store, timeline, 1.0) == pytest.approx([5.0, 0.0, 0.0])


def test_the_marker_at_a_segment_boundary_is_the_shared_point() -> None:
    """`index_at` uses side="left", so the boundary instant is the *end* of the earlier segment —
    which is the same point in space as the start of the next one, so the marker cannot jump."""
    store = polyline_store([[0, 0, 0], [10, 0, 0], [10, 10, 0]], [2.0, 2.0])
    timeline = build_timeline(store)
    assert marker_point(store, timeline, 2.0) == pytest.approx([10.0, 0.0, 0.0])


def test_the_marker_starts_at_the_first_point() -> None:
    store = polyline_store([[1, 2, 3], [10, 0, 0]], [2.0])
    timeline = build_timeline(store)
    assert marker_point(store, timeline, 0.0) == pytest.approx([1.0, 2.0, 3.0])


def test_scrubbing_past_the_end_pins_the_marker_to_the_last_point() -> None:
    store = polyline_store([[0, 0, 0], [10, 0, 0]], [2.0])
    timeline = build_timeline(store)
    assert marker_point(store, timeline, 99.0) == pytest.approx([10.0, 0.0, 0.0])


def test_a_zero_duration_segment_yields_its_start_point() -> None:
    """An instant has no inside, and dividing by its duration is how that becomes a crash."""
    store = polyline_store([[0, 0, 0], [10, 0, 0], [20, 0, 0], [30, 0, 0]], [1.0, 0.0, 2.0])
    timeline = build_timeline(store)
    assert marker_point(store, timeline, 1.0) == pytest.approx([10.0, 0.0, 0.0])


def test_an_empty_program_has_no_marker() -> None:
    store = SegmentStore.empty()
    assert marker_point(store, build_timeline(store), 0.0) is None


def test_the_marker_reads_part_coordinates_when_they_are_displayed() -> None:
    """A marker in machine coordinates over a part-coordinate toolpath floats beside the path."""
    store = polyline_store([[0, 0, 0], [10, 0, 0]], [2.0])
    store.set_part_coordinates(store.lin + np.array([100.0, 0.0, 0.0]))
    timeline = build_timeline(store)
    assert marker_point(store, timeline, 1.0, part_coordinates=True) == pytest.approx(
        [105.0, 0.0, 0.0]
    )


def test_part_coordinates_without_a_transform_are_refused() -> None:
    """The same contract as the highlight: raise rather than quietly draw the other frame."""
    store = polyline_store([[0, 0, 0], [10, 0, 0]], [2.0])
    timeline = build_timeline(store)
    with pytest.raises(ValueError, match="display transform"):
        marker_point(store, timeline, 1.0, part_coordinates=True)


def test_the_marker_never_leaves_the_chord(profile) -> None:
    """The property that matters, over a real program: at every position the marker is *on* the
    segment in progress and between its endpoints — never off the path, never past the move."""
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    timeline = build_timeline(sim.store, sim.unknown_durations)
    assert timeline.total > 0.0

    for step in range(51):
        seconds = timeline.total * step / 50.0
        index = timeline.index_at(seconds)
        point = marker_point(sim.store, timeline, seconds)
        begin, end = sim.store.lin[index]
        delta = end - begin
        length_sq = float(delta @ delta)
        if length_sq == 0.0:
            assert point == pytest.approx(begin)
            continue
        fraction = float((point - begin) @ delta) / length_sq
        assert -1e-9 <= fraction <= 1.0 + 1e-9, "the marker ran past the ends of its own segment"
        assert np.linalg.norm((point - begin) - fraction * delta) < 1e-9, (
            "the marker left the chord"
        )


def test_the_marker_only_ever_moves_forwards(profile) -> None:
    """Distance travelled along the path is monotonic in time. A marker that stepped backwards would
    look like a rendering glitch rather than the timing bug it would be."""
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    timeline = build_timeline(sim.store, sim.unknown_durations)
    lengths = np.linalg.norm(sim.store.lin[:, 1] - sim.store.lin[:, 0], axis=1)
    travelled = np.concatenate(([0.0], np.cumsum(lengths)))

    previous = -1.0
    for step in range(201):
        seconds = timeline.total * step / 200.0
        index = timeline.index_at(seconds)
        point = marker_point(sim.store, timeline, seconds)
        distance = travelled[index] + float(np.linalg.norm(point - sim.store.lin[index, 0]))
        assert distance >= previous - 1e-9, f"the marker went backwards at {seconds:.3f}s"
        previous = distance


# --------------------------------------------------------------------------- the readout


def test_the_readout_can_show_a_position_inside_a_segment() -> None:
    """Without this the clock would tick in uneven jumps of whatever the current move takes."""
    timeline = build_timeline(polyline_store([[0, 0, 0], [10, 0, 0]], [60.0]))
    assert describe_position(timeline, 0, None, seconds=12.5).startswith("0:12.5 / 1:00.0")


def test_the_readout_without_a_position_still_names_the_segment_end() -> None:
    """The scrubber passes nothing and must keep its old behaviour."""
    timeline = build_timeline(polyline_store([[0, 0, 0], [10, 0, 0]], [60.0]))
    assert describe_position(timeline, 0, None).startswith("1:00.0 / 1:00.0")


# --------------------------------------------------------------------------- the transport widget

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="the [gui] extra is not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from foursight.gui.timeline import format_scrub_time  # noqa: E402
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


@pytest.fixture
def loaded(bar, profile):
    """A bar showing a real program, plus that program's simulation."""
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    bar.set_simulation(sim)
    return bar, sim


def transport(bar):
    return (bar.play_button, bar.reset_button, bar.speed_box)


def test_the_transport_is_disabled_until_a_program_is_loaded(bar) -> None:
    assert [widget.isEnabled() for widget in transport(bar)] == [False, False, False]


def test_loading_a_program_enables_the_transport(loaded) -> None:
    bar, _ = loaded
    assert all(widget.isEnabled() for widget in transport(bar))


def test_a_program_with_no_usable_time_leaves_the_transport_disabled(bar, profile) -> None:
    """Segments but a zero total. The same judgement that already disables the slider: a play button
    that lights up and does nothing reads as a broken player rather than an untimed program."""
    sim, _ = simulate_text("G21 G90 G94\nG1 X10\n", profile)
    bar.set_simulation(sim)
    assert sim.duration == 0.0
    assert not any(widget.isEnabled() for widget in transport(bar))
    assert bar.slider.isEnabled() is False


def test_playing_starts_the_frame_timer_and_pausing_stops_it(loaded) -> None:
    bar, _ = loaded
    bar.toggle_playback()
    assert bar.playback.playing is True
    assert bar._timer.isActive() is True

    bar.toggle_playback()
    assert bar.playback.playing is False
    assert bar._timer.isActive() is False


def test_a_frame_advances_the_position_and_moves_the_handle(loaded) -> None:
    bar, _ = loaded
    bar.toggle_playback()
    bar._tick(1.0)
    assert bar.seconds == pytest.approx(1.0)
    assert bar.slider.value() == round(1.0 / bar.timeline.total * TICKS)


def test_a_frame_reports_both_the_position_and_the_segment(loaded) -> None:
    """Two signals because a segment index cannot say where *inside* a move the tool is."""
    bar, _ = loaded
    positions, indices = [], []
    bar.advanced.connect(positions.append)
    bar.scrubbed.connect(indices.append)

    bar.toggle_playback()
    bar._tick(1.0)
    assert positions == [pytest.approx(1.0)]
    assert indices and 0 <= indices[-1] < bar.timeline.segments


def test_reaching_the_end_stops_the_timer_and_restores_the_play_icon(loaded) -> None:
    """A player that runs off the end and keeps firing frames is a hang that looks like a still."""
    bar, _ = loaded
    bar.toggle_playback()
    before = bar.play_button.icon().cacheKey()
    bar._tick(bar.timeline.total + 1.0)

    assert bar.seconds == pytest.approx(bar.timeline.total)
    assert bar.playback.playing is False
    assert bar._timer.isActive() is False
    assert bar.play_button.icon().cacheKey() != before, "the icon still shows pause"


def test_a_frame_while_the_handle_is_held_does_not_advance(loaded) -> None:
    """A stationary held handle emits no `valueChanged`, so nothing would seek the clock back — at
    1000× the position would run away underneath the user's fingers."""
    bar, _ = loaded
    bar.toggle_playback()
    bar._tick(1.0)
    bar.slider.setSliderDown(True)
    bar._tick(5.0)
    assert bar.seconds == pytest.approx(1.0)


def test_dragging_during_playback_seeks_without_stopping(loaded) -> None:
    """A drag mid-run is a seek, not a stop. Making the user press play again is the difference
    between a player and a toy."""
    bar, _ = loaded
    bar.toggle_playback()
    bar.slider.setSliderDown(True)
    bar.slider.setValue(TICKS // 2)

    assert bar.seconds == pytest.approx(bar.timeline.total / 2.0, rel=1e-3)
    assert bar.playback.playing is True

    bar.slider.setSliderDown(False)
    bar._tick(1.0)
    assert bar.seconds > bar.timeline.total / 2.0


def test_the_speed_selector_changes_the_clock(loaded) -> None:
    bar, _ = loaded
    bar.speed_box.setCurrentIndex(1)
    assert bar.playback.speed == SPEEDS[1]

    bar.toggle_playback()
    bar._tick(1.0)
    assert bar.seconds == pytest.approx(SPEEDS[1])


def test_the_speed_selection_survives_a_reload(bar, profile) -> None:
    """Every applied fix reloads the program; a multiplier that snapped back to 1× each time would
    make an inspection at 100× unusable."""
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    bar.set_simulation(sim)
    bar.speed_box.setCurrentIndex(2)
    bar.set_simulation(sim)
    assert bar.playback.speed == SPEEDS[2]


def test_loading_a_new_program_stops_playback_without_emitting(loaded, profile) -> None:
    """The reset path every reload goes through. A reset that emitted would jump the editor."""
    bar, _ = loaded
    bar.toggle_playback()
    bar._tick(1.0)

    received = []
    bar.advanced.connect(received.append)
    bar.scrubbed.connect(received.append)
    second, _ = simulate_text(fixture_text("arc_helical.nc"), profile)
    bar.set_simulation(second)

    assert bar._timer.isActive() is False
    assert bar.playback.playing is False
    assert bar.seconds == 0.0
    assert bar.slider.value() == 0
    assert received == [], "resetting for a new program emitted a position"


def test_the_reset_button_rewinds_and_emits(loaded) -> None:
    """Unlike the load reset, this one is the user asking to go back — the editor should follow."""
    bar, _ = loaded
    bar.toggle_playback()
    bar._tick(2.0)

    received = []
    bar.advanced.connect(received.append)
    bar.reset_playback()

    assert bar.seconds == 0.0
    assert bar.playback.playing is False
    assert bar._timer.isActive() is False
    assert received == [0.0]


def test_clearing_stops_playback(loaded) -> None:
    bar, _ = loaded
    bar.toggle_playback()
    bar.clear()
    assert bar._timer.isActive() is False
    assert bar.playback is None
    assert bar.timeline is None
    assert bar.readout.text() == "No geometry"


def test_the_readout_follows_the_clock_rather_than_the_segment_end(loaded) -> None:
    """Reporting the end of the segment in progress would tick the clock forward in uneven jumps of
    whatever the current move happens to take."""
    bar, _ = loaded
    bar.toggle_playback()
    bar._tick(1.0)
    assert bar.readout.text().startswith(format_scrub_time(1.0))


def test_the_transport_never_touches_a_program_without_one(bar) -> None:
    """Every entry point is reachable from a menu action before a file is open."""
    bar.toggle_playback()
    bar.reset_playback()
    bar._tick(1.0)
    assert bar.seconds == 0.0
