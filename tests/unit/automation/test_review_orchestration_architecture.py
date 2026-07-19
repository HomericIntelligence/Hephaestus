"""Architecture guardrails for review-phase orchestration collaborators."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ClassBudget:
    """Maximum size permitted for one collaborator and its methods."""

    max_lines: int
    max_method_lines: int


_BUDGETS = {
    "hephaestus.automation._review_conflict_resolver.ReviewConflictResolver": ClassBudget(
        max_lines=260,
        max_method_lines=80,
    ),
    "hephaestus.automation._review_loop.ReviewLoopCoordinator": ClassBudget(
        max_lines=260,
        max_method_lines=80,
    ),
}

_REVIEW_PHASE_FACADE_BUDGETS = {
    "_resolve_conflict_before_review": 60,
    "_run_impl_review_loop": 60,
}

_FORBIDDEN_LOOP_IMPORTS = {
    "hephaestus.agents",
    "hephaestus.github",
    "hephaestus.automation.git_utils",
    "hephaestus.automation.github_api",
    "hephaestus.automation.models",
    "hephaestus.automation.status_tracker",
}


def _module_path(dotted: str) -> Path:
    """Map a dotted module/class reference to its source file."""
    module_name = dotted.rsplit(".", 1)[0]
    return Path(*module_name.split(".")).with_suffix(".py")


def _class_node(dotted: str) -> ast.ClassDef:
    """Return the named class node from a repository source file."""
    class_name = dotted.rsplit(".", 1)[1]
    tree = ast.parse(_module_path(dotted).read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(f"{dotted} not found")


def _span(node: ast.AST) -> int:
    """Return the inclusive line span for one parsed node."""
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    assert isinstance(start, int)
    assert isinstance(end, int)
    return end - start + 1


def _methods(node: ast.ClassDef) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return methods declared directly by a class."""
    return [item for item in node.body if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)]


def test_review_collaborator_budgets() -> None:
    """Keep extracted collaborators focused enough to retain one responsibility."""
    failures: list[str] = []
    for dotted, budget in _BUDGETS.items():
        node = _class_node(dotted)
        if _span(node) > budget.max_lines:
            failures.append(f"{dotted}: {_span(node)} lines > {budget.max_lines}")
        for method in _methods(node):
            if _span(method) > budget.max_method_lines:
                failures.append(
                    f"{dotted}.{method.name}: {_span(method)} lines > {budget.max_method_lines}"
                )
    assert failures == []


def test_review_phase_facades_remain_small() -> None:
    """Prevent the compatibility facade from regaining loop orchestration."""
    tree = ast.parse(Path("hephaestus/automation/_review_phase.py").read_text())
    phase = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ReviewPhase"
    )
    methods = {
        method.name: method
        for method in _methods(phase)
        if method.name in _REVIEW_PHASE_FACADE_BUDGETS
    }
    assert methods.keys() == _REVIEW_PHASE_FACADE_BUDGETS.keys()
    failures = [
        f"{name}: {_span(methods[name])} lines > {budget}"
        for name, budget in _REVIEW_PHASE_FACADE_BUDGETS.items()
        if _span(methods[name]) > budget
    ]
    assert failures == []


def test_pure_loop_has_no_direct_io_or_state_imports() -> None:
    """The loop coordinator receives all effects through its operations object."""
    tree = ast.parse(Path("hephaestus/automation/_review_loop.py").read_text())
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    imported_modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    offending = {
        candidate
        for candidate in _FORBIDDEN_LOOP_IMPORTS
        if any(
            module == candidate or module.startswith(f"{candidate}.") for module in imported_modules
        )
    }
    assert imports
    assert offending == set()
