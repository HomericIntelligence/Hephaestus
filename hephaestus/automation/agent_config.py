"""Agent configuration for the automation pipeline.

Model selection, timeout defaults, and deterministic session identities share
one configuration owner. Claude subprocess execution remains in
``claude_invoke.py``.

Model selection
---------------
The caller selects a tool separately from its model string. An omitted model
uses that tool's configured default. Explicit names pass through without
catalog checks or alias translation. Role selections inherit the global model.
A quota fallback requires an explicit fallback model.

Reasoning effort
----------------
Each model option accepts ``MODEL[:EFFORT]``. The final nonempty colon segment
is a free-form provider effort. Codex receives ``model_reasoning_effort``.
OpenCode receives ``--variant``. Pi receives ``--thinking``. The value
``default`` selects the applicable provider default. Claude uses the base model
and its default effort.

Timeouts
--------
Timeouts are typed CLI/configuration values. The default accessors return
fixed values and do not read process environment variables.

Session naming
--------------
Every Claude invocation for the same ``(repo, issue, agent)`` tuple lands on
the SAME Claude CLI session — first call creates it via ``--session-id``,
every later call resumes it via ``--resume``. This restores prompt-cache
reuse across loop iterations *and* across main-bumps (#841): the artifact
being worked on is the issue/PR, not the commit at which the loop started,
so the session must persist as long as the issue does.

Human-readable name: ``<repo>_<issue>_<agent>``; the session ID is a UUIDv5
derived from that name plus a collision-resistant identity for the current Git
checkout. The identity is shared by a repository root and its registered
worktrees, but differs for unrelated clones. Different ``agent`` produces a
different UUID, preserving the "planner and reviewer are independent sessions"
property while still letting each agent resume itself across loop iterations.

The UUID is artifact-stable, while transcript lookup is restricted to the
current Git checkout's registered worktree family. This lets repo-root and
worktree callers share a transcript without allowing same-slug repositories
from unrelated checkouts to resume one another's sessions.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import uuid
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

from hephaestus.agents.model_selection import (
    AgentModelSelection,
    normalize_model_reference,
    parse_model_selection,
)
from hephaestus.agents.runtime import (
    normalize_provider_model_reference,
)
from hephaestus.constants import (
    AGENT_IMPL_TIMEOUT,
    AGENT_LEARN_TIMEOUT,
    AGENT_PLAN_TIMEOUT,
    AGENT_REVIEW_TIMEOUT,
)

logger = logging.getLogger(__name__)

# ── Model selection ──────────────────────────────────────────────────────────


def _normalize_configured_model(model: str) -> AgentModelSelection:
    """Return a literal model name and optional provider effort."""
    return parse_model_selection(model)


def normalize_claude_model(model: str) -> str:
    """Return the literal Claude model without the effort suffix."""
    return parse_model_selection(model).model


def _resolve_model(value: str | None, *, agent: str = "claude") -> str:
    """Use an explicit selection or the selected tool's configured default."""
    return normalize_provider_model_reference(agent, value or "")


def planner_model(value: str | None = None, *, agent: str = "claude") -> str:
    """Model used to generate implementation plans from issue text."""
    return _resolve_model(value, agent=agent)


def implementer_model(value: str | None = None, *, agent: str = "claude") -> str:
    """Return the model for implementation and its resumed repair sessions."""
    return _resolve_model(value, agent=agent)


def reviewer_model(value: str | None = None, *, agent: str = "claude") -> str:
    """Model used by plan/PR reviewers and the review-fix loop."""
    return _resolve_model(value, agent=agent)


def advise_model(value: str | None = None, *, agent: str = "claude") -> str:
    """Return an explicit host advice model or the tool default."""
    return _resolve_model(value, agent=agent)


def learn_model(value: str | None = None, *, agent: str = "claude") -> str:
    """Return an explicit host learning model or the tool default."""
    return _resolve_model(value, agent=agent)


def fallback_model(value: str | None = None, *, agent: str = "claude") -> str:
    """Model substituted when a model-specific usage cap is detected (#1793).

    A "reached your <model> limit … switch models with /model" 429 carries no
    reset epoch, so waiting cannot help — the invoke chokepoint retries on
    this model instead, only when an explicit fallback was supplied.
    """
    return _resolve_model(value, agent=agent)


