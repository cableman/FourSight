"""Loading a program on a background thread, so the window stays responsive.

PLAN.md § Performance Requirements: *"Simulation runs off the GUI thread (QThread) with progress
reporting for large files."* At 100k lines, parse plus simulate takes about 4.6 s on the baseline
machine — long enough that doing it inline makes the application look hung rather than busy.

The division of labour is the point:

- **`sim/simulator` knows nothing about Qt.** It takes `progress(done, total)` and `cancelled() -> bool`,
  and a `threading.Event.is_set` satisfies the latter exactly. No Qt type crosses into `sim/`.
- **This module owns only the threading**, and is deliberately tiny, because a QThread is awkward to
  test and everything worth asserting already lives in `session.py` and `simulator.py`.

**A cancelled load produces nothing.** `simulate` raises rather than returning a partial `Simulation`,
because a half-stepped program is a truncated toolpath and there is no honest way to draw one. The
window keeps whatever it had, exactly as it does for a failed open.

Signals cross the thread boundary, so the payloads are plain Python objects — an `OpenedProgram`, a
string, two ints. Qt queues them for the receiving thread automatically; nothing here touches a widget.
"""

import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from foursight.fileio.loader import FileLoadError
from foursight.gui.session import open_program, verify_program
from foursight.machine.profile import MachineProfile
from foursight.sim.simulator import SimulationCancelled

# Progress is reported this often, in blocks. Small enough that the bar moves visibly on a large file,
# large enough that the signal traffic is irrelevant next to ~40 us of work per block.
PROGRESS_INTERVAL = 2000

PARSING = "Parsing"
SIMULATING = "Simulating"
#: The stage-two label. Not emitted as progress (see `_verify`); the diagnostics panel owns
#: reporting that a check is running.
VERIFYING = "Checking"


class ProgramLoader(QThread):
    """Loads, parses and simulates a file off the GUI thread.

    Emits exactly one terminal signal — `loaded`, `failed` or `cancelled` — so a caller can re-enable
    its UI in one place without tracking which path it took.
    """

    #: (done, total, stage) — `total` is 0 while the stage has no measurable extent, as parsing does not.
    progressed = Signal(int, int, str)
    loaded = Signal(object)  # OpenedProgram
    #: Diagnostics, emitted *after* `loaded`. A second stage on purpose: verification costs 5.93 s at
    #: 100k lines, and the toolpath is what the user opened the file to see. Geometry first, findings
    #: after — the way a linter fills in behind an editor.
    verified = Signal(object)  # tuple[Diagnostic, ...]
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        path: Path,
        profile: MachineProfile,
        *,
        block_delete: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.path = Path(path)
        self.profile = profile
        self.block_delete = block_delete
        self._stop = threading.Event()

    def cancel(self) -> None:
        """Ask the load to stop. Safe from the GUI thread; `Event` is what makes it so.

        Returns immediately rather than waiting: the worker notices on its next progress tick, at most
        `PROGRESS_INTERVAL` blocks away. Blocking here would freeze the very UI this class exists to
        keep responsive.
        """
        self._stop.set()

    @property
    def cancellation_requested(self) -> bool:
        return self._stop.is_set()

    def run(self) -> None:  # pragma: no cover - exercised via `start()` in the GUI
        """The thread body. Every exit path emits exactly one terminal signal."""
        try:
            self.progressed.emit(0, 0, PARSING)
            program = open_program(
                self.path,
                self.profile,
                block_delete=self.block_delete,
                progress=self._report,
                cancelled=self._stop.is_set,
            )
        except SimulationCancelled:
            self.cancelled.emit()
        except (OSError, FileLoadError, ValueError) as error:
            # Same exception set the synchronous path catches. A bare `except Exception` would report a
            # programming error as though the user's file were at fault, which sends them looking in
            # the wrong place entirely.
            self.failed.emit(str(error))
        else:
            self.loaded.emit(program)
            self._verify(program)

    def _verify(self, program) -> None:
        """Stage two. Failures here must not retract the toolpath that already loaded successfully.

        A rule raising is a bug in us, not in the user's file, and `verify` already converts a raising
        rule into an `internal.rule-failed` diagnostic. Anything escaping that is reported as a failed
        *check* while the geometry stays on screen — the alternative would throw away a good toolpath
        because the verifier tripped.
        """
        if self._stop.is_set():
            self.cancelled.emit()
            return
        # Deliberately *no* progress emit here. Stage one owns the status bar and leaves the program
        # summary in it; announcing "Checking…" there would overwrite the blocks/segments/time line the
        # user just got, and replace information with a transient. The diagnostics panel's own header
        # says "Checking…", which is where check status belongs.
        try:
            diagnostics = verify_program(program, self.profile, block_delete=self.block_delete)
        except Exception as error:  # noqa: BLE001 - see the docstring; the toolpath must survive
            self.failed.emit(f"the check could not be completed: {error}")
            return
        if not self._stop.is_set():
            self.verified.emit(diagnostics)

    def _report(self, done: int, total: int) -> None:
        self.progressed.emit(done, total, SIMULATING)
