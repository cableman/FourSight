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

from foursight.parser.dialect import ARC_CENTRE_CODES, DIALECT_NAMES

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
    _add_dialect_arguments(parser)
    return parser


def _add_dialect_arguments(parser: argparse.ArgumentParser) -> None:
    """The dialect overrides, shared with the headless CLI's wording.

    Imported at module scope is fine: `parser.dialect` pulls in nothing but `dataclasses`, `enum`
    and `parser.model`, so it cannot drag Qt or numpy into a `--help` run.
    """
    parser.add_argument(
        "--dialect",
        choices=DIALECT_NAMES,
        default=None,  # not "linuxcnc": a default here would silently beat every profile
        help=(
            "controller dialect, overriding the profile's [dialect].name "
            "(default: the profile's, which is linuxcnc unless it says otherwise)"
        ),
    )
    parser.add_argument(
        "--arc-centre",
        choices=tuple(ARC_CENTRE_CODES),
        default=None,
        help=(
            "arc I/J mode, overriding the profile's [dialect].arc_centre. Applies only where the "
            "arc centre is a controller setting, so it requires a non-linuxcnc dialect"
        ),
    )


def resolve_profile(
    profile_arg, dialect: str | None, arc_centre: str | None
) -> "tuple[object, object]":
    """The effective profile *and* the text it came from. Imports no Qt, so it is testable directly.

    Resolved once, at the boundary — and here that means into the profile's own **text**, not only into
    the loaded object. The GUI can edit the profile (T8.2), so a `--dialect` override that existed only
    on the `MachineProfile` would show as absent in the editor and be reverted by the first unrelated
    edit the user applied, drawing arcs with the wrong I/J convention and saying nothing.

    Raises `OSError` or `ProfileError`; `main` turns either into a refusal to start.
    """
    from foursight.machine.profile import default_profile_path
    from foursight.machine.profile_doc import ProfileDocument, apply_dialect_override

    source = Path(profile_arg) if profile_arg else default_profile_path()
    document = apply_dialect_override(
        ProfileDocument.from_text(source.read_text(encoding="utf-8")), dialect, arc_centre
    )
    return document.profile(path=source), document


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(_INSTALL_HINT)
        return MISSING_GUI_EXTRA

    from foursight.machine.profile import ProfileError

    try:
        profile, document = resolve_profile(args.profile, args.dialect, args.arc_centre)
    except (OSError, ProfileError) as error:
        # Before the window exists, so there is nowhere to show a dialog. Refusing outright beats
        # opening with a silently substituted default profile: limits and rapid rates would be wrong,
        # and every verification result with them.
        print(f"foursight-gui: cannot load profile: {error}")
        return USAGE_ERROR

    from foursight.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(profile, block_delete=args.block_delete, profile_document=document)
    window.show()
    if args.file is not None:
        window.open_file(args.file)
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
