"""Exact-pair guard for the product-layer edges of the git module set.

Issue #3509 (epic #3504): the git-lifecycle modules that will move to
``hephaestus/git/`` must hold a frozen, exact set of imports. The guard
classifies every import of each mover with an AST walk (function-local
imports included) and asserts two exact-equality rules:

- No mover imports a non-mover ``hephaestus.automation.*`` module
  (``external == frozenset()``).
- The internal mover-to-mover edges equal ``INTERNAL_ALLOWED`` exactly.
  An added pair and a stale pair both fail, so a retired edge cannot be
  silently reintroduced and a new edge cannot replace a removed one.

``collect_edges()`` is exported so the implementation order can measure
the counts with the same code the guard asserts on.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import hephaestus

# The 12 git-lifecycle modules of epic #3504. Names whose source file has
# already moved to the library layer stay in the set: the name alone decides
# that an import target is internal, so a later
# ``hephaestus.automation.<moved-name>`` import cannot read as external.
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

# The exact mover-to-mover import pairs allowed on the final tree. Each pair
# is (consumer module name, imported module name). The pairs that import
# ``implementation_writer`` retire with the module's move to
# ``hephaestus.git.writer`` (#3509); they must leave this set in the same
# change that retargets the consumers.
INTERNAL_ALLOWED = frozenset(
    {
        ("commit_runtime", "commit_paths"),
        ("commit_runtime", "commit_policy"),
        ("git_utils", "commit_runtime"),
        ("source_worktree", "commit_paths"),
        ("source_worktree", "implementation_writer"),
        ("source_worktree", "worktree_manager"),
        ("worktree_manager", "git_utils"),
        ("worktree_manager", "implementation_writer"),
        ("worktree_manager", "repo_intake"),
    }
)

_LIB_ROOT = Path(hephaestus.__file__).parent
_AUTOMATION_ROOT = _LIB_ROOT / "automation"


def _package_of(py: Path) -> str:
    """Return the dotted package of *py* (for example ``hephaestus.automation``)."""
    parts = py.relative_to(_LIB_ROOT).parts
    return ".".join(("hephaestus", *parts[:-1]))


def _resolve_relative(level: int, module: str | None, package: str) -> str:
    """Resolve a relative-import base the way ``importlib`` would."""
    base = "." * level + (module or "")
    try:
        return importlib.util.resolve_name(base, package)
    except (ImportError, ValueError):
        return base


def collect_edges() -> tuple[frozenset[tuple[str, str]], frozenset[tuple[str, str]]]:
    """Collect the product-layer import edges of every mover module.

    Returns:
        A tuple of two frozensets of ``(consumer, imported)`` pairs. The
        first holds external edges (a non-mover ``hephaestus.automation.*``
        target); the second holds internal mover-to-mover edges.

    """
    external: set[tuple[str, str]] = set()
    internal: set[tuple[str, str]] = set()
    for mover in sorted(MOVERS):
        py = _AUTOMATION_ROOT / f"{mover}.py"
        if not py.exists():
            # The parse loop skips a mover whose source file is absent. The
            # name stays in MOVERS so import targets still classify as
            # internal; see the MOVERS docstring.
            continue
        package = _package_of(py)
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                entries: list[tuple[int, str | None, str]] = [
                    (0, None, alias.name) for alias in node.names
                ]
            elif isinstance(node, ast.ImportFrom):
                entries = [(node.level, node.module, alias.name) for alias in node.names]
            else:
                continue
            for level, module, name in entries:
                base = _resolve_relative(level, module, package) if level else module or ""
                target = f"{base}.{name}" if name != "*" else base
                if target != "hephaestus.automation" and not target.startswith(
                    "hephaestus.automation."
                ):
                    continue
                rest = target.removeprefix("hephaestus.automation").lstrip(".")
                imported = rest.split(".")[0] if rest else name.split(".")[0]
                pair = (mover, imported)
                (internal if imported in MOVERS else external).add(pair)
    return frozenset(external), frozenset(internal)


def test_mover_edges_match_the_exact_allowlist() -> None:
    """Verify the mover edge set equals the frozen allowlist exactly."""
    external, internal = collect_edges()
    extra_external = sorted(external - INTERNAL_ALLOWED)
    extra_internal = sorted(internal - INTERNAL_ALLOWED)
    assert external == frozenset(), (
        "movers must import zero non-mover hephaestus.automation modules; "
        f"unexpected external edges={extra_external}, "
        f"unexpected internal edges={extra_internal}"
    )
    assert internal == INTERNAL_ALLOWED, (
        "internal mover edges must equal INTERNAL_ALLOWED exactly; "
        f"added={sorted(internal - INTERNAL_ALLOWED)}, "
        f"stale={sorted(INTERNAL_ALLOWED - internal)}"
    )
