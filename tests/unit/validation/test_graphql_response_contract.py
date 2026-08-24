"""Static and runtime guards for the automation GraphQL boundary."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import hephaestus.automation.github_api as github_api

AUTOMATION_ROOT = Path(__file__).resolve().parents[3] / "hephaestus" / "automation"


class _GraphQLBoundaryVisitor(ast.NodeVisitor):
    """Find raw client imports and free-form GraphQL process construction."""

    def __init__(self) -> None:
        self.function_stack: list[str] = []
        self.violations: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]  # noqa: N815

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        if node.module == "hephaestus.github.client" and any(
            alias.name in {"gh_call", "_gh_call"} for alias in node.names
        ):
            self.violations.append("raw gh_call import")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "_graphql"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            self.violations.append("free-form _graphql call")
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"run", "Popen", "check_call", "check_output", "run_subprocess"}
            and node.args
            and _contains_graphql_literal(node.args[0])
        ):
            self.violations.append("direct GraphQL process construction")
        if (
            isinstance(node.func, ast.Name)
            and node.func.id
            in {
                "GraphQLQuerySpec",
                "GraphQLMutationSpec",
            }
        ) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"GraphQLQuerySpec", "GraphQLMutationSpec"}
        ):
            self.violations.append("direct typed spec construction")
        self.generic_visit(node)


def _contains_graphql_literal(node: ast.AST) -> bool:
    """Return whether an AST value contains a GraphQL endpoint token."""
    return any(
        isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and child.value.lstrip("/").casefold() == "graphql"
        for child in ast.walk(node)
    )


def raw_graphql_executors(root: Path) -> dict[Path, set[str]]:
    """Return the files/functions that can construct raw GraphQL calls."""
    result: dict[Path, set[str]] = {}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _GraphQLBoundaryVisitor()
        visitor.visit(tree)
        relative = path.relative_to(root)
        if relative == Path("github_api/graphql.py"):
            result[relative] = {"run_graphql"}
        elif visitor.violations:
            result[relative] = set(visitor.violations)
    return result


def test_automation_graphql_has_one_execution_boundary() -> None:
    """Only the central executor module may own raw GraphQL execution."""
    assert raw_graphql_executors(AUTOMATION_ROOT) == {
        Path("github_api/graphql.py"): {"run_graphql"}
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["api", "graphql", "-f", "query={viewer{login}}"],
        ["api", "--method", "POST", "/graphql", "-f", "query={viewer{login}}"],
    ],
)
def test_package_facade_rejects_graphql_bypass(argv: list[str]) -> None:
    """The compatibility façade cannot bypass ``run_graphql``."""
    with pytest.raises(RuntimeError, match="must use run_graphql"):
        github_api.gh_call(argv)
