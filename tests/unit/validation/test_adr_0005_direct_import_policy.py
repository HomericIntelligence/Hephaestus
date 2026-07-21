"""Guard ADR-0005's frozen legacy direct-import baseline."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_AUTOMATION_ROOT = _REPO_ROOT / "hephaestus" / "automation"
_ADR_PATH = _REPO_ROOT / "docs" / "adr" / "0005-multi-agent-runtime-abstraction.md"
_LEGACY_MODULES = frozenset({"claude_invoke", "claude_models", "claude_timeouts"})

_APPROVED_DIRECT_IMPORTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("_followup_phase.py", "claude_models"),
        ("_implement_phase.py", "claude_invoke"),
        ("_implement_phase.py", "claude_models"),
        ("_review_phase.py", "claude_invoke"),
        ("_review_phase.py", "claude_models"),
        ("address_review_core.py", "claude_invoke"),
        ("address_review_core.py", "claude_models"),
        ("advise_runner.py", "claude_invoke"),
        ("audit_reviewer.py", "claude_invoke"),
        ("audit_reviewer.py", "claude_models"),
        ("ci_fix_flow.py", "claude_invoke"),
        ("ci_fix_flow.py", "claude_models"),
        ("ci_fix_orchestrator.py", "claude_invoke"),
        ("ci_fix_orchestrator.py", "claude_models"),
        ("comment_difficulty.py", "claude_invoke"),
        ("comment_difficulty.py", "claude_models"),
        ("learn.py", "claude_models"),
        ("pipeline/stages/plan_review.py", "claude_invoke"),
        ("pipeline/stages/pr_review.py", "claude_invoke"),
        ("pipeline/worker_pool.py", "claude_invoke"),
        ("plan_reviewer.py", "claude_invoke"),
        ("plan_reviewer.py", "claude_models"),
        ("post_merge_processor.py", "claude_invoke"),
        ("post_merge_processor.py", "claude_models"),
        ("pr_manager.py", "claude_invoke"),
        ("pr_manager.py", "claude_models"),
        ("pr_review_core.py", "claude_invoke"),
        ("pr_review_core.py", "claude_models"),
        ("review_validator.py", "claude_invoke"),
        ("review_validator.py", "claude_models"),
    }
)

_DECISION_POLICY = """
New automation call sites must use `hephaestus.agents.runtime` for provider
selection and provider-neutral execution. Existing automation compatibility
seams may retain the frozen direct imports of `claude_invoke` for Claude
invocation, response parsing or validation, and formatting Claude-specific
failure diagnostics, and of `claude_models` for its `agent_config`
compatibility exports. No direct `claude_timeouts` consumer is approved.
These imports are migration debt: no new consumer/module pair may be added,
and migrated pairs must be removed from source and the regression baseline
together.
"""


def _legacy_targets(node: ast.Import | ast.ImportFrom) -> set[str]:
    """Return legacy automation modules imported by one AST node."""
    if isinstance(node, ast.Import):
        return {
            target
            for alias in node.names
            for target in _LEGACY_MODULES
            if alias.name in {target, f"hephaestus.automation.{target}"}
        }

    module = node.module or ""
    if module in _LEGACY_MODULES:
        return {module}
    if module.startswith("hephaestus.automation."):
        target = module.removeprefix("hephaestus.automation.").split(".", 1)[0]
        return {target} if target in _LEGACY_MODULES else set()
    if module == "hephaestus.automation" or (node.level and not module):
        return {alias.name for alias in node.names if alias.name in _LEGACY_MODULES}
    return set()


def _collect_direct_imports() -> frozenset[tuple[str, str]]:
    """Collect every direct legacy import beneath the automation product layer."""
    imports: set[tuple[str, str]] = set()
    for source in sorted(_AUTOMATION_ROOT.rglob("*.py")):
        relative = source.relative_to(_AUTOMATION_ROOT).as_posix()
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports.update((relative, target) for target in _legacy_targets(node))
    return frozenset(imports)


def _normalize(text: str) -> str:
    """Collapse Markdown wrapping so policy assertions remain readable."""
    return " ".join(text.split())


def _baseline_summary() -> str:
    """Return the source-derived baseline sentence required in ADR-0005."""
    counts = Counter(target for _, target in _APPROVED_DIRECT_IMPORTS)
    return (
        f"The approved legacy baseline is {len(_APPROVED_DIRECT_IMPORTS)} "
        "exact consumer/module pairs: "
        f"{counts['claude_invoke']} for `claude_invoke`, "
        f"{counts['claude_models']} for `claude_models`, and "
        f"{counts['claude_timeouts']} for `claude_timeouts`."
    )


def test_direct_imports_match_frozen_migration_baseline() -> None:
    """Source imports must exactly match ADR-0005's approved migration debt."""
    actual = _collect_direct_imports()
    assert actual == _APPROVED_DIRECT_IMPORTS, (
        "ADR-0005 direct-import baseline drifted: "
        f"added={sorted(actual - _APPROVED_DIRECT_IMPORTS)}, "
        f"removed={sorted(_APPROVED_DIRECT_IMPORTS - actual)}"
    )


def test_approved_importers_stay_in_automation_product_layer() -> None:
    """Legacy exceptions must never name library-layer consumers."""
    root = _AUTOMATION_ROOT.resolve()
    invalid = []
    for relative, target in sorted(_APPROVED_DIRECT_IMPORTS):
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            invalid.append((relative, target))
    assert not invalid, f"non-automation or missing approved importers: {invalid}"


def test_adr_documents_complete_direct_import_policy() -> None:
    """ADR-0005 must state the policy, including diagnostic-only consumers."""
    adr = _normalize(_ADR_PATH.read_text(encoding="utf-8"))
    assert _normalize(_DECISION_POLICY) in adr
    assert _normalize(_baseline_summary()) in adr
