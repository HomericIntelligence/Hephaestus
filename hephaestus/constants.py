"""Shared constants for the ProjectHephaestus package."""

from __future__ import annotations

import logging
import os
from pathlib import Path

_logger = logging.getLogger(__name__)

# Default directories to exclude when scanning files recursively.
# Used across markdown, validation, and other file-traversal utilities.
DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".git",
        "venv",
        "__pycache__",
        ".tox",
        ".pixi",
        ".pytest_cache",
        "dist",
        "build",
        ".mypy_cache",
        ".eggs",
    }
)

# Standard log format used across all logging utilities.
LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Base field names included in every JSON log record.
JSON_LOG_FIELDS: tuple[str, ...] = ("timestamp", "level", "logger", "message")


def _read_timeout_env(name: str, default: int) -> int:
    """Return an integer timeout override or the documented default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        _logger.warning("Ignoring non-integer %s=%r; using default %ds", name, raw, default)
        return default


AGENT_AUTH_STATUS_TIMEOUT: int = _read_timeout_env("HEPH_AGENT_AUTH_STATUS_TIMEOUT", 10)
AGENT_GIT_TIMEOUT: int = _read_timeout_env("HEPH_AGENT_GIT_TIMEOUT", 30)
AGENT_DIFF_TIMEOUT: int = _read_timeout_env("HEPH_AGENT_DIFF_TIMEOUT", 60)
AGENT_CLONE_TIMEOUT: int = _read_timeout_env("HEPH_AGENT_CLONE_TIMEOUT", 120)
AGENT_DEFAULT_TIMEOUT: int = _read_timeout_env("HEPH_AGENT_DEFAULT_TIMEOUT", 300)
AGENT_PLAN_TIMEOUT: int = _read_timeout_env("HEPH_AGENT_PLAN_TIMEOUT", 300)
AGENT_LEARN_TIMEOUT: int = _read_timeout_env("HEPH_AGENT_LEARN_TIMEOUT", 300)
AGENT_REVIEW_TIMEOUT: int = _read_timeout_env("HEPH_AGENT_REVIEW_TIMEOUT", 600)
AGENT_PRE_PR_TEST_TIMEOUT: int = _read_timeout_env("HEPH_AGENT_PRE_PR_TEST_TIMEOUT", 600)
AGENT_IMPL_TIMEOUT: int = _read_timeout_env("HEPH_AGENT_IMPL_TIMEOUT", 1800)
AGENT_REBASE_TIMEOUT: int = _read_timeout_env("HEPH_AGENT_REBASE_TIMEOUT", 2400)

# Canonical transient-failure substrings shared by the resilience and retry
# layers. Both ``subprocess_resilience.TRANSIENT_ERROR_PATTERNS`` and
# ``retry.NETWORK_ERROR_KEYWORDS`` derive from this core so the overlapping
# signals cannot drift (see issue #1205). Each consumer adds its own
# layer-specific extras on top: the retry layer additionally treats
# rate-limit/throttle signals as retryable, which the resilience layer
# intentionally does NOT retry (rate-limit passthrough is handled by callers).
TRANSIENT_ERROR_CORE: frozenset[str] = frozenset(
    {
        "connection",
        "timed out",
        "temporary failure",
        "could not resolve",
        "network unreachable",
        "503",
        "502",
        "504",
    }
)

# Marker file that identifies the repo root in a dev checkout.
_REPO_ROOT_MARKER = "pyproject.toml"


def repo_root() -> Path:
    """Resolve the ProjectHephaestus repo root.

    Priority:
      1. ``$HEPHAESTUS_REPO_ROOT`` env var, IFF that directory contains a
         ``pyproject.toml`` marker.
      2. Walk upward from this module's ``__file__`` until a directory
         containing ``pyproject.toml`` is found.

    The env-var override exists so editable installs (where ``__file__``
    resolves under ``site-packages``) and CI containers can pin the
    location explicitly without coupling to package layout.
    """
    env = os.environ.get("HEPHAESTUS_REPO_ROOT")
    if env:
        candidate = Path(env)
        if (candidate / _REPO_ROOT_MARKER).is_file():
            return candidate
        _logger.warning(
            "HEPHAESTUS_REPO_ROOT=%s missing %s; falling back to __file__ walk",
            env,
            _REPO_ROOT_MARKER,
        )

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / _REPO_ROOT_MARKER).is_file():
            return parent
    raise RuntimeError(
        f"Could not locate repo root: no {_REPO_ROOT_MARKER} above {here}. "
        "Set $HEPHAESTUS_REPO_ROOT to pin the location explicitly."
    )


def scripts_dir() -> Path:
    """Return the repo's top-level ``scripts/`` directory."""
    return repo_root() / "scripts"
