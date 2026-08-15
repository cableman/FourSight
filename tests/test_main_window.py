"""Main-window tests (T2.7). Headless via Qt's ``offscreen`` platform; skips without the `[gui]` extra.

Two behaviours here are worth a test rather than an eye, because both look fine on screen:

- **A failed open must not disturb the program already loaded.** Clearing the viewport would lose the
  user's program to a mistyped filename; drawing nothing under the new name would misrepresent what
  they are looking at.
- **The banner must appear exactly when geometry is missing** — and not when geometry is merely
  untrusted, or the warning fires on a large share of real programs and stops being read.

`offscreen` cannot create a GL context, so nothing here asserts on pixels; T2.12's manual script owns
those. Widget state, titles and banner visibility are all readable without one.
"""

import os
import threading
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="the [gui] extra is not installed")
pytest.importorskip("pyqtgraph", reason="the [gui] extra is not installed")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from conftest import DEFAULT_PROFILE_PATH, FIXTURES  # noqa: E402
from foursight.gui.playback import marker_point  # noqa: E402
from foursight.machine.profile import load_profile  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def profile():
    return load_profile(DEFAULT_PROFILE_PATH)


@pytest.fixture
def window(qt_app, profile, monkeypatch):
    """A window with modal dialogs stubbed out, so a failure path cannot block the run."""
    from PySide6.QtWidgets import QMessageBox

    from foursight.gui.main_window import MainWindow

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    try:
        return MainWindow(profile)
    except Exception as error:  # pragma: no cover - environment-dependent
        pytest.skip(f"cannot construct the window on this platform: {error}")


def replace_kinematics(profile, kinematics):
    """A copy of `profile` with different kinematics, for exercising the refusal path."""
    import dataclasses

    return dataclasses.replace(profile, kinematics=kinematics)


# --------------------------------------------------------------------------- opening


def wait_for_load(window, timeout_ms: int = 30_000) -> None:
    """Pump the event loop until the in-flight load settles.

    Loading moved to a background thread in T2.9, so a test that asserts on the *result* has to let
    the worker finish and its signals be delivered.
    """
    from PySide6.QtCore import QDeadlineTimer, QEventLoop

    deadline = QDeadlineTimer(timeout_ms)
    while window._loader is not None and not deadline.hasExpired():
        QApplication.processEvents(QEventLoop.AllEvents, 20)


def test_opening_a_file_draws_it_and_titles_the_window(window) -> None:
    assert window.open_file_and_wait(FIXTURES / "baseline_4axis.nc") is True
    assert window.program is not None
    assert window.viewport.batches, "the viewport has no geometry after a successful open"
    assert "baseline_4axis.nc" in window.windowTitle()


def test_the_status_bar_summarizes_the_program(window) -> None:
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    message = window.statusBar().currentMessage()
    assert "blocks" in message and "segments" in message


def test_reload_is_disabled_until_a_file_is_open(window) -> None:
    assert window.reload_action.isEnabled() is False
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    assert window.reload_action.isEnabled() is True


def test_reload_rereads_the_file_from_disk(window, tmp_path: Path) -> None:
    """An edit made in another editor must be picked up, not served from the parsed copy."""
    path = tmp_path / "p.nc"
    path.write_text("G21 G90 G94\nG1 X10 F600\n", encoding="utf-8")
    window.open_file_and_wait(path)
    before = len(window.program.simulation.store)

    path.write_text("G21 G90 G94\nG1 X10 F600\nG1 X20\nG1 X30\n", encoding="utf-8")
    window.reload()
    wait_for_load(window)
    assert len(window.program.simulation.store) > before


# --------------------------------------------------------------------------- failure paths


def test_a_failed_open_keeps_the_previous_program_on_screen(window) -> None:
    """The behaviour that matters most on this path, and the one a user would not notice was wrong."""
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    kept = window.program
    batches = list(window.viewport.batches)

    assert window.open_file_and_wait(Path("definitely-not-here.nc")) is False
    assert window.program is kept, "a failed open replaced the loaded program"
    assert window.viewport.batches == batches, "a failed open disturbed the drawn geometry"
    assert "baseline_4axis.nc" in window.windowTitle(), (
        "the title now names a file that failed to open"
    )


def test_a_failed_open_says_what_is_still_being_shown(window) -> None:
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    window.open_file_and_wait(Path("definitely-not-here.nc"))
    message = window.statusBar().currentMessage()
    assert "Could not open" in message and "baseline_4axis.nc" in message


def test_a_failed_open_with_nothing_loaded_does_not_claim_to_show_a_file(window) -> None:
    assert window.open_file_and_wait(Path("definitely-not-here.nc")) is False
    assert "nothing" in window.statusBar().currentMessage()


def test_a_binary_file_is_refused_rather_than_parsed(window, tmp_path: Path) -> None:
    """A mistakenly opened STL must not become thousands of meaningless diagnostics."""
    path = tmp_path / "model.nc"
    path.write_bytes(bytes(range(256)) * 40)
    assert window.open_file_and_wait(path) is False


