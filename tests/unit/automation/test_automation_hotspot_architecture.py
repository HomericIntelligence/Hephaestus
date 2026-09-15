"""Regression budgets for the decomposed automation hotspots."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

from hephaestus.automation import pipeline_github_contract
from hephaestus.automation.pipeline.merge_wait_admission import RequiredChecksDeferred

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
    # Explicit type-only method signatures replace the unrestricted fallback.
    "hephaestus/automation/pipeline/coordinator_contract.py": 450,
    "hephaestus/automation/pipeline/coordinator_types.py": 500,
    "hephaestus/automation/pipeline/coordinator_issue_classification.py": 125,
    "hephaestus/automation/pipeline/coordinator_runtime.py": 1_400,
    "hephaestus/automation/pipeline/coordinator_sources.py": 950,
    "hephaestus/automation/pipeline/coordinator_dispatch.py": 500,
    "hephaestus/automation/pipeline_github.py": 500,
    "hephaestus/automation/pipeline_github_contract.py": 128,
    "hephaestus/automation/pipeline_github_transport.py": 425,
    "hephaestus/automation/pipeline_github_review_queries.py": 150,
    "hephaestus/automation/pipeline_github_queries.py": 650,
    "hephaestus/automation/pipeline_github_repository.py": 100,
    # Effective classic and ruleset policy reads form one stable snapshot. The
    # bound includes the authenticated absent-classic-protection parser.
    "hephaestus/automation/pipeline_github_check_policy.py": 450,
    # Complete policy values remain separate from mutable policy reads.
    "hephaestus/automation/pipeline_github_merge_policy.py": 75,
    "hephaestus/automation/pipeline_github_merge_rules.py": 125,
    # Parent-ruleset selectors and branch matching are one bounded concern.
    "hephaestus/automation/pipeline_github_ruleset_conditions.py": 200,
    # GitHub branch patterns use Ruby's path-separator-aware wildcard rules.
    "hephaestus/automation/pipeline_github_ref_patterns.py": 125,
    # Commit statuses use an independent paginated stability boundary.
    "hephaestus/automation/pipeline_github_commit_statuses.py": 225,
    # Check Suite and Check Run inventories share one bounded pagination owner.
    "hephaestus/automation/pipeline_github_check_run_inventory.py": 225,
    # Check Run lifecycle validation stays separate from evidence evaluation.
    "hephaestus/automation/pipeline_github_check_run_validation.py": 150,
    # Exact-head Check Runs use a complete paginated double-read. Keep this
    # separate repository-scoped merge-gate collaborator bounded.
    "hephaestus/automation/pipeline_github_required_checks.py": 425,
    "hephaestus/automation/pipeline/merge_wait_admission.py": 125,
    "hephaestus/automation/pipeline_github_reviews.py": 1_475,
    "hephaestus/automation/pipeline_github_mutations.py": 475,
    "hephaestus/automation/pipeline/stages/pr_review.py": 550,
    "hephaestus/automation/pipeline/stages/pr_review_threads.py": 850,
    "hephaestus/automation/pipeline/stages/pr_review_diagnostics.py": 150,
    "hephaestus/automation/pipeline/stages/pr_review_repository.py": 100,
    # Receipt storage moved here when the bootstrap stage helper was removed.
    "hephaestus/automation/pipeline/stages/pr_review_receipts.py": 150,
    "hephaestus/automation/pipeline/stages/pr_review_verification.py": 250,
    "hephaestus/automation/pipeline/stages/pr_review_verification_specs.py": 150,
    "hephaestus/automation/pipeline/stages/pr_review_verification_paths.py": 150,
    "hephaestus/automation/pipeline/stages/pr_review_verification_publication_specs.py": 150,
    # The GraphQL contract helpers added by #2393 bring this collaborator to
    # 1,403 lines; keep the explicit cap just above the measured source size.
    "hephaestus/automation/pipeline/stages/pr_review_jobs.py": 1_403,
    "hephaestus/automation/pipeline/stages/pr_review_findings.py": 250,
    "hephaestus/automation/review_anchors.py": 500,
    "hephaestus/automation/review_finding_history.py": 350,
    "hephaestus/automation/pipeline/stages/pr_review_history.py": 225,
    "hephaestus/automation/pipeline/stages/pr_review_recovery.py": 350,
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
        "pipeline_github_review_queries",
        "pipeline_github_contract",
        "pipeline_github_check_policy",
        "pipeline_github_merge_policy",
        "pipeline_github_merge_rules",
        "pipeline_github_commit_statuses",
        "pipeline_github_check_run_inventory",
        "pipeline_github_check_run_validation",
        "pipeline_github_ref_patterns",
        "pipeline_github_ruleset_conditions",
        "pipeline_github_queries",
        "pipeline_github_repository",
        "pipeline_github_required_checks",
        "merge_wait_admission",
        "pipeline_github_reviews",
        "pipeline_github_mutations",
        "pr_review_threads",
        "pr_review_diagnostics",
        "pr_review_repository",
        "pr_review_receipts",
        "pr_review_verification",
        "pr_review_verification_specs",
        "pr_review_verification_paths",
        "pr_review_verification_publication_specs",
        "pr_review_jobs",
        "pr_review_findings",
        "review_anchors",
        "review_finding_history",
        "pr_review_history",
        "pr_review_recovery",
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

_FACADE_MODULES = frozenset(
    {
        "hephaestus.automation.pipeline.coordinator",
        "hephaestus.automation.pipeline.stages.pr_review",
        "hephaestus.automation.pipeline_github",
    }
)


def _absolute_import_modules(module_name: str, node: ast.ImportFrom) -> frozenset[str]:
    """Return possible module names for one absolute or relative import."""
    if node.level == 0:
        prefix = node.module.split(".") if node.module else []
    else:
        package = module_name.split(".")[:-1]
        parent_count = node.level - 1
        if parent_count > len(package):
            return frozenset()
        prefix = package[: len(package) - parent_count]
        if node.module:
            prefix.extend(node.module.split("."))
    if not prefix:
        return frozenset()
    imported = {".".join(prefix)} if node.module else set()
    imported.update(".".join((*prefix, alias.name)) for alias in node.names)
    return frozenset(imported)


def _facade_import_violations(module_name: str, source: str, source_name: str) -> tuple[str, ...]:
    """Return prohibited façade imports from one collaborator source."""
    violations: list[str] = []
    for node in ast.walk(ast.parse(source, filename=source_name)):
        if isinstance(node, ast.Import):
            imported = frozenset(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported = _absolute_import_modules(module_name, node)
        else:
            continue
        prohibited = imported & _FACADE_MODULES
        violations.extend(f"{source_name}: imports {facade}" for facade in sorted(prohibited))
    return tuple(violations)


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


def test_required_checks_deferred_keeps_its_contract_identity() -> None:
    """The stable contract import exposes the canonical admission value."""
    assert pipeline_github_contract.RequiredChecksDeferred is RequiredChecksDeferred


def test_collaborators_do_not_import_their_facades() -> None:
    """The split modules remain one-way dependencies behind stable façades."""
    violations: list[str] = []
    for path in (_ROOT / "hephaestus/automation").rglob("*.py"):
        if path.stem not in _COLLABORATOR_MODULES:
            continue
        module_name = ".".join(path.relative_to(_ROOT).with_suffix("").parts)
        violations.extend(
            _facade_import_violations(
                module_name,
                path.read_text(encoding="utf-8"),
                str(path),
            )
        )
    assert violations == []


def test_relative_facade_imports_resolve_to_absolute_modules() -> None:
    """Dependency checks recognize normal relative façade imports."""
    cases = (
        (
            "hephaestus.automation.pipeline_github_check_run_validation",
            "from .pipeline_github import PipelineGitHub",
            "hephaestus.automation.pipeline_github",
        ),
        (
            "hephaestus.automation.pipeline.merge_wait_admission",
            "from ..pipeline_github import PipelineGitHub",
            "hephaestus.automation.pipeline_github",
        ),
        (
            "hephaestus.automation.pipeline.coordinator_runtime",
            "from .coordinator import PipelineCoordinator",
            "hephaestus.automation.pipeline.coordinator",
        ),
        (
            "hephaestus.automation.pipeline.stages.pr_review_gate",
            "from .pr_review import PrReviewStage",
            "hephaestus.automation.pipeline.stages.pr_review",
        ),
        (
            "hephaestus.automation.pipeline_github_check_run_validation",
            "from . import pipeline_github",
            "hephaestus.automation.pipeline_github",
        ),
    )
    for module_name, source, expected in cases:
        node = ast.parse(source).body[0]
        assert isinstance(node, ast.ImportFrom)
        assert expected in _absolute_import_modules(module_name, node)
        assert _facade_import_violations(module_name, source, "candidate.py")


def test_absolute_facade_import_uses_the_violation_path() -> None:
    """Dependency checks recognize a direct absolute façade import."""
    violations = _facade_import_violations(
        "hephaestus.automation.pipeline_github_check_run_validation",
        "import hephaestus.automation.pipeline_github as github_facade",
        "candidate.py",
    )

    assert violations == ("candidate.py: imports hephaestus.automation.pipeline_github",)


def test_parent_imports_resolve_to_facade_modules() -> None:
    """Dependency checks recognize façades imported from parent packages."""
    cases = (
        (
            "hephaestus.automation.pipeline_github_check_run_validation",
            "from hephaestus.automation import pipeline_github",
            "hephaestus.automation.pipeline_github",
        ),
        (
            "hephaestus.automation.pipeline_github_check_run_validation",
            "from hephaestus.automation.pipeline import coordinator",
            "hephaestus.automation.pipeline.coordinator",
        ),
        (
            "hephaestus.automation.pipeline_github_check_run_validation",
            "from hephaestus.automation.pipeline.stages import pr_review",
            "hephaestus.automation.pipeline.stages.pr_review",
        ),
        (
            "hephaestus.automation.pipeline_github_check_run_validation",
            "from .pipeline import coordinator",
            "hephaestus.automation.pipeline.coordinator",
        ),
        (
            "hephaestus.automation.pipeline.coordinator_runtime",
            "from .stages import pr_review",
            "hephaestus.automation.pipeline.stages.pr_review",
        ),
    )
    for module_name, source, expected in cases:
        violations = _facade_import_violations(
            module_name,
            source,
            "candidate.py",
        )

        assert violations == (f"candidate.py: imports {expected}",)


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


def test_merge_policy_value_dependencies_remain_pure() -> None:
    """Keep policy values independent of readers, transport, and execution."""
    path = _ROOT / "hephaestus/automation/pipeline_github_merge_policy.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    allowed = {(0, "__future__"), (0, "dataclasses"), (1, "pipeline_github_merge_rules")}
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and (node.level, node.module) not in allowed:
            violations.append(f"{node.level}:{node.module}")
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


def _runtime_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Inspect executable syntax and omit declarations under TYPE_CHECKING."""
    yield node
    if (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
    ):
        for branch in node.orelse:
            yield from _runtime_nodes(branch)
        return
    for child in ast.iter_child_nodes(node):
        yield from _runtime_nodes(child)


def test_contract_methods_do_not_use_no_effect_ellipsis_statements() -> None:
    """Keep executable contract modules free of analyzer-visible no-op expressions."""
    violations: list[str] = []
    for relative in _CONTRACT_MODULES:
        path = _ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _runtime_nodes(tree):
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and node.value.value is Ellipsis
            ):
                violations.append(f"{relative}:{node.lineno}")
    assert violations == []
