"""Regression test for #714: no deferred back-pointer to ``implementer``.

The per-issue runner and CLI siblings (``implementer_phase_runner``,
``implementer_cli``) previously reached patchable symbols through a deferred
``from . import implementer`` (the ``_impl_module`` property), creating a
circular import. This module guards against that edge being reintroduced.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[3] / "hephaestus" / "automation"

_GUARDED_MODULES = ("implementer_cli", "implementer_phase_runner")


def _is_backpointer_import(node: ast.AST) -> bool:
    """Return True for a runtime import that points back at ``implementer``.

    Matches ``from . import implementer`` / ``from .implementer import …`` /
    ``from hephaestus.automation.implementer import …``.
    """
    if isinstance(node, ast.ImportFrom):
        if node.module is None and node.level == 1:
            return any(a.name == "implementer" for a in node.names)
        if node.module == "implementer" and node.level == 1:
            return True
        if node.module == "hephaestus.automation.implementer":
            return True
    return False


def _walk_runtime(tree: ast.Module) -> Iterator[ast.AST]:
    """Yield every node except those inside ``if TYPE_CHECKING:`` blocks."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            for sub in ast.walk(node):
                skip.add(id(sub))
    for node in ast.walk(tree):
        if id(node) not in skip:
            yield node


@pytest.mark.parametrize("module_name", _GUARDED_MODULES)
def test_no_runtime_backpointer_to_implementer(module_name: str) -> None:
    """No deferred back-pointer import inside any function body (#714)."""
    src = (_PKG / f"{module_name}.py").read_text()
    tree = ast.parse(src)
    for node in _walk_runtime(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                assert not _is_backpointer_import(sub), (
                    f"{module_name}.{node.name}: deferred runtime import "
                    f"{ast.unparse(sub)!r} re-introduces the #714 cycle"
                )


@pytest.mark.parametrize("module_name", _GUARDED_MODULES)
def test_no_module_level_backpointer_to_implementer(module_name: str) -> None:
    """No back-pointer import at module top-level (#714)."""
    src = (_PKG / f"{module_name}.py").read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            continue
        assert not _is_backpointer_import(node), (
            f"{module_name}: top-level back-pointer {ast.unparse(node)!r} "
            f"would re-create the #714 cycle"
        )


def test_impl_module_property_removed() -> None:
    """The ``_impl_module`` property — the runtime back-edge — must be gone."""
    src = (_PKG / "implementer_phase_runner.py").read_text()
    assert "_impl_module" not in src, (
        "implementer_phase_runner.py still defines/references _impl_module; "
        "the property must be removed and all call sites rewritten (#714)"
    )
    assert "_impl_mod" not in src, (
        "implementer_phase_runner.py still uses the _impl_mod local alias; "
        "every call site must use direct top-level imports (#714)"
    )


def test_phase_runner_imports_patchable_symbols_directly() -> None:
    """The runner must own the patchable symbols at module level (#714).

    With the ``_impl_module`` indirection gone, tests patch these at
    ``implementer_phase_runner.<X>``; if the runner stopped importing them the
    patch would silently no-op.
    """
    import hephaestus.automation.implementer_phase_runner as pr

    for symbol in (
        "find_pr_for_issue",
        "get_pr_head_branch",
        "is_plan_review_go",
        "fetch_issue_info",
        "invoke_claude_with_session",
        "get_repo_slug",
        "AGENT_IMPLEMENTER",
        "AGENT_ADVISE",
        "current_trunk_githash",
        "review_state",
    ):
        assert hasattr(pr, symbol), (
            f"implementer_phase_runner must bind {symbol!r} at module level "
            "so tests can patch it after the #714 cycle break"
        )


def test_main_defined_in_implementer_module() -> None:
    """``main`` lives in ``implementer`` (not re-imported from the CLI) (#714)."""
    import hephaestus.automation.implementer as impl

    assert impl.main.__module__ == "hephaestus.automation.implementer", (
        "main() must be defined in implementer.py so the console-script entry "
        "point resolves without a deferred import (#714)"
    )
