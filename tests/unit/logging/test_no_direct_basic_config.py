"""Regression tests for centralized logging setup (#1404)."""

from __future__ import annotations

import ast
from pathlib import Path


def _direct_basic_config_calls() -> list[str]:
    root = Path("hephaestus")
    calls: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "basicConfig"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logging"
            ):
                calls.append(f"{path}:{node.lineno}")
    return calls


def test_no_direct_logging_basic_config_calls_remain() -> None:
    """CLI modules must route through hephaestus.logging.utils.setup_logging."""
    assert _direct_basic_config_calls() == []