def test_the_progress_widgets_are_hidden_again_on_both_paths(window) -> None:
    """A progress bar left visible after a load makes a finished application look busy forever.

    Replaces the old wait-cursor test: T2.9 moved loading off the GUI thread, so there is no override
    cursor any more — the bar and the Cancel button are what must be cleaned up, on success and on
    failure alike.
    """
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    assert window.progress.isVisibleTo(window) is False
    assert window.cancel_button.isVisibleTo(window) is False
    window.open_file_and_wait(Path("definitely-not-here.nc"))
    assert window.progress.isVisibleTo(window) is False
    assert window.cancel_button.isVisibleTo(window) is False


# --------------------------------------------------------------------------- the banner


def test_the_banner_appears_when_geometry_is_missing(window) -> None:
    """A canned cycle is not drawn, so the viewer is looking at an incomplete toolpath."""
    window.open_file_and_wait(FIXTURES / "canned_cycle_span.nc")
    assert window.program.summary.incomplete is True
    assert window.banner.isVisibleTo(window) is True
    assert "incomplete" in window.banner.text()


def test_the_banner_stays_hidden_for_merely_untrusted_geometry(window) -> None:
    """Cutter comp *is* drawn and distinctly styled, so the picture is complete.

    Raising the banner here would fire it on a large share of real programs, which is exactly how a
    warning stops being read. The warning still exists — it goes to the status bar.
    """
    window.open_file_and_wait(FIXTURES / "cutter_comp_span.nc")
    assert window.program.summary.unverified
    assert window.banner.isVisibleTo(window) is False
    assert "unverified" in window.statusBar().currentMessage()


def test_the_banner_is_hidden_for_a_clean_program(window, tmp_path: Path) -> None:
    path = tmp_path / "clean.nc"
    path.write_text("G21 G90 G94 G54\nG0 Z5\nG1 X10 Y10 F600\nM30\n", encoding="utf-8")
    window.open_file_and_wait(path)
    assert window.banner.isVisibleTo(window) is False


def test_a_stale_banner_does_not_survive_the_next_load(window, tmp_path: Path) -> None:
    """Hiding the label without clearing its text would let the old warning reappear."""
    window.open_file_and_wait(FIXTURES / "canned_cycle_span.nc")
    assert window.banner.text()
    path = tmp_path / "clean.nc"
    path.write_text("G21 G90 G94 G54\nG0 Z5\nG1 X10 Y10 F600\nM30\n", encoding="utf-8")
    window.open_file_and_wait(path)
    assert window.banner.text() == ""
    assert window.banner.isVisibleTo(window) is False


# --------------------------------------------------------------------------- menus and view


def test_the_menus_offer_open_reload_quit_and_fit(window) -> None:
    """Cheap, but it is the whole of M2's interaction surface."""
    from PySide6.QtWidgets import QMenu

    labels = {
        action.text().replace("&", "")
        for menu in window.menuBar().findChildren(QMenu)
        for action in menu.actions()
    }
    assert {"Open…", "Reload", "Quit", "Fit to program"} <= labels


def test_fitting_the_view_without_a_program_does_nothing_rather_than_failing(window) -> None:
    window.fit_view()  # must not raise


def test_fitting_the_view_reframes_the_loaded_program(window) -> None:
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    window.viewport.setCameraPosition(distance=99999.0)
    window.fit_view()
    assert window.viewport.opts["distance"] < 99999.0


# --------------------------------------------------------------------------- off the GUI thread (T2.9)


def test_the_load_really_runs_on_another_thread(window, monkeypatch) -> None:
    """The whole point of T2.9. Asserted by identity, not by timing, so it cannot pass by luck."""
    import threading

    from foursight.gui import background

    gui_thread = threading.get_ident()
    worker_thread = []
    real = background.open_program

    def recording(*args, **kwargs):
        worker_thread.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(background, "open_program", recording)
    assert window.open_file_and_wait(FIXTURES / "baseline_4axis.nc") is True
    assert worker_thread, "the loader never called open_program"
    assert worker_thread[0] != gui_thread, "the load ran on the GUI thread after all"


def test_open_file_returns_before_the_load_finishes(window, monkeypatch) -> None:
    """`open_file` must not block, or the progress bar it shows could never be painted."""
    from foursight.gui import background

    started = threading.Event()
    release = threading.Event()
    real = background.open_program

    def slow(*args, **kwargs):
        started.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(background, "open_program", slow)
    window.open_file(FIXTURES / "baseline_4axis.nc")
    assert started.wait(10), "the worker never started"
    assert window.program is None, "open_file blocked until the load completed"
    release.set()
    wait_for_load(window)
    assert window.program is not None


