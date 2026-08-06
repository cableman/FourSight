"""PyInstaller one-dir build for the current OS.

**One-dir only, never one-file.** A one-file bundle re-extracts ~200 MB on every launch and
trips Windows AV heuristics (PLAN.md § Tech Stack).

Usage::

    .venv/bin/python scripts/build.py                       # bundle the GUI entry point
    .venv/bin/python scripts/build.py --entry spikes/gl_window.py --name gl-spike
    .venv/bin/python scripts/build.py --windowed            # release: suppress the console

The console is left **on** by default. The T0.8 packaging spike exists to observe *how* a bundled
GL app fails to start on a clean Windows VM, and ``--windowed`` discards exactly the output that
would tell us. Turn it on for release builds only.
"""

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENTRY = REPO_ROOT / "src" / "foursight" / "gui" / "app.py"
DEFAULT_NAME = "foursight"

# Extra modules PyInstaller's analysis misses. Expected to gain PySide6/PyOpenGL plugin entries
# once the T0.8 spike reports what a bundled GL app actually fails to find; empty until then,
# because guessing hidden imports hides the real failure.
HIDDEN_IMPORTS: tuple[str, ...] = ()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PyInstaller one-dir build for FourSight.")
    parser.add_argument(
        "--entry", type=Path, default=DEFAULT_ENTRY, help="entry-point script to bundle"
    )
    parser.add_argument("--name", default=DEFAULT_NAME, help="name of the bundled application")
    parser.add_argument(
        "--windowed", action="store_true", help="suppress the console window (release builds)"
    )
    parser.add_argument(
        "--clean", action="store_true", help="clear the PyInstaller cache before building"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the command without running it"
    )
    return parser.parse_args(argv)


def pyinstaller_command(args: argparse.Namespace) -> list[str]:
    """Build the argv for PyInstaller. `--onedir` is not configurable on purpose."""
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onedir",
        "--noconfirm",
        "--name",
        args.name,
        "--distpath",
        str(REPO_ROOT / "dist"),
        "--workpath",
        str(REPO_ROOT / "build"),
        # Keep the generated .spec out of the repo root.
        "--specpath",
        str(REPO_ROOT / "build"),
        # src-layout: the package is not importable from the repo root.
        "--paths",
        str(REPO_ROOT / "src"),
    ]
    if args.windowed:
        cmd.append("--windowed")
    if args.clean:
        cmd.append("--clean")
    for module in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", module]
    cmd.append(str(args.entry))
    return cmd


def report_output(name: str) -> int:
    """Confirm a one-dir tree landed in dist/ and describe it."""
    bundle = REPO_ROOT / "dist" / name
    if not bundle.is_dir():
        print(f"build reported success but {bundle} is not a directory", file=sys.stderr)
        return 1
    executable = bundle / (f"{name}.exe" if sys.platform == "win32" else name)
    if not executable.exists():
        print(f"no executable at {executable}", file=sys.stderr)
        return 1
    files = [p for p in bundle.rglob("*") if p.is_file()]
    size_mb = sum(p.stat().st_size for p in files) / 1e6
    print(f"one-dir bundle: {bundle}  ({len(files)} files, {size_mb:.1f} MB)")
    print(f"executable:     {executable}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.entry.is_file():
        print(f"entry point not found: {args.entry}", file=sys.stderr)
        return 2
    if importlib.util.find_spec("PyInstaller") is None:
        print('PyInstaller missing — run: .venv/bin/pip install -e ".[dev]"', file=sys.stderr)
        return 2

    cmd = pyinstaller_command(args)
    print(" ".join(cmd), flush=True)
    if args.dry_run:
        return 0

    result = subprocess.run(cmd, cwd=REPO_ROOT, check=False)
    if result.returncode != 0:
        return result.returncode
    return report_output(args.name)


if __name__ == "__main__":
    sys.exit(main())
