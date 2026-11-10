"""Shared invocation configuration for provider runtimes.

Model resolution, the generic agent timeout, and deterministic session
identities live in one library owner. The automation layer imports them from
here; Claude subprocess execution stays in :mod:`hephaestus.agents.claude_invoke`.

Model selection
---------------
``_resolve_model`` returns an explicit selection or the selected tool's
configured default. Role-specific wrappers live in the caller's configuration
module.

Timeouts
--------
``DEFAULT_AGENT_TIMEOUT`` is the phase-agnostic invocation fallback. Stage
budgets stay in the caller's configuration module.

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

import os
import re
import subprocess
import uuid
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

from hephaestus.agents.runtime import normalize_provider_model_reference

# ── Model selection ──────────────────────────────────────────────────────────


def _resolve_model(value: str | None, *, agent: str = "claude") -> str:
    """Use an explicit selection or the selected tool's configured default."""
    return normalize_provider_model_reference(agent, value or "")


def fallback_model(value: str | None = None, *, agent: str = "claude") -> str:
    """Model substituted when a model-specific usage cap is detected (#1793).

    A "reached your <model> limit … switch models with /model" 429 carries no
    reset epoch, so waiting cannot help — the invoke chokepoint retries on
    this model instead, only when an explicit fallback was supplied.
    """
    return _resolve_model(value, agent=agent)


# ── Subprocess timeouts ──────────────────────────────────────────────────────


# The shared invocation timeout is separate from each stage's default.
# Stage defaults live in the caller's configuration module.
DEFAULT_AGENT_TIMEOUT: int = 7200


def agent_default_timeout() -> int:
    """Return the generic agent-invocation timeout in seconds (default 7200s).

    This is the phase-agnostic fallback for
    :func:`hephaestus.agents.claude_invoke.invoke_claude_with_session`
    when a caller omits ``timeout``. It must stay generic — no phase's own
    budget (planner/reviewer/implementer/...) may leak in here, since every
    agent type funnels through that single entry point (#1415).
    """
    return DEFAULT_AGENT_TIMEOUT


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
    """Return True for a known agent, reviewer token, cycle token, or recovery key."""
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
    ) or bool(
        re.fullmatch(
            r"requirements-recovery(?:-review)?:[0-9]+:[0-9a-f]+",
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


def _joined_session_name(
    repo: str, issue: int | str, agent: str, model: str | None, *, for_display: bool
) -> str:
    """Assemble the underscore-joined session string shared by name and identity.

    ``for_display=True`` normalizes the agent token via :func:`_model_token`
    into a ``claude --name``-safe component (recovery keys contain ``:``).
    The session identity input (``for_display=False``) keeps the RAW agent key,
    so display-name normalization never changes a session's uuid5 identity.
    """
    if not _is_valid_agent(agent):
        raise ValueError(
            f"unknown agent {agent!r}; must be one of {sorted(_ALL_AGENTS)} "
            f"or a scoped reviewer token (e.g. 'plan-reviewer-r0'), "
            f"a plan-reviewer-cycle-<uuid> token, or a recovery key "
            f"('requirements-recovery:<issue>:<hex>')"
        )
    repo_s = repo.strip()
    if not repo_s:
        raise ValueError("repo must be non-empty")
    issue_s = str(issue).lstrip("#").strip()
    if not issue_s:
        raise ValueError("issue must be non-empty")
    model_token = _model_token(model)
    agent_token = _model_token(agent) if for_display else agent
    base = f"{repo_s}_{issue_s}_{agent_token}"
    return f"{base}_{model_token}" if model_token else base


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
        Underscore-joined name suitable for ``claude --name``. The agent token
        is normalized via :func:`_model_token` (recovery keys contain ``:``);
        :func:`session_uuid` keeps the RAW key as its identity input, so the
        display form never changes the session identity.

    Raises:
        ValueError: If any component is empty or ``agent`` is unknown.

    """
    return _joined_session_name(repo, issue, agent, model, for_display=True)


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
    name = _joined_session_name(repo, issue, agent, model, for_display=False)
    checkout_cwd = cwd if cwd is not None else Path.cwd()
    name = f"{name}@{_checkout_identity(checkout_cwd, remaining_timeout=remaining_timeout)}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


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
    # Model selection
    "DEFAULT_AGENT_TIMEOUT",
    "agent_default_timeout",
    "fallback_model",
    # Session naming
    "AGENT_COMMIT_MESSAGE",
    "AGENT_IMPLEMENTER",
    "AGENT_PLANNER",
    "AGENT_PLAN_REVIEWER",
    "AGENT_PR_REVIEWER",
    "resolve_session_jsonl_path",
    "reviewer_agent",
    "session_jsonl_path",
    "session_name",
    "session_uuid",
]