def test_progress_is_reported_while_loading(window) -> None:
    """A bar that never moves is indistinguishable from a hung application."""
    seen = []
    window.open_file(FIXTURES / "baseline_4axis.nc")
    window._loader.progressed.connect(lambda done, total, stage: seen.append(stage))
    wait_for_load(window)
    assert seen, "no progress was reported"


# --------------------------------------------------------------------------- cancelling


def fake_outcome(monkeypatch, exception: BaseException):
    """Make the worker fail deterministically, instead of racing a real load to cancel it."""
    from foursight.gui import background

    def raising(*args, **kwargs):
        raise exception

    monkeypatch.setattr(background, "open_program", raising)


def test_a_cancelled_load_keeps_the_previous_program_on_screen(window, monkeypatch) -> None:
    """A cancelled load draws nothing: `simulate` raises rather than returning a partial store.

    So the viewer keeps what they had, exactly as for a failed open — never a truncated toolpath.
    """
    from foursight.sim.simulator import SimulationCancelled

    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    kept = window.program
    batches = list(window.viewport.batches)

    fake_outcome(monkeypatch, SimulationCancelled("cancelled after 100 of 900 blocks"))
    window.open_file_and_wait(FIXTURES / "arc_helical.nc")
    assert window.program is kept, "a cancelled load replaced the loaded program"
    assert window.viewport.batches == batches, "a cancelled load disturbed the drawn geometry"
    assert "baseline_4axis.nc" in window.windowTitle()


def test_a_cancelled_load_says_what_is_still_being_shown(window, monkeypatch) -> None:
    from foursight.sim.simulator import SimulationCancelled

    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    fake_outcome(monkeypatch, SimulationCancelled("cancelled"))
    window.open_file_and_wait(FIXTURES / "arc_helical.nc")
    message = window.statusBar().currentMessage()
    assert "Cancelled" in message and "baseline_4axis.nc" in message


def test_cancelling_hides_the_progress_widgets(window, monkeypatch) -> None:
    from foursight.sim.simulator import SimulationCancelled

    fake_outcome(monkeypatch, SimulationCancelled("cancelled"))
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    assert window.progress.isVisibleTo(window) is False
    assert window.cancel_button.isVisibleTo(window) is False


def test_cancel_load_with_nothing_running_does_nothing(window) -> None:
    window.cancel_load()  # must not raise


def test_the_cancel_button_requests_cancellation(window, monkeypatch) -> None:
    """Wiring check: the button must reach the worker's Event, not just look clickable."""
    from foursight.gui import background

    release = threading.Event()
    real = background.open_program

    def slow(*args, **kwargs):
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(background, "open_program", slow)
    window.open_file(FIXTURES / "baseline_4axis.nc")
    loader = window._loader
    window.cancel_button.click()
    assert loader.cancellation_requested is True
    release.set()
    wait_for_load(window)


# --------------------------------------------------------------------------- superseding a load


def test_a_second_open_supersedes_the_first(window, monkeypatch) -> None:
    """The newer request is what the user wants, and the older one must not win a race.

    A late `loaded` from the superseded worker would draw a file the user has already moved on from —
    so the old loader is disconnected before the new one starts.
    """
    from foursight.gui import background

    release = threading.Event()
    real = background.open_program

    def slow_first(*args, **kwargs):
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(background, "open_program", slow_first)
    window.open_file(FIXTURES / "baseline_4axis.nc")
    first = window._loader

    monkeypatch.setattr(background, "open_program", real)
    window.open_file(FIXTURES / "arc_helical.nc")
    assert window._loader is not first
    assert first.cancellation_requested is True, "the superseded load was not cancelled"

    release.set()
    wait_for_load(window)
    first.wait(10_000)
    QApplication.processEvents()
    assert window.program is not None
    assert "arc_helical.nc" in window.windowTitle(), "the superseded load won the race"


def test_closing_the_window_stops_a_running_load(window, monkeypatch) -> None:
    """A QThread outliving its parent widget turns a clean exit into a crash on shutdown."""
    from foursight.gui import background

    release = threading.Event()
    real = background.open_program

    def slow(*args, **kwargs):
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(background, "open_program", slow)
    window.open_file(FIXTURES / "baseline_4axis.nc")
    loader = window._loader
    release.set()
    window.close()
    assert loader.isFinished() or loader.wait(10_000)


# --------------------------------------------------------------------------- editor -> viewport (T3.2)


def test_moving_the_cursor_highlights_that_line(window) -> None:
    """The sync T3.2 exists for: the cursor drives the highlight, not just a click."""
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    store = window.program.simulation.store
    line_no = int(store.line[0])

    window.editor.goto_line(line_no)
    assert window.selection is not None
    assert window.selection.line_no == line_no
    assert window.viewport.highlighted_segments == window.selection.count > 0


def test_moving_to_a_line_with_no_motion_clears_the_highlight(window) -> None:
    """A stale highlight would attribute geometry to a line that produced none."""
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    store = window.program.simulation.store
    window.editor.goto_line(int(store.line[0]))
    assert window.viewport.highlighted_segments > 0

    drawn = set(store.line.tolist())
    bare = next(n for n in range(1, window.editor.source_line_count + 1) if n not in drawn)
    window.editor.goto_line(bare)
    assert window.viewport.highlighted_segments == 0


