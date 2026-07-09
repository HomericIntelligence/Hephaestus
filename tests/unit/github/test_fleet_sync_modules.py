"""Fleet-sync package split and facade compatibility tests."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FLEET_SYNC_DIR = _REPO_ROOT / "hephaestus" / "github" / "fleet_sync"
_MAX_FUNCTION_LINES = 80


def test_fleet_sync_facade_preserves_public_imports() -> None:
    """The old hephaestus.github.fleet_sync import surface stays available."""
    module = importlib.import_module("hephaestus.github.fleet_sync")

    assert callable(module.main)
    assert callable(module.resolve_fleet_config)
    assert callable(module.process_repo)
    assert module.PRStatus.READY.name == "READY"


def test_fleet_sync_is_split_into_focused_modules() -> None:
    """The monolith is now a package with responsibility-focused submodules."""
    for name in (
        "models",
        "gpg",
        "config",
        "pr_api",
        "git_ops",
        "conflict_resolver",
        "sync_coordinator",
        "cli",
    ):
        assert importlib.import_module(f"hephaestus.github.fleet_sync.{name}") is not None


def test_fleet_sync_functions_stay_decomposed() -> None:
    """Fleet-sync orchestration functions stay below the decomposition budget."""
    violations: list[str] = []
    for path in sorted(_FLEET_SYNC_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                length = (node.end_lineno or node.lineno) - node.lineno + 1
                if length > _MAX_FUNCTION_LINES:
                    rel_path = path.relative_to(_REPO_ROOT)
                    violations.append(f"{rel_path}:{node.lineno} {node.name} has {length} lines")

    assert violations == []
