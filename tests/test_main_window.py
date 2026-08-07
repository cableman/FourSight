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


def test_opening_a_file_draws_it_and_titles_the_window(window) -> None:
    assert window.open_file(FIXTURES / "baseline_4axis.nc") is True
    assert window.program is not None
    assert window.viewport.batches, "the viewport has no geometry after a successful open"
    assert "baseline_4axis.nc" in window.windowTitle()


def test_the_status_bar_summarizes_the_program(window) -> None:
    window.open_file(FIXTURES / "baseline_4axis.nc")
    message = window.statusBar().currentMessage()
    assert "blocks" in message and "segments" in message


def test_reload_is_disabled_until_a_file_is_open(window) -> None:
    assert window.reload_action.isEnabled() is False
    window.open_file(FIXTURES / "baseline_4axis.nc")
    assert window.reload_action.isEnabled() is True


def test_reload_rereads_the_file_from_disk(window, tmp_path: Path) -> None:
    """An edit made in another editor must be picked up, not served from the parsed copy."""
    path = tmp_path / "p.nc"
    path.write_text("G21 G90 G94\nG1 X10 F600\n", encoding="utf-8")
    window.open_file(path)
    before = len(window.program.simulation.store)

    path.write_text("G21 G90 G94\nG1 X10 F600\nG1 X20\nG1 X30\n", encoding="utf-8")
    window.reload()
    assert len(window.program.simulation.store) > before


# --------------------------------------------------------------------------- failure paths


def test_a_failed_open_keeps_the_previous_program_on_screen(window) -> None:
    """The behaviour that matters most on this path, and the one a user would not notice was wrong."""
    window.open_file(FIXTURES / "baseline_4axis.nc")
    kept = window.program
    batches = list(window.viewport.batches)

    assert window.open_file(Path("definitely-not-here.nc")) is False
    assert window.program is kept, "a failed open replaced the loaded program"
    assert window.viewport.batches == batches, "a failed open disturbed the drawn geometry"
    assert "baseline_4axis.nc" in window.windowTitle(), (
        "the title now names a file that failed to open"
    )


def test_a_failed_open_says_what_is_still_being_shown(window) -> None:
    window.open_file(FIXTURES / "baseline_4axis.nc")
    window.open_file(Path("definitely-not-here.nc"))
    message = window.statusBar().currentMessage()
    assert "Could not open" in message and "baseline_4axis.nc" in message


def test_a_failed_open_with_nothing_loaded_does_not_claim_to_show_a_file(window) -> None:
    assert window.open_file(Path("definitely-not-here.nc")) is False
    assert "nothing" in window.statusBar().currentMessage()


def test_a_binary_file_is_refused_rather_than_parsed(window, tmp_path: Path) -> None:
    """A mistakenly opened STL must not become thousands of meaningless diagnostics."""
    path = tmp_path / "model.nc"
    path.write_bytes(bytes(range(256)) * 40)
    assert window.open_file(path) is False


def test_the_wait_cursor_is_always_restored(window) -> None:
    """A stuck hourglass makes a working application look hung, on both paths."""
    window.open_file(FIXTURES / "baseline_4axis.nc")
    assert QApplication.overrideCursor() is None
    window.open_file(Path("definitely-not-here.nc"))
    assert QApplication.overrideCursor() is None


# --------------------------------------------------------------------------- the banner


def test_the_banner_appears_when_geometry_is_missing(window) -> None:
    """A canned cycle is not drawn, so the viewer is looking at an incomplete toolpath."""
    window.open_file(FIXTURES / "canned_cycle_span.nc")
    assert window.program.summary.incomplete is True
    assert window.banner.isVisibleTo(window) is True
    assert "incomplete" in window.banner.text()


def test_the_banner_stays_hidden_for_merely_untrusted_geometry(window) -> None:
    """Cutter comp *is* drawn and distinctly styled, so the picture is complete.

    Raising the banner here would fire it on a large share of real programs, which is exactly how a
    warning stops being read. The warning still exists — it goes to the status bar.
    """
    window.open_file(FIXTURES / "cutter_comp_span.nc")
    assert window.program.summary.unverified
    assert window.banner.isVisibleTo(window) is False
    assert "unverified" in window.statusBar().currentMessage()


def test_the_banner_is_hidden_for_a_clean_program(window, tmp_path: Path) -> None:
    path = tmp_path / "clean.nc"
    path.write_text("G21 G90 G94 G54\nG0 Z5\nG1 X10 Y10 F600\nM30\n", encoding="utf-8")
    window.open_file(path)
    assert window.banner.isVisibleTo(window) is False


def test_a_stale_banner_does_not_survive_the_next_load(window, tmp_path: Path) -> None:
    """Hiding the label without clearing its text would let the old warning reappear."""
    window.open_file(FIXTURES / "canned_cycle_span.nc")
    assert window.banner.text()
    path = tmp_path / "clean.nc"
    path.write_text("G21 G90 G94 G54\nG0 Z5\nG1 X10 Y10 F600\nM30\n", encoding="utf-8")
    window.open_file(path)
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
    window.open_file(FIXTURES / "baseline_4axis.nc")
    window.viewport.setCameraPosition(distance=99999.0)
    window.fit_view()
    assert window.viewport.opts["distance"] < 99999.0
