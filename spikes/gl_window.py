"""SPIKE (T0.8): does a PyInstaller-bundled Qt + OpenGL app start on a clean machine?

Resolves decision D2 in TASKS.md. Bundle it with the shipping build script::

    .venv/bin/python scripts/build.py --entry spikes/gl_window.py --name gl-spike

then copy ``dist/gl-spike/`` to a Windows VM with no Python and no dev tooling, and run
``gl-spike.exe`` from a terminal so the console output is visible.

This script's purpose is to make a *failure* legible. On a clean machine the interesting outcomes
are not "it worked" but "PySide6 imported and PyOpenGL did not", or "the window opened and no
frame was ever painted" — so each import is probed separately and the exit code distinguishes
them:

    0  a frame was painted and GL reported itself      -> D2 answered yes
    1  imports fine, but no frame was ever painted      -> GL/driver or QPA plugin problem
    2  an import failed                                 -> missing hidden import or binary

It imports pyqtgraph as well as PySide6 and PyOpenGL, deliberately: pyqtgraph ships shader source
as package data, which is exactly the kind of thing PyInstaller's analysis drops. Testing the bare
Qt window alone would pass while the real application still failed.
"""

import argparse
import sys

DEFAULT_SECONDS = 10


def describe_environment() -> None:
    """Print what a bundled app's own view of the world looks like."""
    frozen = getattr(sys, "frozen", False)
    print("=" * 70)
    print("FourSight T0.8 packaging spike")
    print("=" * 70)
    print(f"frozen:      {frozen}")
    print(f"executable:  {sys.executable}")
    print(f"platform:    {sys.platform}")
    print(f"python:      {sys.version.split()[0]}")
    if frozen:
        # Set by the PyInstaller bootloader; the one-dir extraction root.
        print(f"_MEIPASS:    {getattr(sys, '_MEIPASS', '(unset)')}")
    print()


def probe_imports() -> dict[str, str] | None:
    """Import each dependency separately so a failure names the culprit.

    Returns version info, or None if anything failed. A bare traceback here would be nearly
    useless on a VM with no Python installed: the point is a one-line answer to "what is missing".
    """
    versions: dict[str, str] = {}
    for label, module_name, attribute in (
        ("PySide6", "PySide6", "__version__"),
        ("shiboken6", "shiboken6", "__version__"),
        ("PyOpenGL", "OpenGL", "__version__"),
        ("pyqtgraph", "pyqtgraph", "__version__"),
    ):
        try:
            module = __import__(module_name)
        # Bare `Exception` is intentional: a bundled app can fail to import for reasons well
        # outside ImportError (missing DLL, bad shiboken binding), and the failure is the result.
        except Exception as exc:
            print(f"IMPORT FAILED  {label}: {type(exc).__name__}: {exc}")
            return None
        versions[label] = str(getattr(module, attribute, "?"))
        print(f"imported ok    {label} {versions[label]}")
    print()
    return versions


def describe_qt() -> None:
    """Qt plugin discovery is the usual reason a bundled Qt app dies before painting."""
    from PySide6 import QtCore

    paths = QtCore.QLibraryInfo.path(QtCore.QLibraryInfo.LibraryPath.PluginsPath)
    print(f"Qt plugins:  {paths}")
    print(f"QPA platform: {QtCore.QCoreApplication.instance() and 'set' or 'not yet created'}")
    print()


def build_window(seconds: int, stay: bool):
    """A GL view with a little geometry, wired to report the first painted frame."""
    import numpy as np
    import pyqtgraph.opengl as gl
    from OpenGL import GL
    from PySide6 import QtCore

    state = {"painted": False}

    class SpikeView(gl.GLViewWidget):
        def paintGL(self, *args: object, **kwargs: object) -> None:  # noqa: N802 - Qt override
            super().paintGL(*args, **kwargs)
            if state["painted"]:
                return
            state["painted"] = True

            def text(name: int) -> str:
                value = GL.glGetString(name)
                return value.decode("utf-8", "replace") if value else "?"

            print("first frame painted")
            print(f"GL_RENDERER: {text(GL.GL_RENDERER)}")
            print(f"GL_VENDOR:   {text(GL.GL_VENDOR)}")
            print(f"GL_VERSION:  {text(GL.GL_VERSION)}")
            print(flush=True)
            if not stay:
                QtCore.QTimer.singleShot(seconds * 1000, QtCore.QCoreApplication.quit)

    view = SpikeView()
    view.setWindowTitle("FourSight T0.8 packaging spike")
    view.resize(800, 600)
    view.setCameraPosition(distance=40)

    angle = np.linspace(0.0, 8.0 * np.pi, 2000, dtype=np.float32)
    helix = np.stack([np.cos(angle) * 8.0, np.sin(angle) * 8.0, angle], axis=1)
    view.addItem(gl.GLLinePlotItem(pos=helix, color=(0.2, 0.9, 0.3, 1.0), mode="line_strip"))
    return view, state


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="T0.8 bundled Qt+GL launch spike.")
    parser.add_argument(
        "--seconds", type=int, default=DEFAULT_SECONDS, help="close this long after the first frame"
    )
    parser.add_argument("--stay", action="store_true", help="leave the window open until closed")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Qt aborts the process (not a clean exit) when no platform plugin can be initialised, which
    # discards a block-buffered stdout and leaves only Qt's own stderr line. That is precisely the
    # failure this spike exists to diagnose, so make every line survive it.
    sys.stdout.reconfigure(line_buffering=True)

    args_repr = " ".join(argv) if argv is not None else " ".join(sys.argv[1:])
    describe_environment()
    print(f"args: {args_repr or '(none)'}\n")

    if probe_imports() is None:
        print("\nRESULT: import failure — record the module and add it to HIDDEN_IMPORTS in")
        print("scripts/build.py, then rebuild. D2 is not yet answered.")
        return 2

    describe_qt()

    from PySide6 import QtWidgets

    # Narrate the next two steps: both can kill the process outright rather than raising, so the
    # last line printed is the diagnosis.
    print("creating QApplication (a QPA platform-plugin failure aborts here)...")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    print("creating the GL view (a GL driver failure aborts here)...")
    view, state = build_window(args.seconds, args.stay)
    view.show()
    if not args.stay:
        # Hard watchdog. The auto-close timer only starts on the *first painted frame*, so without
        # this a window that never paints (offscreen platform, no usable GL driver) would hang
        # forever and never print a RESULT line — making the exit-1 case unreachable in practice.
        from PySide6 import QtCore

        QtCore.QTimer.singleShot(max(args.seconds, 1) * 1000 + 5000, app.quit)
        print(f"window shown; closing {args.seconds}s after the first frame", flush=True)
    app.exec()

    if not state["painted"]:
        print("\nRESULT: the window was created but no frame was ever painted.")
        print("Look for a missing QPA platform plugin or an unavailable GL driver.")
        return 1
    print("RESULT: bundled Qt + OpenGL app launched and painted. D2 answered yes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
