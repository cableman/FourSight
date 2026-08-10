"""CLI tests (T1.11).

Exit codes are part of the contract — `foursight check` is meant to run in a build pipeline — so
each is asserted explicitly, including the deliberate choice that `unsupported` does *not* fail a
run.

The Qt-free requirement is checked in a subprocess. This venv has the `[gui]` extra installed, so an
in-process check would prove nothing about a `.[dev]`-only install.
"""

import subprocess
import sys
import sysconfig
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from conftest import FIXTURES, fixture_text
from foursight.cli import (
    EXIT_CANNOT_RUN,
    EXIT_ERRORS_FOUND,
    EXIT_OK,
    build_parser,
    main,
)

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


def script_directories() -> list[Path]:
    """Where an installed console script could be, on any platform.

    `sysconfig.get_path("scripts")` is the correct answer and comes first. `sys.executable`'s own
    directory is kept as a fallback because it is right for a Linux venv and costs nothing — but it is
    *wrong on Windows*, where `python.exe` sits one level above `Scripts\\`, and relying on it alone is
    what made this test skip on Windows for eleven commits.
    """
    candidates = [Path(sysconfig.get_path("scripts")), Path(sys.executable).parent]
    return list(dict.fromkeys(candidates))


def find_console_script(name: str) -> Path | None:
    """The installed executable for `name`, or None. Both filename forms, every plausible directory."""
    for directory in script_directories():
        for filename in (name, f"{name}.exe"):
            candidate = directory / filename
            if candidate.exists():
                return candidate
    return None


def declared_console_scripts() -> set[str]:
    """The console scripts this installed distribution says it provides."""
    return {
        entry.name
        for entry in entry_points(group="console_scripts")
        if entry.value.startswith("foursight")
    }


@pytest.mark.parametrize("name", ["foursight", "foursight-gui"])
def test_every_declared_console_script_is_actually_installed(name: str) -> None:
    """A declared entry point with no executable on disk is a broken install, not a skip.

    This used to `pytest.skip` when it could not find the file, which is how it went unnoticed that it
    was looking in the wrong directory on Windows — the one platform where a console-script shim is
    most likely to be what breaks. Asserting instead removes the hiding place: the only legitimate
    reason to skip is the package not being installed as a distribution at all.
    """
    declared = declared_console_scripts()
    if name not in declared:
        pytest.skip(f"{name} is not a declared console script (declared: {sorted(declared)})")
    executable = find_console_script(name)
    assert executable is not None, (
        f"{name} is declared in pyproject.toml but no executable exists; searched "
        f"{[str(directory) for directory in script_directories()]}"
    )


def test_the_console_script_is_installed_and_runs() -> None:
    """The `foursight` entry point exercised as a user would, not through `main()`."""
    executable = find_console_script("foursight")
    if executable is None:  # pragma: no cover - not installed as a distribution
        pytest.skip("foursight console script is not installed")
    result = subprocess.run(  # noqa: S603
        [str(executable), "check", str(BASELINE)], capture_output=True, text=True, check=False
    )
    assert result.returncode == EXIT_OK
    assert "no problems found" in result.stdout


def test_fixture_text_helper_still_matches_disk() -> None:
    """Guards the assumption the tests above rest on: the CLI reads the same bytes we do."""
    assert fixture_text("baseline_4axis.nc") == BASELINE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- dialect (M6)

MACH3_ABSOLUTE = """
[machine]
units = "mm"
[dialect]
name = "mach3"
arc_centre = "absolute"
"""


def _profile(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "profile.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_modal_output_reports_the_profiles_arc_centre(capsys, tmp_path: Path) -> None:
    """`parse --modal` and `check` must not disagree about the same file.

    This is why `--profile` is common to both subcommands rather than `check`-only: without it here,
    --modal would print the default arc mode for a file the verifier reads under another.
    """
    program = tmp_path / "arc.nc"
    program.write_text("G21 G90 G17 G54\nG2 X20 Y0 I10 J0 F600\n", encoding="utf-8")
    code, out, _ = run(
        ["parse", str(program), "--modal", "--profile", str(_profile(tmp_path, MACH3_ABSOLUTE))],
        capsys,
    )
    assert code == EXIT_OK
    assert "arc=G90.1" in out


def test_modal_output_is_incremental_by_default(capsys, tmp_path: Path) -> None:
    program = tmp_path / "arc.nc"
    program.write_text("G21 G90 G17 G54\nG2 X20 Y0 I10 J0 F600\n", encoding="utf-8")
    code, out, _ = run(["parse", str(program), "--modal"], capsys)
    assert code == EXIT_OK
    assert "arc=G91.1" in out


def test_the_dialect_flag_overrides_the_profile(capsys, tmp_path: Path) -> None:
    """--dialect > [dialect].name > linuxcnc. Overriding away resets the controller settings."""
    program = tmp_path / "arc.nc"
    program.write_text("G21 G90 G17 G54\nG2 X20 Y0 I10 J0 F600\n", encoding="utf-8")
    argv = ["parse", str(program), "--modal", "--profile", str(_profile(tmp_path, MACH3_ABSOLUTE))]
    code, out, _ = run([*argv, "--dialect", "linuxcnc"], capsys)
    assert code == EXIT_OK
    assert "arc=G91.1" in out


def test_the_arc_centre_flag_overrides_without_editing_the_profile(capsys, tmp_path: Path) -> None:
    """The flag that makes the CLI half of "configurable in both places" real."""
    program = tmp_path / "arc.nc"
    program.write_text("G21 G90 G17 G54\nG2 X20 Y0 I10 J0 F600\n", encoding="utf-8")
    code, out, _ = run(
        ["parse", str(program), "--modal", "--dialect", "mach3", "--arc-centre", "absolute"],
        capsys,
    )
    assert code == EXIT_OK
    assert "arc=G90.1" in out


def test_the_arc_centre_flag_is_refused_under_linuxcnc(capsys) -> None:
    """Under LinuxCNC the G-code decides it, so accepting the flag would be a false promise."""
    code, _, err = run(["parse", str(BASELINE), "--arc-centre", "absolute"], capsys)
    assert code == EXIT_CANNOT_RUN
    assert "controller setting" in err


def test_an_unknown_dialect_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as raised:
        main(["check", str(BASELINE), "--dialect", "haas"])
    assert raised.value.code == EXIT_CANNOT_RUN


def test_the_dialect_defaults_to_the_profiles(capsys) -> None:
    """A CLI default of "linuxcnc" would silently beat every profile's [dialect].name."""
    assert build_parser().parse_args(["check", str(BASELINE)]).dialect is None


def test_a_subprogram_call_is_reported_without_failing_the_run(capsys, tmp_path: Path) -> None:
    program = tmp_path / "sub.nc"
    program.write_text(
        "G21 G90 G17 G94 G54\nS8000 M3\nG1 X10 F600\nM98 P1000\nM5\nM30\n", encoding="utf-8"
    )
    code, out, _ = run(["check", str(program)], capsys)
    assert code == EXIT_OK
    assert "structural.unsupported-motion" in out
    assert "M98" in out
