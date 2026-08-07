"""GUI entry point: ``foursight-gui``.

**Qt is imported inside `main`, not at module scope.** That looks backwards for a module whose whole
job is Qt, and it is deliberate: a console script resolves `foursight.gui.app:main` by importing this
module, so a top-level `from PySide6...` would make a missing `[gui]` extra surface as a bare
`ModuleNotFoundError` traceback before any of our code runs. Importing late lets the same situation
produce one sentence naming the fix.

`foursight parse` and `foursight check` stay in `cli.py` and must never need Qt — the `headless` CI job
enforces that by installing `[dev]` only.
"""

import argparse
from collections.abc import Sequence
from pathlib import Path

USAGE_ERROR = 2
MISSING_GUI_EXTRA = 3

_INSTALL_HINT = (
    "The GUI needs the optional Qt dependencies, which are not installed.\n"
    "Install them with:\n\n"
    '    pip install "foursight-cnc[gui]"\n\n'
    "The headless commands (foursight parse, foursight check) work without them."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="foursight-gui", description="View a 4-axis G-code toolpath."
    )
    parser.add_argument(
        "file", nargs="?", type=Path, help="G-code file to open on startup (optional)"
    )
    parser.add_argument(
        "--profile",
        type=Path,
        help="machine profile TOML (default: the profile bundled with foursight)",
    )
    parser.add_argument(
        "--block-delete",
        action="store_true",
        help="honour block-delete '/' and skip those blocks (default: they execute, as on most controls)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(_INSTALL_HINT)
        return MISSING_GUI_EXTRA

    from foursight.machine.profile import ProfileError, default_profile_path, load_profile

    try:
        profile = load_profile(args.profile or default_profile_path())
    except (OSError, ProfileError) as error:
        # Before the window exists, so there is nowhere to show a dialog. Refusing outright beats
        # opening with a silently substituted default profile: limits and rapid rates would be wrong,
        # and every verification result with them.
        print(f"foursight-gui: cannot load profile: {error}")
        return USAGE_ERROR

    from foursight.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(profile, block_delete=args.block_delete)
    window.show()
    if args.file is not None:
        window.open_file(args.file)
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