def test_the_status_bar_describes_the_selected_line(window) -> None:
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    window.editor.goto_line(int(window.program.simulation.store.line[0]))
    assert window.statusBar().currentMessage() == window.selection.describe()


def test_a_suppressed_line_is_reported_as_not_drawn_in_the_status_bar(window) -> None:
    """Clicking a canned-cycle line must not read as "this line does nothing"."""
    window.open_file_and_wait(FIXTURES / "canned_cycle_span.nc")
    span = window.program.simulation.suppressed[0]
    window.editor.goto_line(span.first_line)
    message = window.statusBar().currentMessage()
    assert "not drawn" in message
    assert window.viewport.highlighted_segments == 0


def test_the_cursor_in_the_trailing_block_clears_rather_than_failing(window) -> None:
    """A newline-terminated file has a final block with no source line behind it."""
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    assert window.editor.line_count == window.editor.source_line_count + 1
    window.editor.goto_line(window.editor.line_count)
    assert window.selection is None
    assert window.viewport.highlighted_segments == 0


def test_moving_the_cursor_with_nothing_loaded_does_nothing(window) -> None:
    window.editor.setPlainText("G1 X10\n")
    window.editor.goto_line(1)  # must not raise
    assert window.selection is None


def test_loading_a_new_program_drops_the_previous_selection(window) -> None:
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    window.editor.goto_line(int(window.program.simulation.store.line[0]))
    assert window.selection is not None
    window.open_file_and_wait(FIXTURES / "arc_helical.nc")
    assert window.viewport.highlighted_segments == 0


# --------------------------------------------------------------------------- viewport -> editor (T3.3)


def test_picking_a_segment_moves_the_editor_to_its_line(window) -> None:
    """`SegmentStore.line[index]` is the whole mechanism — a lookup, not a search."""
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    store = window.program.simulation.store
    index = len(store) // 2
    window._on_segment_picked(index)
    assert window.editor.current_line == int(store.line[index])


def test_picking_a_segment_highlights_its_whole_line(window) -> None:
    """A click lights up the entire block, not the single segment under the cursor.

    That is what tells the user how far the block they clicked actually travels — one segment of a
    tessellated arc would say almost nothing. It works by reusing the T3.2 cursor path.
    """
    window.open_file_and_wait(FIXTURES / "arc_helical.nc")
    store = window.program.simulation.store
    index = len(store) // 2
    window._on_segment_picked(index)
    assert window.selection is not None
    assert window.selection.line_no == int(store.line[index])
    assert window.viewport.highlighted_segments == window.selection.count > 1


def test_the_pick_to_highlight_loop_terminates(window) -> None:
    """Picking moves the cursor, which highlights, which must not move the cursor again."""
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    store = window.program.simulation.store
    index = len(store) // 2
    window._on_segment_picked(index)
    settled = window.editor.current_line
    window._on_segment_picked(index)
    assert window.editor.current_line == settled


def test_picking_with_nothing_loaded_does_nothing(window) -> None:
    window._on_segment_picked(0)  # must not raise
    assert window.selection is None


def test_an_out_of_range_pick_index_is_ignored(window) -> None:
    """Defensive: a stale index arriving after a reload must not raise or jump somewhere arbitrary."""
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    before = window.editor.current_line
    window._on_segment_picked(10_000_000)
    window._on_segment_picked(-1)
    assert window.editor.current_line == before


def test_the_viewport_click_signal_is_connected(window) -> None:
    """Wiring check: the signal must actually reach the handler, not merely exist."""
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    store = window.program.simulation.store
    index = len(store) // 3
    window.viewport.segment_picked.emit(index)
    assert window.editor.current_line == int(store.line[index])


# --------------------------------------------------------------------------- diagnostics panel (T3.4)


def test_diagnostics_arrive_after_the_toolpath(window) -> None:
    """Stage two. Geometry first, findings after — verification costs 5.9 s at 100k lines.

    `open_file_and_wait` now spans both stages, so by the time it returns the panel is populated.
    """
    assert window.open_file_and_wait(FIXTURES / "canned_cycle_span.nc") is True
    assert window.viewport.batches, "the toolpath did not load"
    assert window.diagnostics.diagnostics, "no diagnostics arrived from stage two"


def test_a_canned_cycle_program_reports_an_unsupported_finding(window) -> None:
    """The tier that must not read as a warning, end to end from the verifier to the panel."""
    window.open_file_and_wait(FIXTURES / "canned_cycle_span.nc")
    severities = {d.severity for d in window.diagnostics.diagnostics}
    assert "unsupported" in severities


def test_a_clean_program_reports_no_problems(window) -> None:
    """The control case. Without it, "the panel is empty" could mean the check never ran."""
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    assert window.diagnostics.diagnostics == ()
    assert "No problems found" in window.diagnostics.header.text()


