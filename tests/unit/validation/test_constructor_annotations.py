"""Guard explicit ``-> None`` annotations on source constructors."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPO_ROOT / "hephaestus"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def test_all_source_constructors_explicitly_return_none() -> None:
    """Every package constructor must declare an explicit ``None`` return."""
    missing: list[str] = []

    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != "__init__":
                continue

            returns_none = isinstance(node.returns, ast.Constant) and node.returns.value is None
            if not returns_none:
                relative_path = path.relative_to(REPO_ROOT)
                missing.append(f"{relative_path}:{node.lineno}")

    assert missing == [], f"Constructors missing `-> None`: {missing}"


def test_ruff_enforces_special_method_return_annotations() -> None:
    """The normal Ruff configuration must reject missing constructor returns."""
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    selected_rules = config["tool"]["ruff"]["lint"]["select"]

    assert "ANN204" in selected_rules