# ── Subprocess timeouts ──────────────────────────────────────────────────────


# The shared invocation timeout is separate from each stage's default.
# Stage defaults use the AGENT_* values from ``hephaestus.constants``.
DEFAULT_AGENT_TIMEOUT: int = 7200
DEFAULT_THROUGHPUT_TIMEOUT: int = 1200
DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT: int = DEFAULT_THROUGHPUT_TIMEOUT


def agent_default_timeout() -> int:
    """Return the generic agent-invocation timeout in seconds (default 7200s).

    This is the phase-agnostic fallback for
    :func:`hephaestus.automation.claude_invoke.invoke_claude_with_session`
    when a caller omits ``timeout``. It must stay generic — no phase's own
    budget (planner/reviewer/implementer/...) may leak in here, since every
    agent type funnels through that single entry point (#1415).
    """
    return DEFAULT_AGENT_TIMEOUT


def planner_claude_timeout() -> int:
    """Timeout for planner agent calls (default 1200s)."""
    return AGENT_PLAN_TIMEOUT


def plan_reviewer_claude_timeout() -> int:
    """Timeout for agent calls inside the plan reviewer (default 1200s)."""
    return AGENT_REVIEW_TIMEOUT


def implementer_claude_timeout() -> int:
    """Timeout for the implementer's agent invocation (default 1800s)."""
    return AGENT_IMPL_TIMEOUT


def advise_claude_timeout() -> int:
    """Timeout for advise agent calls (default 7200s)."""
    return 7200


def pr_reviewer_claude_timeout() -> int:
    """Timeout for the PR reviewer's agent analysis (default 1200s)."""
    return AGENT_REVIEW_TIMEOUT


def learn_claude_timeout() -> int:
    """Timeout for ``/learn`` agent calls (default 1200s)."""
    return AGENT_LEARN_TIMEOUT


def git_message_agent_timeout() -> int:
    """Timeout for the lightweight commit/PR message writer (default 1200s)."""
    return DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT


# Re-exported from hephaestus.github.client so the gh-adapter timeout lives
# with the gh adapter; this alias preserves the legacy import path.
from hephaestus.github.client import gh_cli_timeout  # noqa: E402

# ── Session naming ───────────────────────────────────────────────────────────

# Session identifiers for the queue workers.
AGENT_PLANNER = "planner"
AGENT_PLAN_REVIEWER = "plan-reviewer"
AGENT_IMPLEMENTER = "implementer"
AGENT_PR_REVIEWER = "pr-reviewer"
# Commit-message generation uses a separate session. It must not inherit or
# change an implementation transcript.
AGENT_COMMIT_MESSAGE = "commit-message"

_ALL_AGENTS = frozenset(
    {
        AGENT_PLANNER,
        AGENT_PLAN_REVIEWER,
        AGENT_IMPLEMENTER,
        AGENT_PR_REVIEWER,
        AGENT_COMMIT_MESSAGE,
    }
)

# Each reviewer iteration has a separate session UUID. This prevents a review
# from inheriting the previous verdict. Planning and implementation resume
# their stage sessions.
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
    return {"PATH": os.defpath}


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
    if sep and base in _PER_ITERATION_REVIEWERS and suffix.isdigit():
        return True
    return bool(
        re.fullmatch(
            r"plan-reviewer-cycle-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}",
            agent,
        )
    )


def _model_token(model: str | None) -> str:
    """Normalize a model id into a filesystem/name-safe session-key token.

    ``claude --resume`` is locked to the model that created the session, so the
    model MUST be part of the deterministic key — otherwise switching
    ``--implementer-model`` (or any per-agent model) between runs resumes a
    transcript created under a different model, which the CLI rejects (#1166).
    Returns ``""`` when no model is given (legacy callers keep the old key).
    """
    if not model:
        return ""
    value = model.strip()
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", value)
    if safe == value:
        return value
    return f"{safe}~{sha256(value.encode()).hexdigest()}"


