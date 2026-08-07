"""Capture the README screenshots. Regenerate with:

    DISPLAY=:0 .venv/bin/python scripts/screenshots.py

Scripted rather than hand-captured so the images can be refreshed after a UI change instead of drifting
out of date — a README showing a version of the tool that no longer exists is worse than one with no
pictures, because it is confidently wrong about what the user will see.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

OUT = REPO / "docs" / "images"
WRAP = """G21 G90 G94 G54
G0 Z25
G0 Y25 Z0
G1 X20 A360 F600
G1 X40 A720
G1 X60 A1080
G0 Z25
M30
"""


def capture(window, name: str, app) -> None:
    for _ in range(6):
        app.processEvents()
    window.grab().save(str(OUT / f"{name}.png"))
    print(f"  wrote docs/images/{name}.png")


def main() -> int:
    from PySide6.QtCore import QDeadlineTimer, QEventLoop
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from foursight.gui.main_window import MainWindow
    from foursight.machine.profile import default_profile_path, load_profile

    OUT.mkdir(parents=True, exist_ok=True)
    profile = load_profile(default_profile_path())

    def settle(window) -> None:
        deadline = QDeadlineTimer(60_000)
        while window._loader is not None and not deadline.hasExpired():
            app.processEvents(QEventLoop.AllEvents, 20)
        app.processEvents()

    # 1. The whole window on a program with a real diagnostic.
    window = MainWindow(profile)
    window.resize(1500, 900)
    window.show()
    window.open_file(REPO / "tests" / "fixtures" / "canned_cycle_span.nc")
    settle(window)
    window.editor.goto_line(9)
    capture(window, "overview", app)

    # 2. A 4-axis wrap, in part coordinates — the picture machine coordinates cannot give you.
    wrapped = REPO / "build" / "wrap-demo.nc"
    wrapped.parent.mkdir(parents=True, exist_ok=True)
    wrapped.write_text(WRAP, encoding="utf-8")
    window.open_file(wrapped)
    settle(window)
    window.part_coordinates_action.setChecked(True)
    window.viewport.setCameraPosition(distance=190, elevation=22, azimuth=-58)
    capture(window, "part-coordinates", app)

    # 3. The diff preview for a fix.
    from foursight.fix.engine import FixContext, apply_fix, get_fix, load_builtin_fixes
    from foursight.gui.diff_dialog import DiffDialog

    load_builtin_fixes()
    text = "G1 X10 F600\nG1 X20\n"
    result = apply_fix("fix.add-safety-preamble", FixContext(text=text, profile=profile))
    dialog = DiffDialog(get_fix("fix.add-safety-preamble"), result)
    dialog.resize(820, 380)
    dialog.show()
    capture(dialog, "diff-preview", app)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
