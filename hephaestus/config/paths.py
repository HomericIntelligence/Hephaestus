"""Path resolution helpers for ProjectHephaestus utilities.

This module centralizes lookup of the "projects root" directory — the
parent directory under which sibling HomericIntelligence repositories
are checked out. Historically this was hardcoded to ``~/Projects``;
callers now resolve it via :func:`resolve_projects_dir`, which honors
an explicit override, the ``PROJECTS_ROOT`` environment variable, or
falls back to the historical default with a warning.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PROJECTS_DIR: Path = Path.home() / "Projects"

# Module-level guard so the warning fires at most once per
# (override, env) tuple per process. Tests can clear this to re-trigger.
_warned_keys: set[tuple[str | None, str | None]] = set()


def resolve_projects_dir(override: str | None = None) -> Path:
    """Resolve the projects root directory.

    Priority:
      1. explicit ``override`` argument (e.g. from a CLI flag)
      2. ``$PROJECTS_ROOT`` environment variable, IFF that directory exists
      3. :data:`DEFAULT_PROJECTS_DIR` (``~/Projects``)

    A warning is emitted when the fallback path is taken because neither
    an override nor a usable ``PROJECTS_ROOT`` was supplied. A distinct
    warning fires when ``PROJECTS_ROOT`` is set but its directory does
    not exist. Warnings are de-duplicated per process per
    ``(override, env)`` tuple.

    Args:
        override: Optional explicit path (e.g. from a ``--projects-dir`` CLI
            flag). When provided, the env var and default are skipped and no
            warning is emitted.

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