def session_name(repo: str, issue: int | str, agent: str, model: str | None = None) -> str:
    """Return the human-readable session name.

    The key is **(repo, issue, agent, model)**. The model is part of the key
    because ``claude --resume`` is locked to the creating model: a session
    created under one model cannot be resumed under another, so each
    ``(repo, issue, agent, model)`` gets its OWN create-once-then-resume lineage
    (#1166). Omitting ``model`` reproduces the historical ``(repo, issue, agent)``
    key for backward compatibility.

    No githash is included — the transcript persists across main-bumps so a
    long-running drive (CI fix loop, planner, implementer) resumes its context
    whenever the same artifact is touched again, instead of being discarded
    every time main advances (#841).

    Args:
        repo: Repository slug without owner (e.g. ``"Scylla"``).
        issue: Issue number; leading ``#`` is stripped.
        agent: One of the ``AGENT_*`` constants in this module.
        model: Optional model id; appended to the key when given so sessions
            never cross models.

    Returns:
        Underscore-joined name suitable for ``claude --name``.

    Raises:
        ValueError: If any component is empty or ``agent`` is unknown.

    """
    if not _is_valid_agent(agent):
        raise ValueError(
            f"unknown agent {agent!r}; must be one of {sorted(_ALL_AGENTS)} "
            f"or a scoped reviewer token (e.g. 'plan-reviewer-r0')"
        )
    repo_s = repo.strip()
    if not repo_s:
        raise ValueError("repo must be non-empty")
    issue_s = str(issue).lstrip("#").strip()
    if not issue_s:
        raise ValueError("issue must be non-empty")
    model_token = _model_token(model)
    base = f"{repo_s}_{issue_s}_{agent}"
    return f"{base}_{model_token}" if model_token else base


