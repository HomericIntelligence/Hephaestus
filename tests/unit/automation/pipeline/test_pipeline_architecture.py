"""Architecture invariant: worker code performs zero GitHub API mutations.

Models tests/unit/automation/test_ci_driver_architecture.py. Enforces via AST
walk that pipeline/* modules never import or call github_api mutator functions,
with an explicit allowlist for documented interim offenders awaiting refactor.
"""

from __future__ import annotations

import ast
from pathlib import Path

import hephaestus
import hephaestus.automation.github_api as github_api

_PIPELINE = Path(hephaestus.__file__).parent / "automation" / "pipeline"

# Public mutators from github_api.__all__ (avoids hardcoding drift)
_PUBLIC_MUTATORS = frozenset(
    n
    for n in github_api.__all__
    if n.startswith(
        (
            "gh_issue_add",
            "gh_issue_remove",
            "gh_issue_comment",
            "gh_issue_create",
            "gh_issue_delete",
            "gh_issue_upsert",
            "gh_create_label",
            "gh_pr_create",
            "gh_pr_review_post",
            "gh_pr_update_review",
            "gh_pr_resolve",
        )
    )
)

# Private review-comment mutators that carry no public alias
_PRIVATE_MUTATORS = frozenset({"_post_shadow_review_comment", "_edit_or_keep_comments"})

_MUTATORS = _PUBLIC_MUTATORS | _PRIVATE_MUTATORS

# Modules awaiting refactor (e.g. _review_phase fuses agent calls with gh mutations).
# Shrinking allowlist strategy: document intent, enforce from day one, grow list
# only when fuse-pattern is unavoidable. Empty on day one per issue #1812.
_ALLOWLIST: frozenset[str] = frozenset()


def _imported_mutators(path: Path) -> set[str]:
    """Find github_api mutators imported or called in a Python file (AST walk)."""
    tree = ast.parse(path.read_text())
    found: set[str] = set()

    for node in ast.walk(tree):
        # Import statements: from github_api import X
        if isinstance(node, ast.ImportFrom) and node.module and "github_api" in node.module:
            found |= {a.name for a in node.names if a.name in _MUTATORS}
        # Attribute access: obj.X where X is a mutator
        if isinstance(node, ast.Attribute) and node.attr in _MUTATORS:
            found.add(node.attr)

    return found


def test_no_worker_module_imports_github_mutators() -> None:
    """Ensure pipeline/* modules do not import/call github_api mutators.

    Workers execute on thread pool with no coordinator access; all GitHub
    state changes must go through the coordinator to maintain consistency.
    """
    violations = []
    for py in _PIPELINE.glob("*.py"):
        if py.stem == "__init__":
            continue
        offenders = _imported_mutators(py) - _ALLOWLIST
        if offenders:
            violations.append(f"{py.name}: {sorted(offenders)}")

    assert not violations, "worker code must not call github_api mutators:\n" + "\n".join(
        violations
    )


def test_mutator_set_is_non_empty() -> None:
    """Guard against github_api.__all__ rename silently emptying _MUTATORS.

    If _MUTATORS becomes empty due to a rename or refactor, the invariant
    passes vacuously. This test ensures the mutator set always has content.
    """
    assert len(_MUTATORS) >= 5, f"_MUTATORS is suspiciously small: {_MUTATORS}"


def test_public_mutators_from_all() -> None:
    """Verify _PUBLIC_MUTATORS is derived from github_api.__all__."""
    # At least the major mutators should be present
    expected = {
        "gh_issue_add_labels",
        "gh_issue_remove_labels",
        "gh_issue_comment",
        "gh_pr_create",
        "gh_create_label",
    }
    assert expected <= _PUBLIC_MUTATORS, f"expected mutators missing: {expected - _PUBLIC_MUTATORS}"
