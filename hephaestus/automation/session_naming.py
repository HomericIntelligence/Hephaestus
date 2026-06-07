"""Deterministic Claude-session naming for the automation pipeline.

Every Claude invocation for the same ``(repo, issue, agent)`` tuple lands on
the SAME Claude CLI session — first call creates it via ``--session-id``,
every later call resumes it via ``--resume``. This restores prompt-cache
reuse across loop iterations *and* across main-bumps (#841): the artifact
being worked on is the issue/PR, not the commit at which the loop started,
so the session must persist as long as the issue does.

Names
-----
Human-readable: ``<repo>_<issue>_<agent>``

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
# #1083: cheap read-only sub-agent that labels each review comment's fix
# difficulty (simple/medium/hard) to pick the per-comment fixer's model tier.
AGENT_COMMENT_CLASSIFIER = "comment-classifier"

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
        AGENT_COMMENT_CLASSIFIER,
    }
)

# Reviewer agents that get a *fresh* session per loop iteration (see
# reviewer_agent). The plan/impl reviewers must stay unbiased across the
# review loop, so each iteration uses a distinct session UUID rather than
# resuming the prior iteration's transcript. Implementer/planner/ci-driver
# deliberately do NOT appear here — they resume one session across their stage.
_PER_ITERATION_REVIEWERS = frozenset({AGENT_PLAN_REVIEWER, AGENT_PR_REVIEWER})
_GIT_REPO_ENV_KEYS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def _repo_scoped_git_env() -> dict[str, str]:
    """Return environment for explicit ``git -C`` calls, ignoring outer repos."""
    env = os.environ.copy()
    for key in _GIT_REPO_ENV_KEYS:
        env.pop(key, None)
    return env


def reviewer_agent(base_agent: str, iteration: int) -> str:
    """Return a per-iteration reviewer agent token.

    The two review loops (plan review, PR/impl review) must run as a *fresh*
    Claude session every iteration so the reviewer never inherits its own
    previous verdict. Because the session UUID is derived from the agent
    string (see :func:`session_uuid`), appending the iteration index yields a
    new session per round while keeping the family human-readable.

    Args:
        base_agent: ``AGENT_PLAN_REVIEWER`` or ``AGENT_PR_REVIEWER``.
        iteration: Zero-based review-loop iteration index.

    Returns:
        ``f"{base_agent}-r{iteration}"`` (e.g. ``"plan-reviewer-r0"``).

    Raises:
        ValueError: If ``base_agent`` is not a per-iteration reviewer or
            ``iteration`` is negative.

    """
    if base_agent not in _PER_ITERATION_REVIEWERS:
        raise ValueError(
            f"reviewer_agent expects one of {sorted(_PER_ITERATION_REVIEWERS)}, got {base_agent!r}"
        )
    if iteration < 0:
        raise ValueError(f"iteration must be >= 0, got {iteration}")
    return f"{base_agent}-r{iteration}"


def _is_valid_agent(agent: str) -> bool:
    """Return True for a known base agent or a per-iteration reviewer token."""
    if agent in _ALL_AGENTS:
        return True
    # Accept the reviewer_agent() form: "<base>-r<N>".
    base, sep, suffix = agent.rpartition("-r")
    return bool(sep) and base in _PER_ITERATION_REVIEWERS and suffix.isdigit()


def session_name(repo: str, issue: int | str, agent: str) -> str:
    """Return the human-readable session name.

    The tuple is intentionally **(repo, issue, agent) only** — no githash.
    The Claude transcript persists across main-bumps so a long-running
    drive (CI fix loop, planner, implementer) resumes its context whenever
    the same artifact (issue/PR) is touched again, instead of being
    discarded every time main advances (#841).

    Args:
        repo: Repository slug without owner (e.g. ``"ProjectScylla"``).
        issue: Issue number; leading ``#`` is stripped.
        agent: One of the ``AGENT_*`` constants in this module.

    Returns:
        Underscore-joined name suitable for ``claude --name``.

    Raises:
        ValueError: If any component is empty or ``agent`` is unknown.

    """
    if not _is_valid_agent(agent):
        raise ValueError(
            f"unknown agent {agent!r}; must be one of {sorted(_ALL_AGENTS)} "
            f"or a per-iteration reviewer token (e.g. 'plan-reviewer-r0')"
        )
    repo_s = repo.strip()
    if not repo_s:
        raise ValueError("repo must be non-empty")
    issue_s = str(issue).lstrip("#").strip()
    if not issue_s:
        raise ValueError("issue must be non-empty")
    return f"{repo_s}_{issue_s}_{agent}"


def session_uuid(repo: str, issue: int | str, agent: str) -> str:
    """Return the deterministic UUIDv5 session ID for the tuple."""
    name = session_name(repo, issue, agent)
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
            env=_repo_scoped_git_env(),
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

    Claude encodes the cwd into the projects-directory name by replacing
    BOTH ``/`` and ``.`` with ``-``. Probing only ``/`` (as a prior version
    of this helper did) misses every cwd containing a dot-prefixed segment
    like ``.worktrees``, ``.git``, or ``.venv``: ``transcript.exists()``
    returns False even though the JSONL is on disk, the caller goes down
    the ``--session-id`` create path, and the CLI rejects with ``Session ID
    <uuid> is already in use``. (#822)
    """
    encoded = str(cwd.resolve()).replace("/", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / encoded / f"{uuid_str}.jsonl"