def _checkout_identity(cwd: Path, *, remaining_timeout: Callable[[], int] | None = None) -> str:
    """Return a collision-resistant identity shared by one Git worktree family."""
    resolved_cwd = cwd.resolve()
    timeout = min(5, remaining_timeout()) if remaining_timeout is not None else 5
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(resolved_cwd),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            check=True,
            capture_output=True,
            env=_repo_scoped_git_env(),
            text=True,
            timeout=timeout,
        )
        identity_path = Path(result.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        identity_path = resolved_cwd
    finally:
        if remaining_timeout is not None:
            remaining_timeout()
    return sha256(os.fsencode(identity_path)).hexdigest()


def session_uuid(
    repo: str,
    issue: int | str,
    agent: str,
    model: str | None = None,
    *,
    cwd: Path | None = None,
    remaining_timeout: Callable[[], int] | None = None,
) -> str:
    """Return the deterministic UUIDv5 session ID for one artifact and checkout.

    Unrelated checkouts get distinct session IDs even if Claude's lossy cwd
    encoding maps their project directories to the same transcript directory.
    The checkout identity is folded into the UUID filename itself before
    transcript lookup, so a pre-existing transcript from another checkout
    cannot satisfy :func:`resolve_session_jsonl_path` for this checkout. A
    repository root and its linked worktrees share the Git common-dir identity
    and therefore keep one resumable session lineage. When callers omit
    ``cwd``, the process working directory supplies the checkout identity;
    session IDs are never unscoped by accident.

    The optional callback checks the operation deadline and cancellation
    before and after Git discovery. Independent callers use a five-second
    Git timeout.
    """
    name = session_name(repo, issue, agent, model)
    checkout_cwd = cwd if cwd is not None else Path.cwd()
    name = f"{name}@{_checkout_identity(checkout_cwd, remaining_timeout=remaining_timeout)}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


_AUTO_IMPL_BRANCH_SUFFIX = "-auto-impl"


def issue_auto_impl_branch_name(issue_number: int | str) -> str:
    """Return the canonical branch name for an issue implementation PR."""
    return f"{issue_number}{_AUTO_IMPL_BRANCH_SUFFIX}"


def session_jsonl_path(uuid_str: str, cwd: Path) -> Path:
    """Return the path where Claude Code persists a session's transcript.

    Claude encodes the cwd into the projects-directory name by replacing
    BOTH ``/`` and ``.`` with ``-``. Probing only ``/`` (as a prior version
    of this helper did) misses every cwd containing a dot-prefixed segment
    like ``.worktrees``, ``.git``, or ``.venv``: ``transcript.exists()``
    returns False even though the JSONL is on disk, the caller goes down
    the ``--session-id`` create path, and the CLI rejects with ``Session ID
    <uuid> is already in use``. (#822)

    That directory encoding is lossy (for example, ``owner.a`` and
    ``owner-a`` collide), so checkout isolation is provided by the
    collision-resistant checkout identity folded into ``uuid_str`` by
    :func:`session_uuid`. The UUID filename, not the encoded parent directory,
    is therefore the isolation boundary.
    """
    encoded = str(cwd.resolve()).replace("/", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / encoded / f"{uuid_str}.jsonl"


def _registered_worktree_roots(
    cwd: Path, *, remaining_timeout: Callable[[], int] | None = None
) -> tuple[Path, ...]:
    """Return worktree roots registered to cwd's exact Git repository.

    The explicit invocation path is authoritative. Ambient Git repository
    environment variables are removed so an outer checkout cannot redirect
    this discovery to a different repository.
    """
    resolved_cwd = cwd.resolve()
    roots = {resolved_cwd}
    timeout = min(5, remaining_timeout()) if remaining_timeout is not None else 5
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(resolved_cwd),
                "worktree",
                "list",
                "--porcelain",
                "-z",
            ],
            check=True,
            capture_output=True,
            env=_repo_scoped_git_env(),
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return (resolved_cwd,)
    finally:
        if remaining_timeout is not None:
            remaining_timeout()

    for field in result.stdout.split("\0"):
        if field.startswith("worktree "):
            roots.add(Path(field.removeprefix("worktree ")).resolve())
    return tuple(sorted(roots, key=str))


def resolve_session_jsonl_path(
    uuid_str: str, cwd: Path, *, remaining_timeout: Callable[[], int] | None = None
) -> Path:
    """Resolve an existing transcript within cwd's registered worktree family.

    The exact cwd path remains the create location when no transcript exists.
    Existing transcripts are selected only from worktrees registered to the
    same Git repository, with lexical ordering making historical duplicates
    deterministic.

    The optional callback checks the operation deadline and cancellation
    before and after Git discovery. Independent callers use a five-second
    Git timeout.
    """
    # ``uuid_str`` is checkout-scoped by session_uuid. This matters before any
    # registered-worktree lookup: Claude's lossy cwd encoding can make the
    # expected parent directory belong to more than one unrelated checkout.
    expected = session_jsonl_path(uuid_str, cwd)
    candidates = {
        session_jsonl_path(uuid_str, root)
        for root in _registered_worktree_roots(cwd, remaining_timeout=remaining_timeout)
    }
    existing = sorted(
        (candidate for candidate in candidates if candidate.is_file()),
        key=str,
    )
    return existing[0] if existing else expected


__all__ = [
    # Session naming
    "AGENT_COMMIT_MESSAGE",
    "AGENT_IMPLEMENTER",
    # Timeouts
    "AGENT_IMPL_TIMEOUT",
    "AGENT_LEARN_TIMEOUT",
    "AGENT_PLANNER",
    "AGENT_PLAN_REVIEWER",
    "AGENT_PLAN_TIMEOUT",
    "AGENT_PR_REVIEWER",
    "AGENT_REVIEW_TIMEOUT",
    # Model selection
    "DEFAULT_AGENT_TIMEOUT",
    "DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT",
    "DEFAULT_THROUGHPUT_TIMEOUT",
    "advise_claude_timeout",
    "advise_model",
    "agent_default_timeout",
    "fallback_model",
    "gh_cli_timeout",
    "git_message_agent_timeout",
    "implementer_claude_timeout",
    "implementer_model",
    "issue_auto_impl_branch_name",
    "learn_claude_timeout",
    "learn_model",
    "normalize_claude_model",
    "normalize_model_reference",
    "parse_model_selection",
    "plan_reviewer_claude_timeout",
    "planner_claude_timeout",
    "planner_model",
    "pr_reviewer_claude_timeout",
    "resolve_session_jsonl_path",
    "reviewer_agent",
    "reviewer_model",
    "session_jsonl_path",
    "session_name",
    "session_uuid",
]