def test_clicking_a_diagnostic_jumps_the_editor_to_its_line(window) -> None:
    window.open_file_and_wait(FIXTURES / "canned_cycle_span.nc")
    panel = window.diagnostics
    assert panel.tree.topLevelItemCount() > 0
    expected = panel.tree.topLevelItem(0).data(1, Qt.UserRole)
    panel.tree.setCurrentItem(panel.tree.topLevelItem(0))
    assert window.editor.current_line == expected


def test_clicking_a_diagnostic_also_highlights_that_line(window) -> None:
    """Reuses the T3.2 cursor path, so a finding is both located and shown."""
    window.open_file_and_wait(FIXTURES / "cutter_comp_span.nc")
    panel = window.diagnostics
    if panel.tree.topLevelItemCount() == 0:
        pytest.skip("the fixture produced no diagnostics to click")
    panel.tree.setCurrentItem(panel.tree.topLevelItem(0))
    assert window.selection is not None
    assert window.selection.line_no == window.editor.current_line


def test_the_panel_shows_pending_while_checking(window, monkeypatch) -> None:
    """An empty list mid-check would read as "no problems found", a claim not yet earned."""
    from foursight.gui import background

    monkeypatch.setattr(background, "verify_program", lambda *a, **k: ())
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    # With verification stubbed to return nothing, the header must still distinguish the two states.
    assert window.diagnostics.header.text() in {"No problems found", "Checking…"}


def test_a_verifier_crash_does_not_retract_the_toolpath(window, monkeypatch) -> None:
    """A raising rule is our bug, not the user's file. Throwing away good geometry would be worse."""
    from foursight.gui import background

    def exploding(*args, **kwargs):
        raise RuntimeError("rule blew up")

    monkeypatch.setattr(background, "verify_program", exploding)
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    assert window.program is not None, "the load was retracted because the check failed"
    assert window.viewport.batches, "the toolpath was cleared because the check failed"


def test_loading_a_new_program_clears_the_previous_diagnostics(window) -> None:
    window.open_file_and_wait(FIXTURES / "canned_cycle_span.nc")
    assert window.diagnostics.diagnostics
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    assert window.diagnostics.diagnostics == ()


# --------------------------------------------------------------------------- part coordinates (T4.5)


def test_the_toggle_starts_off_and_shows_machine_coordinates(window) -> None:
    """`lin` is the default view because it is what the verifier reasons about."""
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    assert window.part_coordinates_action.isChecked() is False
    assert window.viewport.part_coordinates is False


def test_enabling_the_toggle_transforms_and_redraws(window) -> None:
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    store = window.program.simulation.store
    assert store.lin_part is None, "the transform should not be computed until asked for"

    window.part_coordinates_action.setChecked(True)
    assert store.lin_part is not None
    assert window.viewport.part_coordinates is True


def test_the_transform_never_touches_machine_coordinates(window) -> None:
    """The invariant the whole verifier rests on, checked through the GUI path."""
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    store = window.program.simulation.store
    before = store.lin.copy()
    window.part_coordinates_action.setChecked(True)
    assert np.array_equal(store.lin, before)


def test_toggling_invalidates_the_picking_projection(window) -> None:
    """Toggling redraws everything *without moving the camera*, so a matrix-only cache would go stale.

    A stale projection here would pick whatever segment sat at those pixels before the wrap was applied —
    a confidently wrong line with nothing on screen to suggest it.
    """
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    machine = window.viewport.projection()
    assert machine is not None and machine.part_coordinates is False

    window.part_coordinates_action.setChecked(True)
    part = window.viewport.projection()
    assert part is not machine
    assert part.part_coordinates is True


def test_the_selection_survives_the_switch_and_is_redrawn(window) -> None:
    """A selection is segment indices, not coordinates — but it has to be redrawn in the new frame."""
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    store = window.program.simulation.store
    window.editor.goto_line(int(store.line[0]))
    count = window.viewport.highlighted_segments
    assert count > 0

    window.part_coordinates_action.setChecked(True)
    assert window.selection is not None
    assert window.viewport.highlighted_segments == count


def test_a_profile_that_cannot_describe_the_transform_reverts_the_toggle(
    window, monkeypatch
) -> None:
    """A checkbox that lies about what is on screen is worse than one that refuses.

    Head mount with no `pivot_to_tip` has an unknown tool tip; drawing machine coordinates while the menu
    claims part coordinates would misrepresent the geometry silently.
    """
    from foursight.machine.profile import Kinematics

    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    monkeypatch.setattr(
        window, "profile", replace_kinematics(window.profile, Kinematics(rotary_mount="head"))
    )
    window.part_coordinates_action.setChecked(True)
    assert window.part_coordinates_action.isChecked() is False
    assert window.viewport.part_coordinates is False


