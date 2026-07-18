"""Architecture regression tests for the drive-green CI driver."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ClassBudget:
    """Maximum size allowed for a class and each of its methods."""

    max_lines: int
    max_methods: int
    max_method_lines: int


_BUDGETS = {
    # Slimmed to an importable placeholder (#1822): queue-owned merge waiting
    # now lives in pipeline/stages/merge_wait.py, driven by run_pipeline. The
    # class body is docstring-only, with no methods.
    "hephaestus.automation.ci_driver.CIDriver": ClassBudget(20, 0, 0),
    "hephaestus.automation.ci_run_coordinator.CIDriveRunCoordinator": ClassBudget(360, 8, 80),
    "hephaestus.automation.ci_fix_flow.CIFixFlow": ClassBudget(260, 5, 80),
    "hephaestus.automation.auto_merge_coordinator.AutoMergeCoordinator": ClassBudget(320, 8, 80),
    "hephaestus.automation.drive_green_state.DriveGreenArmingCoordinator": ClassBudget(320, 7, 80),
    "hephaestus.automation.drive_green_state.LastCIFixStore": ClassBudget(140, 4, 60),
    "hephaestus.automation.review_thread_resolver.ReviewThreadResolver": ClassBudget(280, 7, 80),
    "hephaestus.automation.pr_discovery.PRDiscovery": ClassBudget(560, 14, 80),
    "hephaestus.automation.ci_fix_orchestrator.CIFixOrchestrator": ClassBudget(1050, 17, 80),
}


def _module_path(dotted: str) -> Path:
    module_name = dotted.rsplit(".", 1)[0]
    return Path(*module_name.split(".")).with_suffix(".py")


def _class_node(dotted: str) -> ast.ClassDef:
    class_name = dotted.rsplit(".", 1)[1]
    tree = ast.parse(_module_path(dotted).read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(f"{dotted} not found")


def _methods(node: ast.ClassDef) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [item for item in node.body if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)]


def _span(node: ast.AST) -> int:
    lineno = getattr(node, "lineno", None)
    end_lineno = getattr(node, "end_lineno", None)
    assert isinstance(lineno, int)
    assert isinstance(end_lineno, int)
    return end_lineno - lineno + 1


def test_ci_driver_architecture_budgets() -> None:
    """Keep the drive-green façade and collaborators below SRP guardrails."""
    failures: list[str] = []
    for dotted, budget in _BUDGETS.items():
        node = _class_node(dotted)
        methods = _methods(node)
        if _span(node) > budget.max_lines:
            failures.append(f"{dotted}: {_span(node)} class lines > {budget.max_lines}")
        if len(methods) > budget.max_methods:
            failures.append(f"{dotted}: {len(methods)} methods > {budget.max_methods}")
        for method in methods:
            if _span(method) > budget.max_method_lines:
                failures.append(
                    f"{dotted}.{method.name}: {_span(method)} lines > {budget.max_method_lines}"
                )
    assert failures == []


def test_cidriver_has_no_private_delegate_stubs() -> None:
    """The façade should not grow one private method per collaborator call."""
    methods = _methods(_class_node("hephaestus.automation.ci_driver.CIDriver"))
    private_methods = [
        method.name
        for method in methods
        if method.name.startswith("_") and not method.name.startswith("__")
    ]
    assert private_methods == []
