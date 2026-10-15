"""Regression tests for import cycles in the hephaestus core library.

These tests use fresh subprocesses so that a partial-import failure in one test
cannot contaminate sys.modules for the next. The AST layering test enforces the
one-way automation-to-library boundary at the source level so CI catches
regressions before runtime.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

import hephaestus.github


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )


def test_github_package_imports_cleanly() -> None:
    """Direct repro of the original circular-import failure."""
    r = _run("import hephaestus.github")
    assert r.returncode == 0, f"hephaestus.github import failed:\n{r.stderr}"


def test_console_script_entry_points_resolve() -> None:
    """hephaestus-fleet-sync and hephaestus-tidy must still import cleanly."""
    r = _run(
        "from hephaestus.github.fleet_sync import main as fs; "
        "from hephaestus.github.tidy import main as td; "
        "assert callable(fs) and callable(td)"
    )
    assert r.returncode == 0, f"console-script entry-point import failed:\n{r.stderr}"


def test_removed_issue_guard_recovery_command_is_not_packaged() -> None:
    """The installed command surface must not expose obsolete guard recovery."""
    project_root = Path(__file__).resolve().parents[3]
    metadata = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert "hephaestus-recover-issue-guard" not in metadata["project"]["scripts"]


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