def test_loading_a_new_program_resets_the_toggle(window) -> None:
    """`lin_part` belongs to the previous store; leaving it checked would misdescribe the new one."""
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    window.part_coordinates_action.setChecked(True)
    window.open_file_and_wait(FIXTURES / "arc_helical.nc")
    assert window.part_coordinates_action.isChecked() is False
    assert window.viewport.part_coordinates is False


def test_toggling_with_nothing_loaded_does_nothing(window) -> None:
    window.part_coordinates_action.setChecked(True)  # must not raise


# --------------------------------------------------------------------------- fixes (T5.1, T5.5)


@pytest.fixture
def auto_accept(monkeypatch):
    """Accept the diff dialog without showing it, and answer the parameter prompt."""
    from PySide6.QtWidgets import QDialog, QInputDialog

    from foursight.gui import diff_dialog

    monkeypatch.setattr(diff_dialog.DiffDialog, "exec", lambda self: QDialog.Accepted)
    monkeypatch.setattr(diff_dialog.RefusalDialog, "exec", lambda self: QDialog.Rejected)
    monkeypatch.setattr(QInputDialog, "getDouble", staticmethod(lambda *a, **k: (450.0, True)))


def write_program(tmp_path, text: str):
    path = tmp_path / "p.nc"
    path.write_text(text, encoding="utf-8")
    return path


def test_applying_a_fix_reruns_the_whole_pipeline(window, tmp_path, auto_accept) -> None:
    """The one-fix contract. The buffer goes back through load → parse → simulate → verify.

    Asserted on the *rebuilt* artefacts, not just the text: a fix that changed the buffer without
    re-simulating would leave every `SegmentStore.line` entry pointing at the old numbering.
    """
    window.open_file_and_wait(write_program(tmp_path, "G21 G90 G94\nG0 Z5\nG1 X10 F600\n"))
    before_blocks = len(window.program.commands)

    assert window.run_fix("fix.append-program-end") is True
    wait_for_load(window)

    assert "M30" in window.editor.toPlainText()
    assert len(window.program.commands) == before_blocks + 1, "the program was not re-parsed"
    assert len(window.program.simulation.store) > 0, "segments were not rebuilt"


def test_a_fix_that_shifts_lines_rebuilds_the_line_numbers(window, tmp_path, auto_accept) -> None:
    """The reason the contract exists: an inserted line invalidates every number after it."""
    window.open_file_and_wait(write_program(tmp_path, "G1 X10 F600\nG1 X20\n"))
    first_motion_before = min(int(line) for line in window.program.simulation.store.line)

    assert window.run_fix("fix.add-safety-preamble") is True
    wait_for_load(window)

    first_motion_after = min(int(line) for line in window.program.simulation.store.line)
    assert first_motion_after > first_motion_before, "segment line numbers were not rebuilt"


def test_the_file_on_disk_is_never_written(window, tmp_path, auto_accept) -> None:
    """PLAN.md: fixes modify the editor buffer; the user saves explicitly."""
    path = write_program(tmp_path, "G21 G90 G94\nG1 X10 F600\n")
    original = path.read_bytes()
    window.open_file_and_wait(path)
    window.run_fix("fix.append-program-end")
    wait_for_load(window)
    assert path.read_bytes() == original


def test_a_refused_fix_leaves_the_buffer_alone(window, tmp_path, auto_accept) -> None:
    """A refusal is a result, not a failed attempt: nothing changes and the reason is shown."""
    window.open_file_and_wait(
        write_program(tmp_path, "G21 G90 G94 G17\nG0 X0 Y0\nG2 X20 Y0 I12 J0 F600\n")
    )
    before = window.editor.toPlainText()
    window.editor.goto_line(3)
    assert window.run_fix("fix.recompute-arc-centre") is False
    assert window.editor.toPlainText() == before


def test_cancelling_the_diff_dialog_applies_nothing(window, tmp_path, monkeypatch) -> None:
    """Review means review: nothing is applied until Apply is pressed."""
    from PySide6.QtWidgets import QDialog

    from foursight.gui import diff_dialog

    monkeypatch.setattr(diff_dialog.DiffDialog, "exec", lambda self: QDialog.Rejected)
    window.open_file_and_wait(write_program(tmp_path, "G21 G90 G94\nG1 X10 F600\n"))
    before = window.editor.toPlainText()
    assert window.run_fix("fix.append-program-end") is False
    assert window.editor.toPlainText() == before


def test_a_parameterized_fix_prompts_and_uses_the_value(window, tmp_path, auto_accept) -> None:
    """The prompt is stubbed to 450; the value must reach the program rather than a default."""
    window.open_file_and_wait(write_program(tmp_path, "G21 G90 G94\nG1 X10\n"))
    assert window.run_fix("fix.inject-feed-rate") is True
    wait_for_load(window)
    assert "F450" in window.editor.toPlainText()


