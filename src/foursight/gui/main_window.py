"""Main window: menus, file open, and the viewport. Thin by design — logic lives in `session.py`.

M2's scope is exactly "open a file, see the toolpath, orbit/pan/zoom". The editor, diagnostics panel
and timeline are M3 and M4, and are deliberately absent rather than stubbed in.

One thing here is not cosmetic. When the simulator **suppresses** geometry — a canned cycle it will
not draw as a straight line through the hole positions — the viewer is looking at an incomplete
toolpath, and a status-bar line is too easy to miss for something that changes what the picture means.
So a banner appears above the viewport and stays until a program loads without suppression. PLAN.md:
*"A previewer that refuses to draw is recoverable; one that draws the wrong path is worse than no
previewer"* — a refusal nobody notices forfeits that.

Loading runs on a background thread (T2.9), so a 100k-line file no longer freezes the window for the
~4.6 s parse-and-simulate takes. `open_file` therefore **returns immediately** and the outcome arrives
on a signal; `open_file_and_wait` is the synchronous wrapper for tests and for a path given on the
command line. Whatever was loaded before stays on screen until a *successful* load replaces it —
through the wait, a failure, or a cancellation.
"""

from pathlib import Path

from PySide6.QtCore import QDeadlineTimer, QEventLoop, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from foursight.fix.engine import FixContext, FixHistory, apply_fix, get_fix, load_builtin_fixes
from foursight.gui.background import BufferLoader, ProgramLoader
from foursight.gui.diagnostics_panel import DiagnosticsPanel
from foursight.gui.diff_dialog import DiffDialog, RefusalDialog
from foursight.gui.editor import CodeEditor
from foursight.gui.selection import LineSelection, select_line
from foursight.gui.session import OpenedProgram
from foursight.gui.timeline_bar import TimelineBar
from foursight.gui.viewport3d import ToolpathViewport
from foursight.machine.kinematics import KinematicsError, apply_display_transform
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
        self._loader: ProgramLoader | None = None
        self._loading_path: Path | None = None
        self.selection: LineSelection | None = None
        self.fix_history = FixHistory()

        self.setWindowTitle("FourSight")
        self.resize(1280, 800)

        self.viewport = ToolpathViewport()
        self.editor = CodeEditor()
        self.diagnostics = DiagnosticsPanel()
        self.timeline = TimelineBar()
        self.banner = QLabel()
        self.banner.setStyleSheet(_BANNER_STYLE)
        self.banner.setWordWrap(True)
        self.banner.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.banner.hide()

        # Code on the left, toolpath on the right. A splitter rather than a fixed layout because the
        # useful ratio depends entirely on the task: reading code wants width, judging geometry wants it
        # all. Sizes are a starting point, not a constraint.
        # Code left, toolpath and diagnostics right. Diagnostics sit *under* the viewport rather than
        # beside the editor: a finding is read and then looked at, so the geometry has to stay in view
        # while the list is scanned.
        # The scrubber sits directly under the viewport, inside the same pane, because it is a control
        # *for* the view rather than a separate one.
        viewport_pane = QWidget()
        viewport_layout = QVBoxLayout(viewport_pane)
        viewport_layout.setContentsMargins(0, 0, 0, 0)
        viewport_layout.setSpacing(0)
        viewport_layout.addWidget(self.viewport, stretch=1)
        viewport_layout.addWidget(self.timeline)

        self.right = QSplitter(Qt.Vertical)
        self.right.addWidget(viewport_pane)
        self.right.addWidget(self.diagnostics)
        self.right.setStretchFactor(0, 4)
        self.right.setStretchFactor(1, 1)
        self.right.setSizes([560, 200])

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.editor)
        self.splitter.addWidget(self.right)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setSizes([480, 800])

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.banner)
        layout.addWidget(self.splitter, stretch=1)
        self.setCentralWidget(container)

        # Progress and Cancel live in the status bar so a long load never blocks the window (T2.9).
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_load)
        self.cancel_button.hide()
        # Added to the status bar only while a load is running; see `_set_busy` for why they cannot
        # simply be hidden in place.
        self._busy_shown = False

        # The cursor drives the highlight, not just a click: following it costs 0.8 ms at 500k segments
        # (T3.2), and arrow-keying down a program while watching the toolpath light up is the point.
        self.editor.cursorPositionChanged.connect(self._on_cursor_moved)
        # The reverse direction (T3.3). Together these close the sync loop, which is why the click
        # handler must not feed back: see `_on_segment_picked`.
        self.viewport.segment_picked.connect(self._on_segment_picked)
        self.diagnostics.line_activated.connect(self._on_diagnostic_activated)
        self.timeline.scrubbed.connect(self._on_scrubbed)

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

        fix_menu = self.menuBar().addMenu("Fi&x")
        self.fix_actions: dict[str, QAction] = {}
        for fix_id, fix in sorted(load_builtin_fixes().items(), key=lambda item: item[1].title):
            # A destructive fix is present but must never be the easy default: no shortcut, and the title
            # says so. PLAN.md keeps N-word stripping off by default for reasons the diff cannot show.
            label = f"{fix.title}…" if fix.needs_parameter else fix.title
            if fix.destructive:
                label += "  (destructive)"
            action = QAction(label, self)
            action.setStatusTip(fix.description)
            action.triggered.connect(lambda _checked=False, fix_id=fix_id: self.run_fix(fix_id))
            fix_menu.addAction(action)
            self.fix_actions[fix_id] = action
        fix_menu.addSeparator()
        self.undo_fix_action = self._add(
            fix_menu, "&Undo last fix", QKeySequence.Undo, self.undo_fix
        )
        self.undo_fix_action.setEnabled(False)

        view_menu = self.menuBar().addMenu("&View")
        self._add(view_menu, "&Fit to program", QKeySequence("Ctrl+0"), self.fit_view)
        view_menu.addSeparator()
        self.part_coordinates_action = QAction("&Part coordinates", self)
        self.part_coordinates_action.setCheckable(True)
        self.part_coordinates_action.setShortcut(QKeySequence("Ctrl+P"))
        self.part_coordinates_action.setToolTip(
            "Show the path as it lies on the part (table mount) or the tool tip (head mount), "
            "instead of machine coordinates"
        )
        self.part_coordinates_action.toggled.connect(self._on_part_coordinates_toggled)
        view_menu.addAction(self.part_coordinates_action)

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

    def open_file(self, path: str | Path) -> None:
        """Start loading ``path`` on a background thread (T2.9).

        Returns immediately; the outcome arrives on `_on_loaded`, `_on_failed` or `_on_cancelled`. At
        100k lines parse plus simulate takes ~4.6 s, and doing that inline made the window look hung.

        Whatever is already loaded stays **untouched and on screen** until a *successful* load replaces
        it — through a failure, a cancellation, or the wait itself. Clearing the viewport up front
        would lose the user's program to a mistyped filename, and drawing nothing under the new name
        would misrepresent what they are looking at.
        """
        path = Path(path)
        # A second Open while one is running: the newer request is what the user wants, so the older
        # load is cancelled rather than queued or refused. Cancelling is not instant — the worker
        # notices on its next progress tick — so the old thread is detached and left to exit on its
        # own, and its signals are disconnected first so a late `loaded` cannot draw the wrong file.
        self._cancel_running_load()

        self._loader = ProgramLoader(
            path, self.profile, block_delete=self.block_delete, parent=self
        )
        self._loader.progressed.connect(self._on_progress)
        self._loader.loaded.connect(self._on_loaded)
        self._loader.failed.connect(lambda message: self._on_failed(path, message))
        self._loader.cancelled.connect(lambda: self._on_cancelled(path))
        self._loader.verified.connect(self._on_verified)
        # `_loader` is cleared by the thread's own `finished`, not by `loaded`. Verification runs as a
        # second stage *after* `loaded`, so clearing it there left a live QThread with nothing holding it:
        # `closeEvent` would not wait for it, which is exactly the crash-on-shutdown it exists to prevent.
        # It also keeps Cancel working during the check, which is a 5.9 s stage at 100k lines.
        self._loader.finished.connect(self._on_loader_finished)
        self._loading_path = path

        self._set_busy(True, f"Loading {path.name}…")
        self._loader.start()

    def open_file_and_wait(self, path: str | Path, timeout_ms: int = 30_000) -> bool:
        """Synchronous wrapper: start a load and pump the event loop until it settles.

        For tests and for `foursight-gui part.nc`, where there is no user to wait for. Kept separate
        from `open_file` so the interactive path has no blocking branch to get wrong.
        """
        self.open_file(path)
        deadline = QDeadlineTimer(timeout_ms)
        while self._loader is not None and not deadline.hasExpired():
            QApplication.processEvents(QEventLoop.AllEvents, 20)
        return self.program is not None and self.program.path == Path(path)

    def cancel_load(self) -> None:
        """Cancel an in-flight load, leaving the previous program on screen."""
        if self._loader is not None:
            self.statusBar().showMessage("Cancelling…")
            self._loader.cancel()

    def _cancel_running_load(self) -> None:
        """Detach any running loader so its result can no longer reach the window."""
        if self._loader is None:
            return
        loader, self._loader = self._loader, None
        try:
            loader.progressed.disconnect()
            loader.loaded.disconnect()
            loader.failed.disconnect()
            loader.cancelled.disconnect()
        except RuntimeError:  # pragma: no cover - already disconnected
            pass
        loader.cancel()

    # ------------------------------------------------------------------ load outcomes

    def _on_progress(self, done: int, total: int, stage: str) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.statusBar().showMessage(f"{stage} {self._loading_name()} — {done:,} of {total:,}")
        else:
            # Parsing has no measurable extent, so the bar runs indeterminate rather than sitting at
            # zero and reading as a stall.
            self.progress.setRange(0, 0)
            self.statusBar().showMessage(f"{stage} {self._loading_name()}…")

    def _on_loaded(self, program: OpenedProgram) -> None:
        self._set_busy(False)
        self._show(program)
        if program.path is not None:
            self._last_directory = str(program.path.parent)

    def _on_failed(self, path: Path, message: str) -> None:
        self._set_busy(False)
        self._report_failure(path, message)

    def _on_cancelled(self, path: Path) -> None:
        """A cancelled load draws nothing — `simulate` raises rather than returning a partial store."""
        self._set_busy(False)
        kept = self.program.path.name if self.program and self.program.path else "nothing"
        self.statusBar().showMessage(f"Cancelled {path.name} — still showing {kept}")

    def _on_loader_finished(self) -> None:
        """The thread has actually exited, so it is safe to forget it.

        Guarded against a superseded loader finishing later and clearing a *newer* one out from under the
        window — `_cancel_running_load` detaches the old thread but cannot make it stop instantly.
        """
        if self._loader is not None and self._loader.isFinished():
            self._loader = None

    def _loading_name(self) -> str:
        return self._loading_path.name if self._loading_path else ""

    def _set_busy(self, busy: bool, message: str = "") -> None:
        """Show or hide the progress bar and Cancel button together.

        Uses `addPermanentWidget`/`removeWidget` rather than `setVisible`, because **`setVisible(False)`
        does not work on a status-bar permanent widget**: `QStatusBar` re-shows everything it manages on
        every reformat, and showing a message causes one. The first version used `setVisible` and the
        widgets stayed on screen for the rest of the session — a finished application looking
        permanently busy, with a Cancel button that did nothing. `removeWidget` is Qt's documented way
        to hide one ("does not delete the widget but hides it").

        `_busy_shown` guards against adding the same widget twice, which would leave a duplicate slot
        in the layout.
        """
        bar = self.statusBar()
        if busy and not self._busy_shown:
            bar.addPermanentWidget(self.progress)
            bar.addPermanentWidget(self.cancel_button)
            self.progress.show()
            self.cancel_button.show()
            self._busy_shown = True
        elif not busy and self._busy_shown:
            bar.removeWidget(self.progress)
            bar.removeWidget(self.cancel_button)
            self._busy_shown = False
        if busy:
            self.progress.setRange(0, 0)
            bar.showMessage(message)

    def reload(self) -> None:
        """Re-read the current file from disk, for an edit made in another editor."""
        if self.program is not None and self.program.path is not None:
            self.open_file(self.program.path)

    def _show(self, program: OpenedProgram) -> None:
        self.program = program
        # The editor shows exactly the text that was parsed — `loaded.text`, after decoding and BOM
        # removal — not a re-read of the file. Anything else and the line numbers in the gutter could
        # disagree with the ones in `SourceRef`, which is what T3.2 and T3.4 sync on.
        self.selection = None
        # The transform belongs to the previous store, so the toggle resets rather than silently
        # displaying the new program untransformed while the menu still shows it checked.
        self.part_coordinates_action.setChecked(False)
        # "Checking…" rather than an empty list: an empty diagnostics panel reads as "no problems found",
        # which is a claim we have not made yet at this point.
        self.diagnostics.set_pending()
        self.timeline.set_simulation(program.simulation)
        # Only rewrite the pane when the text actually differs. Re-setting identical text after a
        # buffer reload would reset the cursor and scroll position on every applied fix.
        if self.editor.toPlainText() != program.loaded.text:
            self.editor.setPlainText(program.loaded.text)
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

    def _report_failure(self, path: Path, message: str) -> None:
        QMessageBox.warning(self, "Cannot open file", f"{path.name}\n\n{message}")
        kept = self.program.path.name if self.program and self.program.path else "nothing"
        self.statusBar().showMessage(f"Could not open {path.name} — still showing {kept}")

    def closeEvent(self, event) -> None:
        """Stop a running load before the window goes away.

        A QThread outliving its parent widget is how a clean exit turns into a crash on shutdown: the
        worker would emit into a deleted receiver. `wait` is bounded because the worker only notices
        cancellation on its next progress tick.
        """
        if self._loader is not None:
            loader, self._loader = self._loader, None
            loader.cancel()
            loader.wait(5000)
        super().closeEvent(event)

    # ------------------------------------------------------------------ editor -> viewport (T3.2)

    def _on_cursor_moved(self) -> None:
        """Highlight the segments belonging to the line under the cursor.

        Silent when nothing is loaded. The status message deliberately distinguishes "no motion" from
        "not drawn": a user clicking a canned-cycle line and seeing nothing highlight would otherwise
        conclude the line does nothing, when in fact the simulator refused to draw it.
        """
        if self.program is None:
            return
        line_no = self.editor.current_line
        if line_no > self.editor.source_line_count:
            # The trailing block of a newline-terminated file is a real cursor position with no source
            # line behind it. Clearing beats highlighting line 0 or raising.
            self.selection = None
            self.viewport.clear_highlight()
            return

        self.selection = select_line(self.program.simulation, line_no)
        self.viewport.set_highlight(self.program.simulation.store, self.selection.mask)
        self.statusBar().showMessage(self.selection.describe())

    def _on_verified(self, diagnostics) -> None:
        """Stage two arrived. Ignored if a different program has since been loaded."""
        if self.program is None:
            return
        self.diagnostics.set_diagnostics(diagnostics)

    def _on_diagnostic_activated(self, line_no: int) -> None:
        """Clicking a finding jumps the editor there, which highlights the line via the T3.2 path."""
        self.editor.goto_line(line_no)

    # ------------------------------------------------------------------ fixes (T5.1)

    def run_fix(self, fix_id: str) -> bool:
        """Apply exactly one fix, then re-run the whole pipeline. Returns whether it was applied.

        **This is the one-fix contract, and the reload is the contract.** A fix that inserts or removes a
        line shifts every line number after it, so every `Diagnostic.line` and every `SegmentStore.line`
        entry is stale the moment it applies. Rather than trying to rebase anything, the buffer goes back
        through load → parse → simulate → verify exactly as if it had been opened again. T2.9 made that
        cheap enough to be practical: it is the same threaded, cancellable path.
        """
        if self.program is None:
            return False
        fix = get_fix(fix_id)
        if fix is None:
            return False

        parameter = None
        if fix.needs_parameter:
            # Prompted, never invented. A feed rate is a machining decision about tool, material and
            # depth of cut; choosing one here would put a number in the program that nobody chose.
            value, accepted = QInputDialog.getDouble(
                self, fix.title, fix.parameter_prompt, 600.0, 0.0001, 1_000_000.0, 4
            )
            if not accepted:
                return False
            parameter = value

        text = self.editor.toPlainText()
        result = apply_fix(
            fix_id,
            FixContext(
                text=text,
                profile=self.profile,
                line=self.selection.line_no if self.selection else self.editor.current_line,
                parameter=parameter,
                block_delete=self.block_delete,
            ),
        )

        if result.refused:
            RefusalDialog(fix, result, self).exec()
            self.statusBar().showMessage(f"{fix.title}: not applied")
            return False
        if result.changed_nothing:
            self.statusBar().showMessage(f"{fix.title}: {result.note or 'nothing to change'}")
            return False
        if DiffDialog(fix, result, self).exec() != DiffDialog.Accepted:
            self.statusBar().showMessage(f"{fix.title}: cancelled")
            return False

        self.fix_history.record(fix_id, text)
        self.undo_fix_action.setEnabled(True)
        self._reload_from_buffer(result.text, f"{fix.title} applied")
        return True

    def undo_fix(self) -> None:
        """Restore the buffer to before the last fix.

        Snapshots, not reversed diffs — reversing a diff is exactly the rebasing the one-fix contract
        forbids, and a snapshot cannot be applied to the wrong place.
        """
        entry = self.fix_history.undo()
        if entry is None:
            return
        fix_id, previous = entry
        self.undo_fix_action.setEnabled(self.fix_history.depth > 0)
        fix = get_fix(fix_id)
        self._reload_from_buffer(previous, f"undid {fix.title if fix else fix_id}")

    def _reload_from_buffer(self, text: str, message: str) -> None:
        """Re-run the pipeline over edited text, off the GUI thread.

        The file on disk is untouched: PLAN.md requires fixes to modify the editor buffer with the user
        saving explicitly. So this loads from *text*, and the path is kept only for the title bar.
        """
        self.editor.setPlainText(text)
        self._cancel_running_load()
        self._loader = BufferLoader(
            text,
            self.profile,
            path=self.program.path if self.program else None,
            block_delete=self.block_delete,
            parent=self,
        )
        self._loader.progressed.connect(self._on_progress)
        self._loader.loaded.connect(self._on_loaded)
        self._loader.failed.connect(lambda reason: self._on_failed(Path("buffer"), reason))
        self._loader.cancelled.connect(lambda: self._on_cancelled(Path("buffer")))
        self._loader.verified.connect(self._on_verified)
        self._loader.finished.connect(self._on_loader_finished)
        self._loading_path = self.program.path if self.program else None
        self._set_busy(True, message)
        self._loader.start()

    # ------------------------------------------------------------------ display transform (T4.5)

    def _on_part_coordinates_toggled(self, enabled: bool) -> None:
        """Switch the viewport between machine and part coordinates.

        The transform is computed **on demand**, not at load: it costs a second (N, 2, 3) float64 array —
        24 MB at 500k segments — and most sessions never ask for it.

        A profile that cannot describe the transform (head mount with no `pivot_to_tip`) is reported and
        the toggle reverts, rather than drawing machine coordinates while the menu claims otherwise. A
        checkbox that lies about what is on screen is worse than one that refuses.
        """
        if self.program is None:
            return
        store = self.program.simulation.store
        if enabled and store.lin_part is None:
            try:
                apply_display_transform(store, self.profile.kinematics)
            except (KinematicsError, ValueError) as error:
                QMessageBox.warning(self, "Cannot show part coordinates", str(error))
                self.part_coordinates_action.setChecked(False)
                return

        self.viewport.set_simulation(self.program.simulation, use_part_coordinates=enabled)
        # The selection survives the switch — it is a set of segment indices, not coordinates — but has to
        # be redrawn in the new frame.
        if self.selection is not None:
            self.viewport.set_highlight(store, self.selection.mask)
        mode = "part" if enabled else "machine"
        self.statusBar().showMessage(f"Showing {mode} coordinates")

    # ------------------------------------------------------------------ timeline (T3.5)

    def _on_scrubbed(self, index: int) -> None:
        """Move the editor to the line of the segment at the scrub position.

        The cursor move highlights the line through the T3.2 path, so the scrubber needs no highlight
        machinery of its own — and watching the code scroll past as the tool advances is what makes a
        timeline worth having in an editor rather than a player.
        """
        if self.program is None:
            return
        store = self.program.simulation.store
        if not 0 <= index < len(store):
            return
        line_no = int(store.line[index])
        self.editor.goto_line(line_no)
        # Fed back so the readout can name the line. Safe from a loop: `show_line` only sets a label.
        self.timeline.show_line(line_no)

    # ------------------------------------------------------------------ viewport -> editor (T3.3)

    def _on_segment_picked(self, index: int) -> None:
        """Move the editor to the source line of the clicked segment.

        `SegmentStore.line[index]` is the whole mechanism — every segment has carried its source line
        since T2.1 precisely so this is a lookup rather than a search.

        Moving the cursor fires `cursorPositionChanged`, which runs `_on_cursor_moved` and highlights the
        line. That is deliberate reuse rather than an accident: a click should light up **the whole line**
        the segment belongs to, not the single segment under the cursor, because that is what tells the
        user how far the block they clicked actually travels. It also terminates — `goto_line` moves the
        cursor once, and the resulting selection does not move it again.
        """
        if self.program is None:
            return
        store = self.program.simulation.store
        if not 0 <= index < len(store):
            return
        self.editor.goto_line(int(store.line[index]))
        self.editor.setFocus()

    # ------------------------------------------------------------------ view

    def fit_view(self) -> None:
        if self.program is not None:
            self.viewport.fit_to(self.program.simulation.store)
