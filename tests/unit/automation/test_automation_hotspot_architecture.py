"""Regression budgets for the decomposed automation hotspots."""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).parents[3]

# These are source budgets, not measurements of the current checkout.  Every
# cap is below the pre-decomposition hotspot it replaces, so future changes
# cannot silently rebuild the old monolith behind a new façade.
_PREDECOMPOSITION_LINES = {
    "hephaestus/automation/pipeline/coordinator.py": 3_325,
    "hephaestus/automation/pipeline_github.py": 2_790,
    "hephaestus/automation/pipeline/stages/pr_review.py": 2_861,
}

_FILE_BUDGETS = {
    "hephaestus/automation/pipeline/coordinator.py": 1_100,
    "hephaestus/automation/pipeline/coordinator_contract.py": 225,
    "hephaestus/automation/pipeline/coordinator_types.py": 500,
    "hephaestus/automation/pipeline/coordinator_issue_classification.py": 125,
    "hephaestus/automation/pipeline/coordinator_runtime.py": 1_400,
    "hephaestus/automation/pipeline/coordinator_sources.py": 950,
    "hephaestus/automation/pipeline/coordinator_dispatch.py": 500,
    "hephaestus/automation/pipeline_github.py": 500,
    "hephaestus/automation/pipeline_github_contract.py": 125,
    "hephaestus/automation/pipeline_github_transport.py": 425,
    "hephaestus/automation/pipeline_github_authorization.py": 300,
    "hephaestus/automation/pipeline_github_queries.py": 650,
    # Effective classic and ruleset policy reads form one stable snapshot.
    "hephaestus/automation/pipeline_github_check_policy.py": 425,
    # Parent-ruleset selectors and branch matching are one bounded concern.
    "hephaestus/automation/pipeline_github_ruleset_conditions.py": 200,
    # Exact-head Check Runs use a complete paginated double-read. Keep this
    # separate repository-scoped merge-gate collaborator bounded.
    "hephaestus/automation/pipeline_github_required_checks.py": 425,
    "hephaestus/automation/pipeline_github_reviews.py": 1_475,
    "hephaestus/automation/pipeline_github_mutations.py": 475,
    "hephaestus/automation/pipeline/stages/pr_review.py": 550,
    "hephaestus/automation/pipeline/stages/pr_review_threads.py": 850,
    "hephaestus/automation/pipeline/stages/pr_review_diagnostics.py": 150,
    "hephaestus/automation/pipeline/stages/pr_review_repository.py": 100,
    "hephaestus/automation/pipeline/stages/pr_review_receipts.py": 100,
    "hephaestus/automation/pipeline/stages/pr_review_verification.py": 250,
    # The GraphQL contract helpers added by #2393 bring this collaborator to
    # 1,403 lines; keep the explicit cap just above the measured source size.
    "hephaestus/automation/pipeline/stages/pr_review_jobs.py": 1_403,
    "hephaestus/automation/pipeline/stages/pr_review_gate.py": 700,
}

_COLLABORATOR_MODULES = frozenset(
    {
        "coordinator_runtime",
        "coordinator_contract",
        "coordinator_issue_classification",
        "coordinator_sources",
        "coordinator_dispatch",
        "pipeline_github_transport",
        "pipeline_github_authorization",
        "pipeline_github_contract",
        "pipeline_github_check_policy",
        "pipeline_github_ruleset_conditions",
        "pipeline_github_queries",
        "pipeline_github_required_checks",
        "pipeline_github_reviews",
        "pipeline_github_mutations",
        "pr_review_threads",
        "pr_review_diagnostics",
        "pr_review_repository",
        "pr_review_receipts",
        "pr_review_verification",
        "pr_review_jobs",
        "pr_review_gate",
    }
)

_SHARED_NAMESPACE_MODULES = (
    "hephaestus/automation/pipeline/stages/pr_review_threads.py",
    "hephaestus/automation/pipeline/stages/pr_review_verification.py",
    "hephaestus/automation/pipeline_github_transport.py",
)

_COORDINATOR_COLLABORATORS = (
    "hephaestus/automation/pipeline/coordinator.py",
    "hephaestus/automation/pipeline/coordinator_runtime.py",
    "hephaestus/automation/pipeline/coordinator_execution.py",
    "hephaestus/automation/pipeline/coordinator_dispatch.py",
    "hephaestus/automation/pipeline/coordinator_issue_classification.py",
    "hephaestus/automation/pipeline/coordinator_sources.py",
    "hephaestus/automation/pipeline/coordinator_learning.py",
)