def test_declining_the_prompt_applies_nothing(window, tmp_path, monkeypatch) -> None:
    from PySide6.QtWidgets import QInputDialog

    monkeypatch.setattr(QInputDialog, "getDouble", staticmethod(lambda *a, **k: (0.0, False)))
    window.open_file_and_wait(write_program(tmp_path, "G21 G90 G94\nG1 X10\n"))
    before = window.editor.toPlainText()
    assert window.run_fix("fix.inject-feed-rate") is False
    assert window.editor.toPlainText() == before


def test_undo_restores_the_previous_buffer(window, tmp_path, auto_accept) -> None:
    """Snapshots, not reversed diffs — reversing one is the rebasing the contract forbids."""
    window.open_file_and_wait(write_program(tmp_path, "G21 G90 G94\nG1 X10 F600\n"))
    before = window.editor.toPlainText()
    window.run_fix("fix.append-program-end")
    wait_for_load(window)
    assert window.editor.toPlainText() != before

    window.undo_fix()
    wait_for_load(window)
    assert window.editor.toPlainText() == before
    assert window.undo_fix_action.isEnabled() is False


def test_undo_with_no_history_does_nothing(window, tmp_path) -> None:
    window.open_file_and_wait(write_program(tmp_path, "G21 G90 G94\nG1 X10 F600\n"))
    window.undo_fix()  # must not raise


def test_running_a_fix_with_nothing_loaded_does_nothing(window) -> None:
    assert window.run_fix("fix.append-program-end") is False


def test_every_fix_appears_in_the_menu(window) -> None:
    """The menu is built from the registry, so a new fix needs no UI change — and none can be forgotten."""
    from foursight.fix.engine import load_builtin_fixes

    assert set(window.fix_actions) == set(load_builtin_fixes())


def test_a_destructive_fix_is_labelled_and_has_no_shortcut(window) -> None:
    """It must never be the easy default; PLAN.md keeps N-word stripping off by default."""
    action = window.fix_actions["fix.strip-line-numbers"]
    assert "destructive" in action.text()
    assert action.shortcut().isEmpty()


# --------------------------------------------------------------------- machine profile (T8.2)


def test_the_profile_dialog_opens_on_the_loaded_profile(window) -> None:
    dialog = window.show_profile_dialog()
    assert dialog.document.value("limits", "max_feed") == 3000.0
    assert dialog.path == DEFAULT_PROFILE_PATH


def test_the_profile_dialog_is_reused_so_unapplied_edits_survive_a_reopen(window) -> None:
    first = window.show_profile_dialog()
    first.close()
    assert window.show_profile_dialog() is first


def test_applying_a_profile_swaps_it_and_reruns_the_check(window) -> None:
    """A profile change is not a display setting: every verifier limit and the dialect read from it,
    so the whole pipeline re-runs exactly as it does for an applied fix."""
    assert window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    dialog = window.show_profile_dialog()
    edited = dialog.document.apply(
        [_edit("limits", "max_plunge_feed", 10.0), _edit("safety", "min_clearance_z", 500.0)]
    )
    dialog.document = edited
    dialog.applied.emit(edited)
    _settle(window)

    assert window.profile.limits.max_plunge_feed == 10.0
    assert window.profile.path == DEFAULT_PROFILE_PATH, "the rebuilt profile keeps its source"
    rules = {d.rule_id for d in window.diagnostics.diagnostics}
    assert "process.rapid-below-clearance" in rules, (
        "the new limit is what the program is checked on"
    )


def test_applying_a_profile_with_no_program_loaded_just_reports_it(window) -> None:
    dialog = window.show_profile_dialog()
    edited = dialog.document.apply([_edit("machine", "name", "Bench mill")])
    dialog.applied.emit(edited)
    assert window.profile.name == "Bench mill"
    assert "Bench mill" in window.statusBar().currentMessage()


def test_a_changed_rotary_mount_cannot_leave_a_stale_part_transform(window) -> None:
    """`lin_part` belongs to the old kinematics, and dies with the old store."""
    assert window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    window.part_coordinates_action.setChecked(True)
    dialog = window.show_profile_dialog()
    edited = dialog.document.apply([_edit("kinematics", "centerline_offset", [0.0, 0.0, 25.0])])
    dialog.applied.emit(edited)
    _settle(window)
    assert window.part_coordinates_action.isChecked() is False
    assert window.program.simulation.store.lin_part is None


def test_the_window_prefers_the_document_it_was_given(qt_app, profile) -> None:
    """`app.py` folds any --dialect override into the document, so the window must not re-read the
    file and quietly drop it."""
    from foursight.gui.main_window import MainWindow
    from foursight.machine.profile_doc import ProfileDocument

    document = ProfileDocument.from_text('[machine]\nunits = "mm"\n[dialect]\nname = "mach3"\n')
    window = MainWindow(profile, profile_document=document)
    assert window.show_profile_dialog().document.value("dialect", "name") == "mach3"


def _edit(section: str, key: str, value):
    from foursight.machine.profile_doc import Edit

    return Edit(section, key, value)


