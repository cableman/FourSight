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

# Cut against `profiles/rotary.toml`, which is the only shipped profile carrying both a [stock] blank
# and a [tool] cutter — the solid view refuses to invent either. Z is radial and measured from the
# blank's surface there, so Z-9 is a 4.4 mm deep groove in a 42.8 mm bar.
GROOVE = """(A wrapped helical groove, for the solid view. Cut with profiles/rotary.toml.)
G21 G90 G94 G54
S18000 M3
G0 Z10
G0 X0 Y10 A0
G1 Z-9.0 F800
G1 Y120.0 A1080.0
G0 Z10
M5
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

    # 3. The solid view: what the program leaves behind, rather than where the tool went. Needs a
    # profile with both [stock] and [tool]; switching it on flips `Part coordinates` by itself,
    # because a cylinder's carve is only stationary in the part's frame.
    # The profile is swapped through the same path `Apply` in the profile dialog takes, rather than by
    # opening a second window: two GL contexts in one process leave the first window's line items
    # undrawable, so the toolpath over the solid would silently go missing.
    from foursight.machine.profile_doc import ProfileDocument

    rotary_text = (REPO / "profiles" / "rotary.toml").read_text(encoding="utf-8")
    window._profile_dialog = None
    # Cleared first: applying a profile under a live `Part coordinates` raises out of
    # `_on_part_coordinates_toggled`, because the reload clears the toggle before the timeline has
    # caught up with the new store. Steered around here rather than papered over — it is a real defect.
    window.part_coordinates_action.setChecked(False)
    window._on_profile_applied(ProfileDocument.from_text(rotary_text))
    settle(window)
    groove = REPO / "build" / "groove-demo.nc"
    groove.write_text(GROOVE, encoding="utf-8")
    window.open_file(groove)
    settle(window)
    window.solid_action.setChecked(True)
    carved = QDeadlineTimer(120_000)
    while window._carver is not None and not carved.hasExpired():
        app.processEvents(QEventLoop.AllEvents, 20)
    window.viewport.setCameraPosition(distance=190, elevation=24, azimuth=-62)
    capture(window, "solid-view", app)

    # 4. The diff preview for a fix.
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
