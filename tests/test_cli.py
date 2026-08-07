"""CLI tests (T1.11).

Exit codes are part of the contract — `foursight check` is meant to run in a build pipeline — so
each is asserted explicitly, including the deliberate choice that `unsupported` does *not* fail a
run.

The Qt-free requirement is checked in a subprocess. This venv has the `[gui]` extra installed, so an
in-process check would prove nothing about a `.[dev]`-only install.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FIXTURES, fixture_text
from foursight.cli import EXIT_CANNOT_RUN, EXIT_ERRORS_FOUND, EXIT_OK, main

BASELINE = FIXTURES / "baseline_4axis.nc"
ALL_FIXTURES = sorted(path.name for path in FIXTURES.glob("*.nc"))


def run(argv, capsys) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --------------------------------------------------------------------------- check


def test_check_a_clean_program_exits_zero(capsys) -> None:
    code, out, _ = run(["check", str(BASELINE)], capsys)
    assert code == EXIT_OK
    assert "no problems found" in out


def test_check_reports_errors_and_exits_one(capsys, tmp_path: Path) -> None:
    program = tmp_path / "broken.nc"
    program.write_text("G1 G2 X10\n", encoding="utf-8")
    code, out, _ = run(["check", str(program)], capsys)
    assert code == EXIT_ERRORS_FOUND
    assert "structural.modal-group-conflict" in out


def test_unsupported_alone_does_not_fail_the_run(capsys) -> None:
    """A program that legitimately contains canned cycles must still pass a pipeline.

    Failing on `unsupported` would push users toward suppressing the whole check, which is worse
    than reporting a span we did not interpret.
    """
    code, out, _ = run(["check", str(FIXTURES / "canned_cycle_span.nc")], capsys)
    assert code == EXIT_OK
    assert "unsupported" in out
    assert "structural.unsupported-motion" in out


def test_warnings_alone_do_not_fail_the_run(capsys, tmp_path: Path) -> None:
    program = tmp_path / "warn.nc"
    program.write_text("G21 G90 G54\nS8000 M3\nG1 X1 F100\n", encoding="utf-8")
    code, out, _ = run(["check", str(program)], capsys)
    assert code == EXIT_OK
    assert "warning" in out


def test_output_is_compiler_style(capsys) -> None:
    """`file:line: severity [rule] message`, so editors can jump and grep can filter."""
    code, out, _ = run(["check", str(FIXTURES / "cutter_comp_span.nc")], capsys)
    assert code == EXIT_OK
    line = next(entry for entry in out.splitlines() if "unsupported" in entry)
    prefix, _, remainder = line.partition(": ")
    assert prefix.endswith(":9")
    assert remainder.startswith("unsupported [structural.unsupported-motion] ")


def test_summary_counts_by_severity(capsys, tmp_path: Path) -> None:
    program = tmp_path / "mixed.nc"
    program.write_text("G1 G2 X10\nG41 D1\n", encoding="utf-8")
    _, out, _ = run(["check", str(program)], capsys)
    summary = out.splitlines()[-1]
    assert "error" in summary and "warning" in summary and "unsupported" in summary


def test_diagnostics_go_to_stdout_and_tool_warnings_to_stderr(capsys, tmp_path: Path) -> None:
    """Mixing them interleaves unpredictably the moment stdout is piped."""
    profile = tmp_path / "typo.toml"
    profile.write_text('[machine]\nunits="mm"\n[limits]\nmax_fed = 3000.0\n', encoding="utf-8")
    _, out, err = run(["check", str(BASELINE), "--profile", str(profile)], capsys)
    assert "unrecognized keys" in err
    assert "unrecognized keys" not in out


# --------------------------------------------------------------------------- the profile


def test_check_uses_the_bundled_profile_by_default(capsys) -> None:
    """No --profile must work after `pip install`, where the repo root does not exist."""
    code, out, err = run(["check", str(BASELINE)], capsys)
    assert code == EXIT_OK
    assert "no problems found" in out
    assert "profile" not in err.lower(), err


def test_an_explicit_profile_changes_the_verdict(capsys, tmp_path: Path) -> None:
    """Proof the --profile argument is actually used, not just accepted."""
    strict = tmp_path / "strict.toml"
    strict.write_text('[machine]\nunits="mm"\n[limits]\nmax_feed = 100.0\n', encoding="utf-8")
    code, out, _ = run(["check", str(BASELINE), "--profile", str(strict)], capsys)
    assert code == EXIT_ERRORS_FOUND
    assert "process.feed-too-high" in out


def test_malformed_profile_toml_exits_two_rather_than_tracebacking(capsys, tmp_path: Path) -> None:
    """Exit code 2 is documented for an unusable profile; a traceback is not honouring that.

    This was a real gap: `load_profile` did not wrap `tomllib.TOMLDecodeError`, so a typo in a
    hand-edited profile crashed the CLI instead of producing a message and code 2.
    """
    bad = tmp_path / "broken.toml"
    bad.write_text("this is not = valid toml [[[\n", encoding="utf-8")
    code, _, err = run(["check", str(BASELINE), "--profile", str(bad)], capsys)
    assert code == 2
    assert "invalid TOML" in err and "broken.toml" in err


def test_a_profile_typo_is_surfaced(capsys, tmp_path: Path) -> None:
    """`max_fed = 3000` silently disables the feed check unless the CLI says so."""
    profile = tmp_path / "typo.toml"
    profile.write_text('[machine]\nunits="mm"\n[limits]\nmax_fed = 3000.0\n', encoding="utf-8")
    code, _, err = run(["check", str(BASELINE), "--profile", str(profile)], capsys)
    assert code == EXIT_OK
    assert "limits.max_fed" in err


# --------------------------------------------------------------------------- parse


def test_parse_lists_commands(capsys) -> None:
    code, out, _ = run(["parse", str(BASELINE)], capsys)
    assert code == EXIT_OK
    assert "19 command(s), 0 parse error(s)" in out
    assert "G21" in out and "motion=" in out


def test_parse_reports_malformed_input_and_exits_one(capsys, tmp_path: Path) -> None:
    program = tmp_path / "bad.nc"
    program.write_text("G1 X Y10\n", encoding="utf-8")
    code, out, _ = run(["parse", str(program)], capsys)
    assert code == EXIT_ERRORS_FOUND
    assert "malformed-word" in out


def test_parse_modal_shows_the_resolved_state(capsys) -> None:
    _, plain, _ = run(["parse", str(BASELINE)], capsys)
    _, detailed, _ = run(["parse", str(BASELINE), "--modal"], capsys)
    assert "feed_mode=" not in plain
    assert "feed_mode=G94" in detailed
    assert "offset=G54" in detailed


# --------------------------------------------------------------------------- block delete


def test_block_delete_is_off_by_default(capsys) -> None:
    """Deleted blocks execute unless asked otherwise, matching the control-panel default."""
    _, executed, _ = run(["parse", str(FIXTURES / "block_delete.nc")], capsys)
    _, skipped, _ = run(["parse", str(FIXTURES / "block_delete.nc"), "--block-delete"], capsys)
    assert "12 command(s)" in executed
    assert "10 command(s)" in skipped


# --------------------------------------------------------------------------- cannot run


def test_missing_file_exits_two_without_a_traceback(capsys, tmp_path: Path) -> None:
    code, out, err = run(["check", str(tmp_path / "nope.nc")], capsys)
    assert code == EXIT_CANNOT_RUN
    assert "No such file" in err
    assert "Traceback" not in err and out == ""


def test_binary_file_exits_two(capsys, tmp_path: Path) -> None:
    program = tmp_path / "part.nc"
    program.write_bytes(b"\x00\x01\x02binary")
    code, _, err = run(["check", str(program)], capsys)
    assert code == EXIT_CANNOT_RUN
    assert "binary" in err


def test_unusable_profile_exits_two(capsys, tmp_path: Path) -> None:
    profile = tmp_path / "bad.toml"
    profile.write_text('[machine]\nunits="furlongs"\n', encoding="utf-8")
    code, _, err = run(["check", str(BASELINE), "--profile", str(profile)], capsys)
    assert code == EXIT_CANNOT_RUN
    assert "furlongs" in err
    assert "Traceback" not in err


def test_head_mount_profile_without_pivot_exits_two(capsys, tmp_path: Path) -> None:
    """The profile refuses to load; the CLI must relay that rather than crash."""
    profile = tmp_path / "head.toml"
    profile.write_text(
        '[machine]\nunits="mm"\n[kinematics]\nrotary_mount="head"\n', encoding="utf-8"
    )
    code, _, err = run(["check", str(BASELINE), "--profile", str(profile)], capsys)
    assert code == EXIT_CANNOT_RUN
    assert "pivot_to_tip" in err


def test_latin1_fallback_is_reported(capsys, tmp_path: Path) -> None:
    program = tmp_path / "part.nc"
    program.write_bytes("(temp 20\xb0C)\nG21 G90 G54\nM30\n".encode("latin-1"))
    code, _, err = run(["check", str(program)], capsys)
    assert code == EXIT_OK
    assert "latin-1" in err


def test_no_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as raised:
        main([])
    assert raised.value.code == EXIT_CANNOT_RUN


# --------------------------------------------------------------------------- every fixture


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_check_runs_on_every_fixture(name: str, capsys) -> None:
    """Never exit 2 and never raise: a fixture is readable G-code even when deliberately broken."""
    code, out, _ = run(["check", str(FIXTURES / name)], capsys)
    assert code in {EXIT_OK, EXIT_ERRORS_FOUND}, out
    assert out.strip()


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_parse_runs_on_every_fixture(name: str, capsys) -> None:
    code, out, _ = run(["parse", str(FIXTURES / name)], capsys)
    assert code in {EXIT_OK, EXIT_ERRORS_FOUND}
    assert "command(s)" in out


# --------------------------------------------------------------------------- no Qt


def test_the_cli_path_imports_no_qt() -> None:
    """Run in a subprocess: this venv has `[gui]` installed, so an in-process check proves nothing.

    The complementary guard is CI's `.[dev]`-only job, where Qt is not installed at all.
    """
    program = (
        "import sys\n"
        "from foursight.cli import main\n"
        f"main(['check', {str(BASELINE)!r}])\n"
        "leaked = sorted(m for m in sys.modules"
        " if m.split('.')[0] in {'PySide6', 'shiboken6', 'pyqtgraph', 'OpenGL'})\n"
        "print('LEAKED=' + ','.join(leaked))\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )
    reported = next(line for line in result.stdout.splitlines() if line.startswith("LEAKED="))
    assert reported == "LEAKED=", reported


def test_the_console_script_is_installed_and_runs() -> None:
    """The entry point declared in pyproject.toml, exercised as a user would.

    Both filename forms are checked: Windows installs `foursight.exe` into `Scripts/`, and looking
    only for the extensionless name made this skip on Windows — the one platform where a
    console-script shim is most likely to be the thing that breaks.
    """
    directory = Path(sys.executable).parent
    executable = next(
        (
            candidate
            for candidate in (directory / "foursight", directory / "foursight.exe")
            if candidate.exists()
        ),
        None,
    )
    if executable is None:  # pragma: no cover - only when not installed as a script
        pytest.skip(f"console script not found in {directory}")
    result = subprocess.run(  # noqa: S603
        [str(executable), "check", str(BASELINE)], capture_output=True, text=True, check=False
    )
    assert result.returncode == EXIT_OK
    assert "no problems found" in result.stdout


def test_fixture_text_helper_still_matches_disk() -> None:
    """Guards the assumption the tests above rest on: the CLI reads the same bytes we do."""
    assert fixture_text("baseline_4axis.nc") == BASELINE.read_text(encoding="utf-8")
