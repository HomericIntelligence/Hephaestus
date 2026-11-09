"""Exact-pair import guard for the git-lifecycle modules moving to hephaestus.git.

Issue #3509 (parent epic #3504): the 12 mover modules must import zero
``hephaestus.automation.*`` symbols outside the mover set itself. The guard
walks every ``Import``/``ImportFrom`` node with ``ast.walk`` (function-local
imports included), resolves each target with ``importlib.util.resolve_name``
(relative and absolute forms both covered), and freezes the exact
``(consumer, module)`` pair set. Exact equality rejects a newly added pair and
a stale entry alike.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import hephaestus

LIB_ROOT = Path(hephaestus.__file__).parent
AUTOMATION_ROOT = LIB_ROOT / "automation"

#: The 12 git-lifecycle modules the epic moves to ``hephaestus/git/``. The
#: names alone decide that an import target is internal; a name stays listed
#: even after its own source file moves, so a later
#: ``hephaestus.automation.implementation_writer`` import still reads as
#: internal and is checked against ``INTERNAL_ALLOWED``.
MOVERS = frozenset(
    {
        "git_config_safety",
        "commit_paths",
        "commit_policy",
        "remote_git",
        "git_runtime",
        "commit_runtime",
        "git_utils",
        "worktree_snapshot",
        "repo_intake",
        "worktree_manager",
        "source_worktree",
        "implementation_writer",
    }
)

#: The frozen internal (consumer, module) pairs. The guard asserts exact
#: equality, so a step that removes a module must shrink this set in the same
#: commit as the consumer retargets.
INTERNAL_ALLOWED = frozenset(
    {
        ("commit_runtime", "commit_paths"),
        ("commit_runtime", "commit_policy"),
        ("commit_runtime", "git_runtime"),
        ("git_utils", "git_runtime"),
        ("git_utils", "commit_runtime"),
        ("worktree_snapshot", "git_utils"),
        ("repo_intake", "git_config_safety"),
        ("repo_intake", "git_runtime"),
        ("worktree_manager", "git_runtime"),
        ("worktree_manager", "implementation_writer"),
        ("worktree_manager", "git_utils"),
        ("worktree_manager", "repo_intake"),
        ("source_worktree", "commit_paths"),
        ("source_worktree", "git_runtime"),
        ("source_worktree", "implementation_writer"),
        ("source_worktree", "worktree_manager"),
        ("source_worktree", "worktree_snapshot"),
    }
)


def package_of(py: Path) -> str:
    """Return the dotted package name of the module at *py*."""
    return ".".join(py.parent.relative_to(LIB_ROOT.parent).parts)


def _automation_module(target: str) -> str | None:
    """Return the ``hephaestus.automation`` submodule *target* names, if any."""
    parts = target.split(".")
    if parts[:2] != ["hephaestus", "automation"] or len(parts) < 3:
        return None
    return parts[2]


def collect_edges() -> tuple[frozenset[tuple[str, str]], frozenset[tuple[str, str]]]:
    """Return the (external, internal) ``hephaestus.automation`` edge sets.

    Each edge is an exact ``(consumer, module)`` pair. External edges target a
    non-mover ``hephaestus.automation`` module; internal edges target a mover.
    The parse loop skips a mover whose source file is absent (only
    ``implementation_writer`` after its move); the skip covers the consumer
    file scan only — the name stays in ``MOVERS`` for target classification.
    """
    external: set[tuple[str, str]] = set()
    internal: set[tuple[str, str]] = set()
    for name in sorted(MOVERS):
        py = AUTOMATION_ROOT / f"{name}.py"
        if not py.exists():
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        pkg = package_of(py)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = "." * node.level + (node.module or "")
                resolved = importlib.util.resolve_name(base, pkg)
                targets = [
                    f"{resolved}.{alias.name}" for alias in node.names if alias.name != "*"
                ] or [resolved]
            else:
                continue
            for target in targets:
                module = _automation_module(target)
                if module is None:
                    continue
                edge = (name, module)
                if module in MOVERS:
                    internal.add(edge)
                else:
                    external.add(edge)
    return frozenset(external), frozenset(internal)


def test_movers_import_no_external_automation_symbols() -> None:
    """Verify the 12 movers import no non-mover ``hephaestus.automation`` module."""
    external, internal = collect_edges()
    assert external == frozenset(), (
        "added external hephaestus.automation edges: "
        f"{sorted(external - frozenset())}; "
        f"internal={sorted(internal)}"
    )


def test_internal_edge_set_equals_allowed_baseline() -> None:
    """Verify the internal mover-to-mover edge set equals the frozen baseline."""
    external, internal = collect_edges()
    assert internal == INTERNAL_ALLOWED, (
        f"added={sorted(internal - INTERNAL_ALLOWED)}, "
        f"removed={sorted(INTERNAL_ALLOWED - internal)}, "
        f"external={sorted(external)}"
    )
