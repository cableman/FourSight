"""Entry-point tests for ``foursight-gui`` (T2.7).

Most of this file runs **without** the `[gui]` extra, which is the point: the behaviour worth pinning
is what a user sees when Qt is *missing*. `app.py` imports Qt inside `main` rather than at module scope
precisely so that case produces one actionable sentence instead of a `ModuleNotFoundError` traceback
raised by the console-script shim before any of our code runs — and a test that needed Qt installed
could never check it.
"""

import sys

import pytest

from foursight.gui.app import MISSING_GUI_EXTRA, USAGE_ERROR, build_parser, main


@pytest.fixture
def without_qt(monkeypatch):
    """Make `import PySide6.QtWidgets` fail the way a missing extra does.

    A `None` entry in `sys.modules` makes the import machinery raise `ImportError`, which is the same
    failure a genuinely absent package produces at the point `app.main` reaches for it.
    """
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", None)


# --------------------------------------------------------------------------- the missing extra


def test_a_missing_gui_extra_prints_the_install_command(without_qt, capsys) -> None:
    assert main([]) == MISSING_GUI_EXTRA
    output = capsys.readouterr().out
    assert 'pip install "foursight-cnc[gui]"' in output


def test_a_missing_gui_extra_says_the_headless_commands_still_work(without_qt, capsys) -> None:
    """Otherwise the message reads as "this tool is broken" rather than "one feature needs an extra"."""
    main([])
    assert "foursight check" in capsys.readouterr().out


def test_a_missing_gui_extra_does_not_raise(without_qt) -> None:
    """A traceback here is the exact outcome importing Qt late exists to prevent."""
    assert isinstance(main([]), int)


def test_importing_the_entry_point_module_does_not_import_qt() -> None:
    """The console script imports this module to find `main`, so the import itself must stay clean.

    Checked by re-importing in a subprocess: this test session already has Qt loaded by other modules,
    so inspecting `sys.modules` in-process would prove nothing.
    """
    import subprocess

    code = (
        "import sys; import foursight.gui.app; "
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'PySide6', 'pyqtgraph', 'OpenGL'}); "
        "print(leaked)"
    )
    result = subprocess.run(  # noqa: S603 - `code` is a literal above, not external input
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]", f"importing foursight.gui.app pulled in {result.stdout}"


# --------------------------------------------------------------------------- arguments


def test_the_file_argument_is_optional() -> None:
    """`foursight-gui` with no arguments must open an empty window, not fail usage."""
    assert build_parser().parse_args([]).file is None


def test_a_file_and_profile_can_both_be_given() -> None:
    args = build_parser().parse_args(["part.nc", "--profile", "mill.toml"])
    assert args.file.name == "part.nc"
    assert args.profile.name == "mill.toml"


def test_block_delete_defaults_to_off() -> None:
    """PLAN.md: deleted blocks execute by default, matching the common control-panel default."""
    assert build_parser().parse_args([]).block_delete is False
    assert build_parser().parse_args(["--block-delete"]).block_delete is True


# --------------------------------------------------------------------------- the profile


def test_an_unusable_profile_is_refused_before_the_window_opens(tmp_path, capsys) -> None:
    """Opening with a silently substituted default would make every limit and rapid rate wrong.

    Needs Qt present, because `main` reaches the profile load only after the Qt import succeeds.
    """
    pytest.importorskip("PySide6", reason="the [gui] extra is not installed")
    bad = tmp_path / "broken.toml"
    bad.write_text("this is not = valid toml [[[\n", encoding="utf-8")
    assert main(["--profile", str(bad)]) == USAGE_ERROR
    assert "cannot load profile" in capsys.readouterr().out


def test_a_missing_profile_file_is_refused(tmp_path, capsys) -> None:
    pytest.importorskip("PySide6", reason="the [gui] extra is not installed")
    assert main(["--profile", str(tmp_path / "absent.toml")]) == USAGE_ERROR
    assert "cannot load profile" in capsys.readouterr().out
