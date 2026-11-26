"""AST guard: the extractable git policy tier stays below the product layer.

Issue #3515 (epic #3504) moves the stdlib-only GitHub modules into
``hephaestus/git/github/policy/``. This guard makes the destination an
executable boundary: a module under ``hephaestus/git/`` must import no
``hephaestus.automation.*`` symbol. The dependency arrow points only one way,
automation -> library (ADR-0001).

The scan walks every import node, so a function-local or ``TYPE_CHECKING``
import cannot escape the check. Relative imports are resolved with
``importlib`` before the comparison, because a text prefix check misses
``from ...automation import models``.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import hephaestus

LIB_ROOT = Path(hephaestus.__file__).parent
GIT_ROOT = LIB_ROOT / "git"
_AUTOMATION_ROOT = "hephaestus.automation"


def _package_of(py: Path, lib_root: Path) -> str:
    """Return the dotted package of *py* relative to *lib_root*."""
    try:
        parts = py.parent.relative_to(lib_root).parts
    except ValueError:
        return ""
    return ".".join(("hephaestus", *parts))


def _resolve_relative(level: int, module: str | None, package: str) -> str:
    """Resolve a relative-import base the way ``importlib`` would.

    An unresolvable base is returned unchanged, so it can only match an
    automation target when the literal text already names one.
    """
    if not level:
        return module or ""
    base = "." * level + (module or "")
    try:
        return importlib.util.resolve_name(base, package)
    except (ImportError, ValueError, TypeError):
        return base


def find_automation_imports(git_root: Path, lib_root: Path = LIB_ROOT) -> list[str]:
    """Return every ``file:line: module`` edge from *git_root* to automation.

    Args:
        git_root: Directory to scan recursively for Python modules.
        lib_root: Directory that holds the ``hephaestus`` package.

    Returns:
        Sorted ``"<relative path>:<line>: <module>"`` findings. The list is
        empty when the tree is clean.

    """
    findings: list[str] = []
    for py in sorted(git_root.rglob("*.py")):
        package = _package_of(py, lib_root)
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                edges = [(node.lineno, alias.name) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = _resolve_relative(node.level, node.module, package)
                edges = [
                    (node.lineno, base if alias.name == "*" else f"{base}.{alias.name}")
                    for alias in node.names
                ]
            else:
                continue
            for lineno, target in edges:
                if target == _AUTOMATION_ROOT or target.startswith(f"{_AUTOMATION_ROOT}."):
                    findings.append(f"{py.relative_to(lib_root)}:{lineno}: {target}")
    return findings


def test_git_package_discovery_is_not_empty() -> None:
    """The guard must fail when its glob stops matching files."""
    assert GIT_ROOT.is_dir(), f"missing git package root: {GIT_ROOT}"
    assert list(GIT_ROOT.rglob("*.py")), "no Python modules discovered under hephaestus/git"


def test_git_package_imports_no_automation_symbols() -> None:
    """Every module under ``hephaestus/git/`` stays below the product layer."""
    findings = find_automation_imports(GIT_ROOT)
    assert findings == [], (
        "hephaestus/git must import zero hephaestus.automation symbols:\n" + "\n".join(findings)
    )


def test_detector_reports_a_relative_automation_edge(tmp_path: Path) -> None:
    """A planted relative edge is detected, so a clean result is not vacuous."""
    lib_root = tmp_path / "hephaestus"
    package_root = lib_root / "git" / "github" / "policy"
    package_root.mkdir(parents=True)
    (package_root / "pagination.py").write_text(
        "from ....automation import models\n",
        encoding="utf-8",
    )

    findings = find_automation_imports(lib_root / "git", lib_root)

    assert findings == [
        "git/github/policy/pagination.py:1: hephaestus.automation.models",
    ]
