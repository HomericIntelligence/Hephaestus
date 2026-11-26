"""Regression test for ADR-0001 boundary contract.

Library subpackages of `hephaestus` MUST NOT import from
`hephaestus.automation`. The dependency arrow points only one way:
automation -> library.

The guard is AST-based: it walks every ``Import``/``ImportFrom`` node and
resolves relative imports using Python's package-relative rules, so a hidden
``from ..automation import x`` cannot slip through the old text-prefix check.
The scanned sources are never executed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import hephaestus

LIB_ROOT = Path(hephaestus.__file__).parent
_AUTOMATION_PREFIX = "hephaestus.automation"


def containing_package(py: Path, root: Path) -> str:
    """Return the dotted package that holds one source file.

    ``hephaestus/x/__init__.py`` belongs to package ``hephaestus.x``,
    ``hephaestus/x/mod.py`` belongs to ``hephaestus.x`` as well. `.`-relative
    imports anchor to this package; each extra leading dot walks one package
    up.
    """
    parts = list(py.relative_to(root).with_suffix("").parts)
    # The last segment is either the module stem or "__init__"; the package
    # is everything before it.
    parts.pop()
    return ".".join(parts)


def iter_import_targets(py: Path, package: str) -> list[tuple[int, str]]:
    """Yield ``(lineno, dotted target)`` for every import of one module.

    Absolute imports keep their written dotted name. Relative imports resolve
    against ``package`` with Python package-relative rules, so
    ``from ..automation import pipeline_cli`` in ``hephaestus/utils/mod.py``
    yields ``hephaestus.automation.pipeline_cli``.
    """
    report: list[tuple[int, str]] = []
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    package_parts = package.split(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                report.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                target = node.module or ""
            else:
                base = package_parts[: len(package_parts) - node.level + 1]
                sub = node.module.split(".") if node.module else []
                target = ".".join([*base, *sub])
            for alias in node.names:
                report.append((node.lineno, f"{target}.{alias.name}"))
    return report


def find_boundary_violations(root: Path) -> list[str]:
    """Return one violation string per import that resolves past the boundary."""
    violations: list[str] = []
    for py in sorted(root.rglob("*.py")):
        if "automation" in py.relative_to(root).parts:
            continue
        package = containing_package(py, root)
        for lineno, target in iter_import_targets(py, package):
            if target.startswith(_AUTOMATION_PREFIX):
                violations.append(f"{py.relative_to(root)}:{lineno}: {target}")
    return violations


def test_no_library_subpackage_imports_automation() -> None:
    """Verify library packages do not import from hephaestus.automation."""
    violations = find_boundary_violations(LIB_ROOT)
    assert not violations, (
        "library subpackages must not import from hephaestus.automation:\n" + "\n".join(violations)
    )


def _plant_repo(tmp_path: Path, layout: dict[str, str]) -> None:
    """Create a synthetic package tree from ``{relative path: contents}``."""
    for relative, contents in layout.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")


@pytest.mark.parametrize(
    "module_path, statement",
    [
        ("hephaestus/lib/mod.py", "from ..automation.prompts import fence_untrusted"),
        ("hephaestus/config.py", "from .automation import pipeline_cli"),
        ("hephaestus/lib/mod.py", "from hephaestus.automation.prompts import fence_untrusted"),
        ("hephaestus/lib/mod.py", "import hephaestus.automation.git_utils"),
        ("hephaestus/lib/mod.py", "import hephaestus.automation  # type: ignore[import]"),
        ("hephaestus/__init__.py", "from .automation.prompts import fence_untrusted"),
    ],
)
def test_boundary_guard_flags_automation_import_forms(
    tmp_path: Path, module_path: str, statement: str
) -> None:
    """Each import form that reaches the product layer reports one violation."""
    _plant_repo(
        tmp_path,
        {
            module_path: f"from __future__ import annotations\n{statement}\n",
            "hephaestus/automation/__init__.py": "",
            "hephaestus/automation/prompts.py": "",
        },
    )
    violations = find_boundary_violations(tmp_path)
    assert len(violations) == 1, violations
    assert _AUTOMATION_PREFIX in violations[0]


def test_boundary_guard_allows_relative_sibling_imports(tmp_path: Path) -> None:
    """A relative import between library subpackages reports no violation."""
    _plant_repo(
        tmp_path,
        {
            "hephaestus/git/__init__.py": "",
            "hephaestus/git/ops.py": "from ..utils.os_helpers import run\n",
            "hephaestus/utils/__init__.py": "",
            "hephaestus/utils/os_helpers.py": "def run():\n    pass\n",
            "hephaestus/automation/__init__.py": "",
        },
    )
    assert find_boundary_violations(tmp_path) == []


def test_boundary_guard_ignores_automation_layer(tmp_path: Path) -> None:
    """Direct imports inside the product layer are not library violations."""
    _plant_repo(
        tmp_path,
        {
            "hephaestus/automation/prompts.py": "x = 1\n",
            "hephaestus/automation/pipeline_cli.py": (
                "from .prompts import x\nfrom hephaestus.utils.os_helpers import run\n"
            ),
        },
    )
    assert find_boundary_violations(tmp_path) == []
