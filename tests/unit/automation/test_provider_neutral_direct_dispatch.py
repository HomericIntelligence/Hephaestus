"""Static guards for provider-neutral direct-agent automation dispatch."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

PROVIDER_NEUTRAL_FILES = [
    "hephaestus/automation/agent_stage.py",
    "hephaestus/automation/planner.py",
    "hephaestus/automation/plan_reviewer.py",
    "hephaestus/automation/_implement_phase.py",
    "hephaestus/automation/implementer_phase_runner.py",
    "hephaestus/automation/implementer.py",
    "hephaestus/automation/_review_phase.py",
    "hephaestus/automation/pr_reviewer.py",
    "hephaestus/automation/audit_reviewer.py",
    "hephaestus/automation/review_validator.py",
    "hephaestus/automation/comment_difficulty.py",
    "hephaestus/automation/address_review.py",
    "hephaestus/automation/ci_driver.py",
    "hephaestus/automation/ci_fix_orchestrator.py",
    "hephaestus/automation/post_merge_processor.py",
    "hephaestus/automation/learn.py",
    "hephaestus/automation/follow_up.py",
    "hephaestus/automation/pr_manager.py",
    "hephaestus/github/tidy.py",
    "hephaestus/github/fleet_sync/conflict_resolver.py",
]

CODEX_ONLY_NAMES = {
    "is_codex",
    "run_codex_text",
    "run_codex_session",
    "resume_codex_session",
    "codex_json_stdout",
}


def _node_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _codex_string_compare(node: ast.Compare) -> bool:
    comparators = [node.left, *node.comparators]
    return any(isinstance(item, ast.Constant) and item.value == "codex" for item in comparators)


@pytest.mark.parametrize("relative_path", PROVIDER_NEUTRAL_FILES)
def test_direct_agent_dispatch_has_no_codex_only_runtime_branches(relative_path: str) -> None:
    """Automation call sites must route Codex and Pi through neutral runtime helpers."""
    path = REPO_ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported = {alias.name for alias in node.names}
            offenders = imported & CODEX_ONLY_NAMES
            if offenders:
                violations.append(f"line {node.lineno}: imports {sorted(offenders)}")
        elif isinstance(node, ast.Call):
            name = _node_name(node.func)
            if name in CODEX_ONLY_NAMES:
                violations.append(f"line {node.lineno}: calls {name}()")
        elif isinstance(node, ast.Compare) and _codex_string_compare(node):
            violations.append(f"line {node.lineno}: compares against 'codex'")

    assert violations == []
