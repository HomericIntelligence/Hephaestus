"""Deterministic Claude-session naming for the automation pipeline.

Within a given trunk git SHA, every Claude invocation for the same
``(repo, issue, agent)`` tuple lands on the SAME Claude CLI session — first
call creates it via ``--session-id``, every later call resumes it via
``--resume``. This restores prompt-cache reuse across loop iterations.

Names
-----
Human-readable: ``<repo>_<issue>_<agent>_<githash>``

Session ID (what ``claude --session-id`` accepts):
``str(uuid.uuid5(NAMESPACE_DNS, <human-readable>))``

UUIDv5 is deterministic, so no state file is needed — two callers on
different machines, given the same tuple, will produce the same UUID.

Agent strings
-------------
Different ``agent`` produces a different UUID, which preserves the
"planner and reviewer are independent sessions" property while still
letting each agent resume itself across loop iterations.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

# Agent identifiers — keep in sync with phase modules.
AGENT_PLANNER = "planner"
AGENT_PLAN_REVIEWER = "plan-reviewer"
AGENT_ADVISE = "advise"
AGENT_LEARNINGS = "learnings"
AGENT_IMPLEMENTER = "implementer"
AGENT_PR_REVIEWER = "pr-reviewer"
AGENT_ADDRESS_REVIEW = "address-review"
AGENT_CI_DRIVER = "ci-driver"

_ALL_AGENTS = frozenset(
    {
        AGENT_PLANNER,
        AGENT_PLAN_REVIEWER,
        AGENT_ADVISE,
        AGENT_LEARNINGS,
        AGENT_IMPLEMENTER,
        AGENT_PR_REVIEWER,
        AGENT_ADDRESS_REVIEW,
        AGENT_CI_DRIVER,
    }
)


def session_name(repo: str, issue: int | str, agent: str, githash: str) -> str:
    """Return the human-readable session name.

    Args:
        repo: Repository slug without owner (e.g. ``"ProjectScylla"``).
        issue: Issue number; leading ``#`` is stripped.
        agent: One of the ``AGENT_*`` constants in this module.
        githash: Short SHA of the trunk HEAD captured at loop start.

    Returns:
        Underscore-joined name suitable for ``claude --name``.

    Raises:
        ValueError: If any component is empty or ``agent`` is unknown.

    """
    if agent not in _ALL_AGENTS:
        raise ValueError(f"unknown agent {agent!r}; must be one of {sorted(_ALL_AGENTS)}")
    repo_s = repo.strip()
    githash_s = githash.strip()
    if not repo_s or not githash_s:
        raise ValueError("repo and githash must be non-empty")
    issue_s = str(issue).lstrip("#").strip()
    if not issue_s:
        raise ValueError("issue must be non-empty")
    return f"{repo_s}_{issue_s}_{agent}_{githash_s}"


def session_uuid(repo: str, issue: int | str, agent: str, githash: str) -> str:
    """Return the deterministic UUIDv5 session ID for the tuple."""
    name = session_name(repo, issue, agent, githash)
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


def short_githash(repo_path: Path) -> str:
    """Return ``git -C <repo_path> rev-parse --short=7 HEAD`` or ``"unknown"``.

    Used by the loop driver to capture the trunk SHA once per repo
    iteration so all phases in that iteration share a session family.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--short=7", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def current_trunk_githash(repo_path: Path | None = None) -> str:
    """Return the trunk SHA every phase should use for session naming.

    Reads ``HEPH_TRUNK_GITHASH`` (set once per repo loop iteration by
    ``scripts/run_automation_loop.sh``) so all phases within one loop
    iteration share the same SHA. Falls back to live ``git rev-parse`` on
    ``repo_path`` (or cwd) when the env var is unset — useful for one-off
    CLI invocations outside the loop.
    """
    env_value = os.environ.get("HEPH_TRUNK_GITHASH")
    if env_value:
        return env_value
    return short_githash(repo_path if repo_path is not None else Path.cwd())


def session_jsonl_path(uuid_str: str, cwd: Path) -> Path:
    """Return the path where Claude Code persists a session's transcript.

    Claude encodes the cwd into the directory name by replacing each ``/``
    with ``-``. Used to detect whether a session already exists (and thus
    should be resumed) or needs to be created.
    """
    encoded = str(cwd.resolve()).replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded / f"{uuid_str}.jsonl"
