"""Path resolution helpers for Hephaestus utilities.

This module centralizes lookup of the "projects root" directory — the
parent directory under which sibling HomericIntelligence repositories
are checked out. Historically this was hardcoded to ``~/Projects``;
callers now resolve it via :func:`resolve_projects_dir`, which honors
an explicit override, the ``PROJECTS_ROOT`` environment variable, or
falls back to the current checkout's parent or the historical default.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PROJECTS_DIR: Path = Path.home() / "Projects"

# Module-level guard so the warning fires at most once per
# (override, env) tuple per process. Tests can clear this to re-trigger.
_warned_keys: set[tuple[str | None, str | None]] = set()


def _current_checkout_parent() -> Path | None:
    """Return the parent of the current git checkout, if one can be detected."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
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
      2. ``$PROJECTS_ROOT`` environment variable, IFF that directory exists
      3. current checkout parent, when ``prefer_cwd_parent`` is true
      4. :data:`DEFAULT_PROJECTS_DIR` (``~/Projects``)

    A warning is emitted when the fallback path is taken because neither
    an override nor a usable ``PROJECTS_ROOT`` was supplied. A distinct
    warning fires when ``PROJECTS_ROOT`` is set but its directory does
    not exist. Warnings are de-duplicated per process per
    ``(override, env)`` tuple.

    Args:
        override: Optional explicit path (e.g. from a ``--projects-dir`` CLI
            flag). When provided, the env var and default are skipped and no
            warning is emitted.
        prefer_cwd_parent: When true, use the parent of the current git
            checkout as the default projects root before falling back to
            :data:`DEFAULT_PROJECTS_DIR`. This is useful for automation loops
            launched from a checkout inside a nonstandard projects directory.

    Returns:
        The resolved projects-root directory as a :class:`pathlib.Path`.

    """
    if override is not None:
        return Path(override)

    env = os.environ.get("PROJECTS_ROOT")
    key: tuple[str | None, str | None] = (override, env)

    if env:
        env_path = Path(env)
        if env_path.is_dir():
            return env_path
        if key not in _warned_keys:
            logger.warning(
                "PROJECTS_ROOT=%s does not exist; falling back to %s",
                env,
                DEFAULT_PROJECTS_DIR,
            )
            _warned_keys.add(key)
        return DEFAULT_PROJECTS_DIR

    if prefer_cwd_parent:
        cwd_parent = _current_checkout_parent()
        if cwd_parent is not None:
            return cwd_parent

    if key not in _warned_keys:
        # Benign: an unset PROJECTS_ROOT with no override is the normal case;
        # the default is a correct fallback. DEBUG (visible under -v) rather
        # than routine WARNING noise (#1556). A *nonexistent* PROJECTS_ROOT
        # (above) stays at WARNING — that is operator misconfiguration.
        logger.debug(
            "PROJECTS_ROOT not set and no --projects-dir given; falling back to default: %s",
            DEFAULT_PROJECTS_DIR,
        )
        _warned_keys.add(key)
    return DEFAULT_PROJECTS_DIR


__all__ = ["DEFAULT_PROJECTS_DIR", "resolve_projects_dir"]
