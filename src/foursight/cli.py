"""Headless CLI: ``foursight parse`` and ``foursight check``.

Imports **no Qt**, and must not start to: this is M1's deliverable and has to work from a
`.[dev]`-only install. CI enforces it with a job that installs without the `[gui]` extra.

Diagnostics and the summary go to **stdout**, as linters do; **stderr** carries problems with the
tool's own inputs (an unreadable file, an unusable profile, a profile key we do not recognize).
Mixing the two interleaves unpredictably the moment stdout is piped.

Exit codes:

- ``0`` — ran successfully, no errors found
- ``1`` — errors found in the program
- ``2`` — could not run: unreadable file, unusable profile, bad usage

``unsupported`` and ``warning`` diagnostics are reported but do **not** fail the run. A program that
legitimately contains canned cycles should still pass a build pipeline; refusing it would push users
toward suppressing the whole check.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from foursight.fileio.loader import FileLoadError, LoadedFile, load
from foursight.machine.profile import (
    MachineProfile,
    ProfileError,
    default_profile_path,
    load_profile,
)
from foursight.parser.model import Command
from foursight.parser.resolver import ParseResult, parse
from foursight.verify.report import Diagnostic, Severity
from foursight.verify.rules import Program, verify

EXIT_OK = 0
EXIT_ERRORS_FOUND = 1
EXIT_CANNOT_RUN = 2

PROGRAM_NAME = "foursight"


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point declared in pyproject.toml."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (FileLoadError, ProfileError) as exc:
        # Both are "we cannot proceed" conditions with a message written for a human. A traceback
        # here would bury the one line that matters.
        _fail(str(exc))
        return EXIT_CANNOT_RUN
    except OSError as exc:
        _fail(f"{exc.filename or ''}: {exc.strerror or exc}".lstrip(": "))
        return EXIT_CANNOT_RUN


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM_NAME, description="Parse, verify and preview 4-axis CNC G-code."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    parse_cmd = subcommands.add_parser("parse", help="dump the parsed commands")
    _add_common(parse_cmd)
    parse_cmd.add_argument(
        "--modal", action="store_true", help="also show each block's resolved modal state"
    )
    parse_cmd.set_defaults(handler=run_parse)

    check_cmd = subcommands.add_parser("check", help="run the verifier")
    _add_common(check_cmd)
    check_cmd.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="machine profile TOML (default: the profile bundled with foursight)",
    )
    check_cmd.set_defaults(handler=run_check)
    return parser


def _add_common(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("file", type=Path, help="the .nc program to read")
    sub.add_argument(
        "--block-delete",
        action="store_true",
        help=(
            "treat /-prefixed blocks as deleted. Off by default, matching the common "
            "control-panel default, where deleted blocks still execute"
        ),
    )


def run_parse(args: argparse.Namespace) -> int:
    loaded = load(args.file)
    _report_load(loaded)
    result = parse(loaded.text, block_delete=args.block_delete)

    for command in result.commands:
        print(_format_command(command, show_modal=args.modal))
    for error in result.errors:
        _print_problem(args.file, error.line, "error", error.kind, error.message)

    # Findings and summary both go to stdout, as linters do; stderr is reserved for problems with
    # the tool itself. Mixing the two interleaves unpredictably as soon as stdout is piped.
    print(f"{len(result.commands)} command(s), {len(result.errors)} parse error(s)")
    return EXIT_ERRORS_FOUND if result.errors else EXIT_OK


def run_check(args: argparse.Namespace) -> int:
    profile = _load_profile(args.profile)
    loaded = load(args.file)
    _report_load(loaded)
    result = parse(loaded.text, block_delete=args.block_delete)
    diagnostics = verify(_program(result, profile, args.block_delete))

    for diagnostic in diagnostics:
        _print_problem(
            args.file,
            diagnostic.line,
            str(diagnostic.severity),
            diagnostic.rule_id,
            diagnostic.message,
        )
    print(_summary(diagnostics))
    return EXIT_ERRORS_FOUND if any(d.is_error for d in diagnostics) else EXIT_OK


def _program(result: ParseResult, profile: MachineProfile, block_delete: bool) -> Program:
    return Program(
        commands=result.commands,
        profile=profile,
        parse_errors=result.errors,
        block_delete=block_delete,
    )


def _load_profile(path: Path | None) -> MachineProfile:
    profile = load_profile(path if path is not None else default_profile_path())
    if profile.unknown_keys:
        # Loading tolerates unknown keys so a newer profile still works, but an unsurfaced typo
        # silently disables a check: `max_fed = 3000` would leave the feed limit unset.
        keys = ", ".join(profile.unknown_keys)
        _warn(f"profile has unrecognized keys, which are ignored: {keys}")
    return profile


def _report_load(loaded: LoadedFile) -> None:
    if loaded.used_fallback:
        _warn(
            f"{loaded.path}: not valid UTF-8, decoded as latin-1; "
            "non-ASCII text in comments may be wrong"
        )


def _format_command(command: Command, *, show_modal: bool) -> str:
    parts = [f"{command.ref.line_no:>6}:"]
    if command.gcodes:
        parts.append(" ".join(f"G{code}" for code in command.gcodes))
    if command.mcodes:
        parts.append(" ".join(f"M{code}" for code in command.mcodes))
    parts.append(f"motion={command.motion or '-'}")
    if command.words:
        parts.append(" ".join(f"{k}{v:g}" for k, v in sorted(command.words.items())))
    line = "  ".join(parts)
    if not show_modal:
        return line
    modal = command.modal_snapshot
    return (
        f"{line}\n"
        f"         units={modal.units} plane=G{modal.plane} dist=G{modal.distance} "
        f"arc=G{modal.arc_distance} feed_mode=G{modal.feed_mode} "
        f"offset={f'G{modal.offset}' if modal.offset else '-'} feed={modal.feed} "
        f"spindle={modal.spindle_on or '-'}@{modal.spindle_rpm} tool={modal.tool} "
        f"comp={f'G{modal.cutter_comp}' if modal.cutter_comp else '-'}"
    )


def _print_problem(path: Path, line: int, severity: str, rule: object, message: str) -> None:
    """Compiler-style, so editors can jump to it and grep can filter it."""
    print(f"{path}:{line}: {severity} [{rule}] {message}")


def _summary(diagnostics: Sequence[Diagnostic]) -> str:
    if not diagnostics:
        return "no problems found"
    counts = {severity: 0 for severity in Severity}
    for diagnostic in diagnostics:
        counts[diagnostic.severity] += 1
    described = ", ".join(
        f"{counts[severity]} {severity}" for severity in Severity if counts[severity]
    )
    return f"{len(diagnostics)} diagnostic(s): {described}"


def _warn(message: str) -> None:
    print(f"{PROGRAM_NAME}: warning: {message}", file=sys.stderr)


def _fail(message: str) -> None:
    print(f"{PROGRAM_NAME}: {message}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
