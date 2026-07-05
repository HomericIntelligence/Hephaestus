"""Architecture invariant: worker code performs zero GitHub API mutations.

Models tests/unit/automation/test_ci_driver_architecture.py. Enforces via AST
walk that pipeline/* modules never import or call github_api mutator functions,
with an explicit allowlist for documented interim offenders awaiting refactor.
Worker-side modules (worker_pool.py, jobs.py) are held to a stricter bar: they
may not import ``hephaestus.automation.github_api`` or
``hephaestus.automation.pr_manager`` AT ALL, in any form — this also catches
non-obvious mutators such as ``skip_epics``, ``gh_call``, and ``run``.

Caveat: this is a static, import/attribute-level guard. Subprocess-level ``gh``
calls (e.g. building a ``["gh", ...]`` argv and running it directly) are OUT OF
SCOPE here and must be caught in code review.
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

# Worker-side modules: code that executes ON worker threads. These may not
# import github_api or pr_manager at all (not even non-mutator helpers) so
# that gh_call/run/skip_epics-style mutators can never sneak in.
_WORKER_SIDE_MODULES = ("worker_pool.py", "jobs.py")
_FORBIDDEN_WORKER_MODULES = ("github_api", "pr_manager")


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


def _forbidden_module_imports(path: Path) -> set[str]:
    """Find any-form imports of forbidden modules (github_api, pr_manager)."""

    def _is_forbidden(dotted: str) -> bool:
        return any(part in _FORBIDDEN_WORKER_MODULES for part in dotted.split("."))

    tree = ast.parse(path.read_text())
    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits |= {a.name for a in node.names if _is_forbidden(a.name)}
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _is_forbidden(module):
                hits.add(module)
            else:
                # e.g. `from hephaestus.automation import github_api`
                hits |= {f"{module}.{a.name}" for a in node.names if _is_forbidden(a.name)}
    return hits


def test_no_worker_module_imports_github_mutators() -> None:
    """Ensure pipeline modules (recursively) do not import/call github_api mutators.

    Workers execute on thread pool with no coordinator access; all GitHub
    state changes must go through the coordinator to maintain consistency.
    Subprocess-level ``gh`` invocations are out of scope for this AST guard.
    """
    violations = []
    for py in sorted(_PIPELINE.rglob("*.py")):
        if py.stem == "__init__":
            continue
        offenders = _imported_mutators(py) - _ALLOWLIST
        if offenders:
            violations.append(f"{py.name}: {sorted(offenders)}")

    assert not violations, "worker code must not call github_api mutators:\n" + "\n".join(
        violations
    )


def test_worker_side_modules_never_import_github_api_or_pr_manager() -> None:
    """Worker-side modules must not import github_api or pr_manager AT ALL.

    The mutator-name scan above cannot enumerate every mutating entry point
    (``skip_epics``, ``gh_call``, ``run``, ...), so for the modules that run on
    worker threads we forbid the whole modules in any import form.
    """
    violations = []
    for name in _WORKER_SIDE_MODULES:
        path = _PIPELINE / name
        assert path.exists(), f"expected worker-side module missing: {path}"
        offenders = _forbidden_module_imports(path)
        if offenders:
            violations.append(f"{name}: {sorted(offenders)}")

    assert not violations, (
        "worker-side modules must not import github_api/pr_manager:\n" + "\n".join(violations)
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
