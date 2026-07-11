"""Shared constants for the Hephaestus package."""

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

# Standard log format used across all library-layer logging utilities
# (``logging.utils.setup_logging`` / ``get_logger``). Uses " - " field
# separators. Library code logs with this format.
LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Canonical log format for automation/CLI entry points (planner, reviewers,
# ci_driver, implementer, the ``hephaestus-*`` CLIs, ...). The bracketed
# "[LEVEL] name:" layout is more readable for interactive CLI output. Defined
# here as the single source of truth so the format cannot drift across the
# automation and CLI modules (issue #1427). Library code uses ``LOG_FORMAT``;
# automation/CLI entry points use ``AUTOMATION_LOG_FORMAT``.
AUTOMATION_LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Shared ``datefmt`` for the automation/CLI ``basicConfig()`` calls that pair
# with ``AUTOMATION_LOG_FORMAT``.
LOG_DATEFMT: str = "%Y-%m-%d %H:%M:%S"

# Base field names included in every JSON log record.
JSON_LOG_FIELDS: tuple[str, ...] = ("timestamp", "level", "logger", "message")

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

AGENT_IMPL_TIMEOUT: int = 1800
AGENT_REVIEW_TIMEOUT: int = 1200
AGENT_PLAN_TIMEOUT: int = 1200
AGENT_LEARN_TIMEOUT: int = 1200
AGENT_GIT_TIMEOUT: int = 30
AGENT_CLONE_TIMEOUT: int = 120
AGENT_AUTH_STATUS_TIMEOUT: int = 10
AGENT_REBASE_TIMEOUT: int = 2400
DIFF_COLLECT_TIMEOUT: int = 60
PRE_PR_TEST_TIMEOUT: int = 600

# Marker file that identifies the repo root in a dev checkout.
_REPO_ROOT_MARKER = "pyproject.toml"


def read_timeout_env(
    env_name: str,
    default: int,
    *,
    legacy_names: tuple[str, ...] = (),
) -> int:
    """Read a timeout env var at call time, falling back to ``default``."""
    for name in (env_name, *legacy_names):
        raw = os.environ.get(name)
        if raw is None:
            continue
        try:
            return int(raw)
        except ValueError:
            _logger.warning(
                "Ignoring non-integer %s=%r; using default %ds",
                name,
                raw,
                default,
            )
            return default
    return default


def agent_impl_timeout() -> int:
    """Return the implementation-agent timeout in seconds."""
    return read_timeout_env("HEPH_AGENT_IMPL_TIMEOUT", AGENT_IMPL_TIMEOUT)


def agent_review_timeout() -> int:
    """Return the review-agent timeout in seconds."""
    return read_timeout_env("HEPH_AGENT_REVIEW_TIMEOUT", AGENT_REVIEW_TIMEOUT)


def agent_plan_timeout() -> int:
    """Return the planning-agent timeout in seconds."""
    return read_timeout_env("HEPH_AGENT_PLAN_TIMEOUT", AGENT_PLAN_TIMEOUT)


def agent_learn_timeout() -> int:
    """Return the learn-agent timeout in seconds."""
    return read_timeout_env("HEPH_AGENT_LEARN_TIMEOUT", AGENT_LEARN_TIMEOUT)


def agent_git_timeout() -> int:
    """Return the timeout for short agent-adjacent git commands in seconds."""
    return read_timeout_env("HEPH_AGENT_GIT_TIMEOUT", AGENT_GIT_TIMEOUT)


def agent_clone_timeout() -> int:
    """Return the timeout for Mnemosyne clone setup in seconds."""
    return read_timeout_env("HEPH_AGENT_CLONE_TIMEOUT", AGENT_CLONE_TIMEOUT)


def agent_auth_status_timeout() -> int:
    """Return the timeout for agent authentication status probes in seconds."""
    return read_timeout_env("HEPH_AGENT_AUTH_STATUS_TIMEOUT", AGENT_AUTH_STATUS_TIMEOUT)


def agent_rebase_timeout() -> int:
    """Return the timeout for direct-agent rebase/conflict work in seconds."""
    return read_timeout_env("HEPH_AGENT_REBASE_TIMEOUT", AGENT_REBASE_TIMEOUT)


def diff_collect_timeout() -> int:
    """Return the timeout for implementation-review diff collection in seconds."""
    return read_timeout_env("HEPH_DIFF_COLLECT_TIMEOUT", DIFF_COLLECT_TIMEOUT)


def pre_pr_test_timeout() -> int:
    """Return the timeout for the optional pre-PR test gate in seconds."""
    return read_timeout_env("HEPH_PRE_PR_TEST_TIMEOUT", PRE_PR_TEST_TIMEOUT)


def repo_root() -> Path:
    """Resolve the Hephaestus repo root.

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
