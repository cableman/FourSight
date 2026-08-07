"""Main window: menus, file open, and the viewport. Thin by design — logic lives in `session.py`.

M2's scope is exactly "open a file, see the toolpath, orbit/pan/zoom". The editor, diagnostics panel
and timeline are M3 and M4, and are deliberately absent rather than stubbed in.

One thing here is not cosmetic. When the simulator **suppresses** geometry — a canned cycle it will
not draw as a straight line through the hole positions — the viewer is looking at an incomplete
toolpath, and a status-bar line is too easy to miss for something that changes what the picture means.
So a banner appears above the viewport and stays until a program loads without suppression. PLAN.md:
*"A previewer that refuses to draw is recoverable; one that draws the wrong path is worse than no
previewer"* — a refusal nobody notices forfeits that.

Simulation runs on the GUI thread here, so a 100k-line file freezes the window for several seconds.
That is T2.9's job; until then the wait cursor at least says the application is working rather than
hung.
"""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from foursight.fileio.loader import FileLoadError
from foursight.gui.session import OpenedProgram, open_program
from foursight.gui.viewport3d import ToolpathViewport
from foursight.machine.profile import MachineProfile

GCODE_FILTER = "G-code (*.nc *.ngc *.gcode *.tap *.cnc);;All files (*)"
_BANNER_STYLE = "background: #7a2a12; color: #ffe9c9; padding: 5px 9px; font-weight: 600;"


class MainWindow(QMainWindow):
    """Open a G-code file and look at its toolpath."""

    def __init__(self, profile: MachineProfile, *, block_delete: bool = False) -> None:
        super().__init__()
        self.profile = profile
        self.block_delete = block_delete
        self.program: OpenedProgram | None = None
        self._last_directory = str(Path.home())

        self.setWindowTitle("FourSight")
        self.resize(1280, 800)

        self.viewport = ToolpathViewport()
        self.banner = QLabel()
        self.banner.setStyleSheet(_BANNER_STYLE)
        self.banner.setWordWrap(True)
        self.banner.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.banner.hide()

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.banner)
        layout.addWidget(self.viewport, stretch=1)
        self.setCentralWidget(container)

        self._build_menus()
        self.statusBar().showMessage("Open a G-code file to begin  (Ctrl+O)")

    # ------------------------------------------------------------------ menus

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self._add(file_menu, "&Open…", QKeySequence.Open, self.prompt_for_file)
        self.reload_action = self._add(file_menu, "&Reload", QKeySequence.Refresh, self.reload)
        self.reload_action.setEnabled(False)
        file_menu.addSeparator()
        self._add(file_menu, "&Quit", QKeySequence.Quit, self.close)

        view_menu = self.menuBar().addMenu("&View")
        self._add(view_menu, "&Fit to program", QKeySequence("Ctrl+0"), self.fit_view)

    def _add(self, menu, text: str, shortcut, slot) -> QAction:
        action = QAction(text, self)
        if shortcut is not None:
            action.setShortcut(shortcut)
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    # ------------------------------------------------------------------ opening

    def prompt_for_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open G-code", self._last_directory, GCODE_FILTER
        )
        if path:
            self.open_file(path)

    def open_file(self, path: str | Path) -> bool:
        """Load, simulate and draw ``path``. Returns whether it succeeded.

        On failure the previously loaded program is left **untouched and still on screen**, with its
        own filename still in the title bar. Clearing the viewport would be worse than useless — the
        user would have lost their program to a mistyped filename — and drawing nothing under the new
        name would be a lie about what they are looking at.
        """
        path = Path(path)
        self.statusBar().showMessage(f"Loading {path.name}…")

        # Exactly one push and one pop. Qt's override cursor is a stack, so restoring in both an
        # `except` branch and a `finally` pops twice for one push — and the failure dialog must open
        # *after* the pop, or it appears under an hourglass. Hence the deferred `failure`.
        failure: Exception | None = None
        program = None
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            program = open_program(path, self.profile, block_delete=self.block_delete)
        except (OSError, FileLoadError, ValueError) as error:
            failure = error
        finally:
            QApplication.restoreOverrideCursor()

        if failure is not None:
            self._report_failure(path, failure)
            return False

        self._show(program)
        self._last_directory = str(path.parent)
        return True

    def reload(self) -> None:
        """Re-read the current file from disk, for an edit made in another editor."""
        if self.program is not None and self.program.path is not None:
            self.open_file(self.program.path)

    def _show(self, program: OpenedProgram) -> None:
        self.program = program
        self.viewport.set_simulation(program.simulation)
        self.reload_action.setEnabled(program.path is not None)

        name = program.path.name if program.path else "(buffer)"
        self.setWindowTitle(f"{name} — FourSight")
        self.statusBar().showMessage(f"{name}  ·  {program.summary.headline}")
        self._update_banner(program)

    def _update_banner(self, program: OpenedProgram) -> None:
        """Show the banner only when geometry is *missing*, and always refresh its text.

        Hiding it without clearing the text would leave a stale warning to reappear on the next load.
        """
        warnings = program.summary.warnings()
        self.banner.setText("   ·   ".join(warnings))
        self.banner.setVisible(bool(warnings) and program.summary.incomplete)
        if warnings and not program.summary.incomplete:
            # Real but not about missing geometry: it belongs in the status bar, not a banner that
            # implies the picture cannot be trusted.
            self.statusBar().showMessage(f"{self.statusBar().currentMessage()}  ·  {warnings[0]}")

    def _report_failure(self, path: Path, error: Exception) -> None:
        QMessageBox.warning(self, "Cannot open file", f"{path.name}\n\n{error}")
        kept = self.program.path.name if self.program and self.program.path else "nothing"
        self.statusBar().showMessage(f"Could not open {path.name} — still showing {kept}")

    # ------------------------------------------------------------------ view

    def fit_view(self) -> None:
        if self.program is not None:
            self.viewport.fit_to(self.program.simulation.store)
