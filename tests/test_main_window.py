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

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="the [gui] extra is not installed")
pytest.importorskip("pyqtgraph", reason="the [gui] extra is not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from conftest import DEFAULT_PROFILE_PATH, FIXTURES  # noqa: E402
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
