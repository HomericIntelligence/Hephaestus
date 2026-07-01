"""Structural guards for validation CLI parser standardization."""

from __future__ import annotations

import ast
from pathlib import Path

VALIDATION_ROOT = Path("hephaestus/validation")


def test_validation_clis_do_not_construct_argparse_parsers_directly() -> None:
    """Validation entry points use shared parser helpers, not local argparse boilerplate."""
    offenders: list[str] = []
    for path in sorted(VALIDATION_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                if (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "argparse"
                    and node.func.attr == "ArgumentParser"
                ):
                    offenders.append(f"{path}:{node.lineno}")
            elif isinstance(node.func, ast.Name) and node.func.id == "ArgumentParser":
                offenders.append(f"{path}:{node.lineno}")

    assert offenders == []
