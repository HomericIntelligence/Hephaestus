"""Path resolution helpers for Hephaestus utilities.

This module centralizes lookup of the "projects root" directory — the
parent directory under which sibling HomericIntelligence repositories
are checked out. Callers resolve it through an explicit override,
current-checkout discovery, or the historical ``~/Projects`` default.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from hephaestus.config.child_environments import build_git_child_env

logger = logging.getLogger(__name__)

DEFAULT_PROJECTS_DIR: Path = Path.home() / "Projects"


def _current_checkout_parent() -> Path | None:
    """Return the parent of the current git checkout, if one can be detected."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
            env=build_git_child_env(),
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None

    checkout = Path(result.stdout.strip())
    if not checkout.name:
        return None
    # Automation issue worktrees live under
    # ``<repo>/build/.worktrees/issue-N``. When the loop is launched from one,
    # the projects root is still the parent of ``<repo>``, not
    # ``<repo>/build/.worktrees``; otherwise repo resolution nests clones and
    # worktrees under the issue worktree area.
    if (
        checkout.parent.name == ".worktrees"
        and checkout.parent.parent.name == "build"
        and checkout.parent.parent.parent.is_dir()
    ):
        parent = checkout.parent.parent.parent.parent
        logger.warning(
            "Current checkout %s is an automation issue worktree; using %s as projects root",
            checkout,
            parent,
        )
    else:
        parent = checkout.parent
    return parent if parent.is_dir() else None


def resolve_projects_dir(
    override: str | None = None,
    *,
    prefer_cwd_parent: bool = False,
) -> Path:
    """Resolve the projects root directory.

    Priority:
      1. explicit ``override`` argument (e.g. from a CLI flag)
      2. current checkout parent, when ``prefer_cwd_parent`` is true
      3. :data:`DEFAULT_PROJECTS_DIR` (``~/Projects``)

    Args:
        override: Optional explicit path (e.g. from a ``--projects-dir`` CLI
            flag). When provided, discovery and the default are skipped.
        prefer_cwd_parent: When true, use the parent of the current git
            checkout as the default projects root before falling back to
            :data:`DEFAULT_PROJECTS_DIR`. This is useful for automation loops
            launched from a checkout inside a nonstandard projects directory.

    Returns:
        The resolved projects-root directory as a :class:`pathlib.Path`.

    """
    if override is not None:
        return Path(override).resolve()

    if prefer_cwd_parent:
        cwd_parent = _current_checkout_parent()
        if cwd_parent is not None:
            return cwd_parent

    return DEFAULT_PROJECTS_DIR


__all__ = ["DEFAULT_PROJECTS_DIR", "resolve_projects_dir"]