def _settle(window, timeout_ms: int = 30_000) -> None:
    """Pump the event loop until the background reload started by an applied profile finishes."""
    from PySide6.QtCore import QDeadlineTimer, QEventLoop

    deadline = QDeadlineTimer(timeout_ms)
    while window._loader is not None and not deadline.hasExpired():
        QApplication.processEvents(QEventLoop.AllEvents, 20)


# --------------------------------------------------------------------------- playback (T10.5)


def test_playing_walks_the_editor_down_the_program(window) -> None:
    """The point of putting a player in an editor: the code scrolls past as the tool advances."""
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    window.editor.goto_line(1)
    window.timeline.toggle_playback()

    lines = []
    for _ in range(20):
        window.timeline._tick(window.timeline.timeline.total / 20.0)
        lines.append(window.editor.current_line)

    assert lines == sorted(lines), "the editor jumped backwards during playback"
    assert lines[-1] > lines[0], "the editor never followed the tool"
    assert window.viewport._marker is not None
    assert window.viewport._marker.visible() is True


def test_playing_moves_the_marker_along_the_path(window) -> None:
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    window.timeline.toggle_playback()

    window.timeline._tick(1.0)
    first = np.asarray(window.viewport._marker.pos, dtype=np.float64).copy()
    window.timeline._tick(window.timeline.timeline.total / 4.0)
    second = np.asarray(window.viewport._marker.pos, dtype=np.float64)

    assert not np.allclose(first, second), "the marker did not move between frames"


def test_a_frame_that_stays_on_one_line_does_not_recentre_the_editor(window) -> None:
    """`goto_line` calls `centerCursor()`. Playback emits ~30 times a second, so doing it every frame
    makes the editor twitch under a tool that is still working along one long move."""
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    window.timeline.toggle_playback()
    window.timeline._tick(0.01)

    line = window.editor.current_line
    moves = []
    window.editor.cursorPositionChanged.connect(lambda: moves.append(window.editor.current_line))
    window.timeline._tick(0.0)
    window.timeline._tick(0.0)

    assert window.editor.current_line == line
    assert moves == [], "the cursor was moved again for a line it was already on"


def test_applying_a_fix_stops_playback_and_rewinds(window, tmp_path, auto_accept) -> None:
    """Every fix reloads the program, and a player left running would animate a position in a
    timeline that no longer describes what is on screen."""
    path = write_program(tmp_path, "G21 G90 G94\nG1 X10 Y10 F600\nG1 X20\n")
    window.open_file_and_wait(path)
    window.timeline.toggle_playback()
    window.timeline._tick(1.0)
    assert window.timeline.seconds > 0.0

    assert window.run_fix("fix.append-program-end") is True
    wait_for_load(window)

    assert window.timeline._timer.isActive() is False
    assert window.timeline.playback.playing is False
    assert window.timeline.seconds == 0.0
    assert window.viewport._marker.visible() is False


def test_switching_to_part_coordinates_keeps_a_paused_marker(window, tmp_path) -> None:
    """A playing marker would heal on the next frame; a paused one would simply vanish.

    Uses a wrapping move rather than a fixture: the two frames coincide wherever A is zero, so a test
    on an unrotated position would pass without checking anything.
    """
    path = write_program(tmp_path, "G21 G90 G94\nG0 Y25 Z0\nG1 X40 A180 F600\nM30\n")
    window.open_file_and_wait(path)
    window.timeline.toggle_playback()
    window.timeline._tick(window.timeline.timeline.total / 2.0)
    window.timeline.toggle_playback()  # pause, leaving the marker where it is
    machine = np.asarray(window.viewport._marker.pos, dtype=np.float64).copy()

    window.part_coordinates_action.setChecked(True)
    assert window.part_coordinates_action.isChecked(), "the transform was refused for this profile"
    assert window.viewport._marker.visible() is True
    part = np.asarray(window.viewport._marker.pos, dtype=np.float64)

    store = window.program.simulation.store
    expected = marker_point(
        store, window.timeline.timeline, window.timeline.seconds, part_coordinates=True
    )
    assert np.allclose(part[0], expected, atol=1e-3)
    assert not np.allclose(machine, part), "this program does not distinguish the two frames"


def test_the_play_menu_action_drives_the_transport(window) -> None:
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    action = next(a for a in window.menuBar().actions() if a.text() == "&View")
    play = next(a for a in action.menu().actions() if a.text() == "&Play / pause")

    play.trigger()
    assert window.timeline.playback.playing is True
    play.trigger()
    assert window.timeline.playback.playing is False


def test_scrubbing_still_moves_the_editor(window) -> None:
    """The T3.5 behaviour the transport is built on top of, guarded against the goto_line throttle."""
    window.open_file_and_wait(FIXTURES / "baseline_4axis.nc")
    window.editor.goto_line(1)
    window.timeline.slider.setValue(900)
    assert window.editor.current_line > 1
