"""Smoke tests: the package is installed, the layout exists, and two invariants hold.

Deliberately narrow. These exist so a red CI run means something real, rather than the suite
collecting nothing at all — see TASKS.md T0.5. Behavioural tests arrive with M1.
"""

import ast
import importlib
import importlib.util
import pkgutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import foursight

# PLAN.md § Repository Layout. Asserted explicitly so that dropping or renaming a module is a
# test failure now, rather than a surprise three milestones later.
HEADLESS_MODULES = (
    "foursight.cli",
    "foursight.parser.model",
    "foursight.parser.tokenizer",
    "foursight.parser.resolver",
    "foursight.machine.state",
    "foursight.machine.profile",
    "foursight.machine.kinematics",
    "foursight.sim.interpolate",
    "foursight.sim.segments",
    "foursight.sim.timing",
    "foursight.sim.simulator",
    "foursight.verify.rules",
    "foursight.verify.report",
    "foursight.verify.checks",
    "foursight.fix.fixes",
    "foursight.fix.differ",
    "foursight.fileio.loader",
)

GUI_MODULES = (
    "foursight.gui.app",
    "foursight.gui.main_window",
    "foursight.gui.viewport3d",
    "foursight.gui.picking",
    "foursight.gui.editor",
    "foursight.gui.timeline",
    "foursight.gui.diagnostics_panel",
)

QT_ROOTS = ("PySide6", "shiboken6", "pyqtgraph", "OpenGL")

# CI runs a job with only the [dev] extra installed, to enforce that headless modules never need
# Qt. These modules legitimately do, so they are skipped there rather than failing the job.
# They are plain stubs today; the skip becomes load-bearing when gui/ really imports Qt in T2.7.
requires_qt = pytest.mark.skipif(
    importlib.util.find_spec("PySide6") is None, reason="needs the [gui] extra"
)


def test_package_is_installed() -> None:
    assert foursight.__version__


def test_no_module_named_io() -> None:
    """The file-loading package is `fileio`; `io` would shadow the stdlib module."""
    names = {module.name for module in pkgutil.walk_packages(foursight.__path__, "foursight.")}
    assert "foursight.fileio" in names
    assert "foursight.io" not in names


@pytest.mark.parametrize("name", HEADLESS_MODULES)
def test_headless_module_imports(name: str) -> None:
    assert importlib.import_module(name) is not None


@requires_qt
@pytest.mark.parametrize("name", GUI_MODULES)
def test_gui_module_imports(name: str) -> None:
    assert importlib.import_module(name) is not None


def test_headless_modules_do_not_import_qt() -> None:
    """Every module outside `foursight.gui` must import without Qt.

    Run in a subprocess on purpose. This venv has the `[gui]` extra installed, so Qt is
    importable here; and once `gui/` genuinely imports Qt, a same-process `sys.modules` check
    would silently become dependent on test ordering. The subprocess makes the assertion mean
    the same thing in every environment.

    The complementary check is CI's `.[dev]`-only job (T0.6), where Qt is not installed at all.
    """
    program = textwrap.dedent(
        f"""
        import importlib, pkgutil, sys
        import foursight

        names = [m.name for m in pkgutil.walk_packages(foursight.__path__, "foursight.")
                 if not m.name.startswith("foursight.gui")]
        for name in names:
            importlib.import_module(name)

        leaked = sorted(m for m in sys.modules if m.split(".")[0] in {QT_ROOTS!r})
        if leaked:
            print("Qt/GL reached headless modules:", leaked)
            raise SystemExit(1)
        if len(names) < 15:
            print(f"only {{len(names)}} headless modules walked; the check is not doing its job")
            raise SystemExit(1)
        """
    )
    result = subprocess.run(  # noqa: S603  # fixed argv, no shell, interpreter is sys.executable
        [sys.executable, "-c", program], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _imported_module_names(path: Path) -> set[str]:
    """Every module named in an import statement, including relative and function-local ones."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


def test_parser_model_does_not_import_machine() -> None:
    """`parser/model.py` must not import from `machine/` (PLAN.md § Conventions).

    Static check rather than a `sys.modules` one: this must hold for imports inside functions
    too, and it must not depend on what else the test session already imported.
    """
    model = importlib.import_module("foursight.parser.model")
    imported = _imported_module_names(Path(model.__file__))
    offenders = sorted(name for name in imported if "machine" in name.split("."))
    assert not offenders, f"parser/model.py imports from machine/: {offenders}"