_COORDINATOR_NAMESPACE_COLLABORATORS = tuple(
    relative for relative in _COORDINATOR_COLLABORATORS if not relative.endswith("/coordinator.py")
)

_CONTRACT_MODULES = (
    "hephaestus/automation/pipeline/coordinator_contract.py",
    "hephaestus/automation/pipeline_github_contract.py",
)


def test_hotspot_file_budgets_are_non_increasing() -> None:
    """Keep each façade and collaborator below its architecture budget."""
    failures: list[str] = []
    for relative, budget in _FILE_BUDGETS.items():
        path = _ROOT / relative
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > budget:
            failures.append(f"{relative}: {lines} lines > {budget}")
    for relative, predecomposition in _PREDECOMPOSITION_LINES.items():
        assert _FILE_BUDGETS[relative] < predecomposition
    assert failures == []


def test_collaborators_do_not_import_their_facades() -> None:
    """The split modules remain one-way dependencies behind stable façades."""
    violations: list[str] = []
    for path in (_ROOT / "hephaestus/automation").rglob("*.py"):
        if path.stem not in _COLLABORATOR_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.module in {
                "hephaestus.automation.pipeline.coordinator",
                "hephaestus.automation.pipeline_github",
                "hephaestus.automation.pipeline.stages.pr_review",
            }:
                violations.append(f"{path}: imports {node.module}")
    assert violations == []


def test_shared_namespaces_declare_static_exports() -> None:
    """Keep star-imported collaborator namespaces visible to static tooling."""
    violations: list[str] = []
    for relative in _SHARED_NAMESPACE_MODULES:
        path = _ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        exports = [
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
            )
        ]
        if len(exports) != 1 or not isinstance(exports[0], (ast.List, ast.Tuple)):
            violations.append(relative)
            continue
        if not all(
            isinstance(element, ast.Constant) and isinstance(element.value, str)
            for element in exports[0].elts
        ):
            violations.append(relative)
    assert violations == []


def test_coordinator_namespace_composition_is_explicit() -> None:
    """Keep coordinator collaborators on explicit imports and direct seams."""
    violations: list[str] = []
    for relative in _COORDINATOR_COLLABORATORS:
        path = _ROOT / relative
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        if "# ruff: noqa: F403, F405" in text:
            violations.append(f"{relative}:ruff-waiver")
        if 'sys.modules["hephaestus.automation.pipeline.coordinator"]' in text:
            violations.append(f"{relative}:sys.modules-facade")
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "coordinator_types"
                and any(alias.name == "*" for alias in node.names)
            ):
                violations.append(f"{relative}:{node.lineno}:star-import")
            if isinstance(node, ast.ClassDef) and node.name == "_CompatModule":
                violations.append(f"{relative}:{node.lineno}:compat-module")
            if isinstance(node, ast.FunctionDef) and node.name == "_compat":
                violations.append(f"{relative}:{node.lineno}:compat-helper")
    assert violations == []


def test_coordinator_collaborators_do_not_recreate_bare_type_aliases() -> None:
    """Require collaborators to keep coordinator-type uses visibly qualified."""
    violations: list[str] = []
    for relative in _COORDINATOR_NAMESPACE_COLLABORATORS:
        path = _ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Attribute):
                continue
            if not isinstance(node.value.value, ast.Name) or node.value.value.id != "ct":
                continue
            if any(isinstance(target, ast.Name) for target in node.targets):
                violations.append(f"{relative}:{node.lineno}:bare-type-alias")
    assert violations == []


def test_coordinator_types_has_no_shared_namespace_all() -> None:
    """Keep coordinator_types free of module-level shared export tables."""
    path = _ROOT / "hephaestus/automation/pipeline/coordinator_types.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            violations.append(f"{path}:assign")
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
        ):
            violations.append(f"{path}:annassign")
    assert violations == []


def test_contract_methods_do_not_use_no_effect_ellipsis_statements() -> None:
    """Keep executable contract modules free of analyzer-visible no-op expressions."""
    violations: list[str] = []
    for relative in _CONTRACT_MODULES:
        path = _ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and node.value.value is Ellipsis
            ):
                violations.append(f"{relative}:{node.lineno}")
    assert violations == []
