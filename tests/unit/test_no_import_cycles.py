"""Regression tests for import cycles between hephaestus.github and hephaestus.automation.

These tests use fresh subprocesses so that a partial-import failure in one test
cannot contaminate sys.modules for the next. The AST layering test enforces the
structural invariant at the source level so CI catches regressions before runtime.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import hephaestus.github


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )


def test_planner_imports_cleanly() -> None:
    """Direct repro of the original circular-import failure."""
    r = _run("from hephaestus.automation.planner import main")
    assert r.returncode == 0, f"planner import failed:\n{r.stderr}"


def test_packages_import_in_either_order() -> None:
    """Order-dependent cycles surface only in one direction; test both."""
    r1 = _run("import hephaestus.github, hephaestus.automation")
    assert r1.returncode == 0, f"github-first import failed:\n{r1.stderr}"

    r2 = _run("import hephaestus.automation, hephaestus.github")
    assert r2.returncode == 0, f"automation-first import failed:\n{r2.stderr}"


def test_console_script_entry_points_resolve() -> None:
    """hephaestus-fleet-sync and hephaestus-tidy must still import cleanly."""
    r = _run(
        "from hephaestus.github.fleet_sync import main as fs; "
        "from hephaestus.github.tidy import main as td; "
        "assert callable(fs) and callable(td)"
    )
    assert r.returncode == 0, f"console-script entry-point import failed:\n{r.stderr}"


def test_github_package_does_not_import_automation() -> None:
    """Structural invariant: hephaestus/github/ must not import from hephaestus.automation.

    This enforces the one-way layering boundary.  Any violation here will also
    eventually become a circular-import at runtime.
    """
    github_dir = Path(hephaestus.github.__file__).parent
    offenders: list[str] = []

    for py in sorted(github_dir.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("hephaestus.automation"):
                    names = ", ".join(alias.name for alias in node.names)
                    offenders.append(f"{py}:{node.lineno}  from {module} import {names}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("hephaestus.automation"):
                        offenders.append(f"{py}:{node.lineno}  import {alias.name}")

    assert not offenders, (
        "hephaestus.github → hephaestus.automation import edge detected "
        "(causes circular imports):\n" + "\n".join(offenders)
    )
