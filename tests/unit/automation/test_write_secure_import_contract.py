"""Import-contract tests for automation secure writes."""

from __future__ import annotations

import ast
from pathlib import Path

import hephaestus.automation.github_api as github_api_module


def test_github_api_does_not_export_write_secure_in_all() -> None:
    """write_secure is not part of the public API surface of github_api.

    The module imports write_secure for internal use, but the name
    must not be part of the public ``__all__`` — callers should import
    from hephaestus.io.utils directly.
    """
    assert "write_secure" not in github_api_module.__all__


def test_automation_modules_do_not_import_write_secure_from_github_api() -> None:
    """First-party automation code should import write_secure from hephaestus.io.utils."""
    automation_root = Path(__file__).parents[3] / "hephaestus" / "automation"
    offenders: list[str] = []

    for path in sorted(automation_root.rglob("*.py")):
        if path.name == "github_api.py":
            continue

        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue

            module = node.module or ""
            imports_write_secure = any(alias.name == "write_secure" for alias in node.names)
            imports_github_api = module == "hephaestus.automation.github_api" or (
                node.level == 1 and module == "github_api"
            )
            if imports_write_secure and imports_github_api:
                rel_path = path.relative_to(automation_root.parent.parent)
                offenders.append(f"{rel_path}:{node.lineno}")

    assert offenders == []
