"""Agent configuration for the automation pipeline.

Merges the formerly-separate ``claude_models``, ``claude_timeouts``, and
``session_naming`` modules (#1441): model selection, subprocess timeouts, and
deterministic Claude-session naming all answer one question — "how is an
agent invocation configured?" ``claude_invoke.py`` (subprocess logic) stays
separate and imports session naming from here.

The three original module paths are retained as thin re-export shims so
existing callers keep working unchanged.

Model selection
---------------
Each Claude automation phase calls the ``claude`` CLI with ``--model <id>`` so
the chosen model is pinned regardless of the user's CLI default. The mapping
reflects the cost/quality tradeoff for each phase:

- Planning needs reasoning quality but few tokens overall → Opus
- Implementation is a long mechanical tool-use loop → Haiku
- Reviewers / advise / learn → Sonnet (middle ground)
- Git/PR message writing is tiny metadata generation → Haiku

Each function honors a ``HEPH_<PHASE>_MODEL`` environment variable so an
operator can override without code changes (e.g. when one tier's quota is
exhausted). Unknown overrides emit a **warning** but are still accepted so
operators can experiment with preview models without a code change.

Timeouts
--------
Each phase that shells out to an agent CLI or ``gh`` has historically
hard-coded its own timeout. Centralising them here gives operators a way to
tune slow repos / network conditions without code changes via
``HEPH_<PHASE>_AGENT_TIMEOUT`` environment variables (values in seconds).

If an env var is set but not an integer, the default is used and a warning is
logged on first read; we never crash on a malformed timeout because the cost
of a runtime startup error is higher than the cost of falling back.

Session naming
--------------
Every Claude invocation for the same ``(repo, issue, agent)`` tuple lands on
the SAME Claude CLI session — first call creates it via ``--session-id``,
every later call resumes it via ``--resume``. This restores prompt-cache
reuse across loop iterations *and* across main-bumps (#841): the artifact
being worked on is the issue/PR, not the commit at which the loop started,
so the session must persist as long as the issue does.

Human-readable name: ``<repo>_<issue>_<agent>``; the session ID is
``str(uuid.uuid5(NAMESPACE_DNS, <human-readable>))``. UUIDv5 is
deterministic, so no state file is needed — two callers on different machines,
given the same tuple, will produce the same UUID. Different ``agent`` produces
a different UUID, preserving the "planner and reviewer are independent
sessions" property while still letting each agent resume itself across loop
iterations.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import uuid
from pathlib import Path

from hephaestus.constants import (
    AGENT_IMPL_TIMEOUT,
    AGENT_LEARN_TIMEOUT,
    AGENT_PLAN_TIMEOUT,
    AGENT_REVIEW_TIMEOUT,
    read_timeout_env,
)

logger = logging.getLogger(__name__)

# ── Model selection ──────────────────────────────────────────────────────────

OPUS_47 = "claude-opus-4-7"
SONNET_46 = "claude-sonnet-4-6"
HAIKU_45 = "claude-haiku-4-5"
CODEX_ADVISE = "gpt-5.4-mini"

# Newer tiers that are valid model IDs but not the per-phase defaults. Listed in
# the known set so pinning them via HEPH_*_MODEL doesn't emit a spurious
# "Unknown model" warning. (Fable and Mythos sit above Opus; 4.8 is the
# current Opus.)
OPUS_48 = "claude-opus-4-8"
FABLE_5 = "claude-fable-5"
SONNET_5 = "claude-sonnet-5"
SONNET_50 = SONNET_5
MYTHOS = "claude-mythos-5"
SONNET = SONNET_46
OPUS = OPUS_48
HAIKU = HAIKU_45
FABLE = FABLE_5

# The set of model IDs the automation suite recognizes. Overrides to values
# outside this set are still accepted (operators may have preview access) but
# trigger a one-time warning so misconfigured/typo'd env vars are visible.
_KNOWN_MODELS: frozenset[str] = frozenset(
    {OPUS_47, SONNET_46, SONNET_5, HAIKU_45, OPUS_48, FABLE_5, MYTHOS}
)
_MODEL_ALIASES: dict[str, str] = {
    "fable": FABLE,
    "mythos": MYTHOS,
}


def normalize_claude_model(model: str) -> str:
    """Return the Claude CLI model ID for a full model ID or supported shorthand."""
    value = model.strip()
    if not value:
        return ""
    return _MODEL_ALIASES.get(value.lower(), value)


def _resolve_model(env_var: str, default: str) -> str:
    """Return the model ID for *env_var*, warning if the value is unknown.

    Args:
        env_var: Name of the environment variable to check.
        default: Default model ID to use when the variable is unset.

    Returns:
        The resolved model ID string.

    """
    value = os.environ.get(env_var)
    if value is None:
        return default
    resolved = normalize_claude_model(value)
    if resolved not in _KNOWN_MODELS:
        logger.warning(
            "Unknown model %r set in %s (known: %s). "
            "Proceeding, but verify the model ID is correct.",
            resolved,
            env_var,
            ", ".join(sorted(_KNOWN_MODELS)),
        )
    return resolved


def planner_model() -> str:
    """Model used to generate implementation plans from issue text."""
    return _resolve_model("HEPH_PLANNER_MODEL", OPUS)


def implementer_model() -> str:
    """Model used by the implementer worker that runs ``claude`` in a worktree.

    Also used for any phase that resumes the implementer's session
    (e.g. address-review, ci-driver), since ``claude --resume`` is locked
    to the model that created the session.
    """
    return _resolve_model("HEPH_IMPLEMENTER_MODEL", HAIKU)


def reviewer_model() -> str:
    """Model used by plan/PR reviewers and the review-fix loop."""
    return _resolve_model("HEPH_REVIEWER_MODEL", SONNET)


def advise_model() -> str:
    """Claude model used by the advise skill-selection step."""
    return _resolve_model("HEPH_ADVISE_MODEL", HAIKU)


def codex_advise_model() -> str:
    """Codex model used by the advise skill-selection step."""
    return CODEX_ADVISE


def learn_model() -> str:
    """Model used by /learn and follow-up issue filing."""
    return _resolve_model("HEPH_LEARN_MODEL", HAIKU)


def git_message_model() -> str:
    """Model used by the lightweight commit/PR message writer."""
    return _resolve_model("HEPH_GIT_MESSAGE_MODEL", HAIKU)


def fallback_model() -> str:
    """Model substituted when a model-specific usage cap is detected (#1793).

    A "reached your <model> limit … switch models with /model" 429 carries no
    reset epoch, so waiting cannot help — the invoke chokepoint retries on
    this model instead. Honors ``HEPH_FALLBACK_MODEL``; defaults to
    :data:`OPUS` (the current Opus).
    """
    return _resolve_model("HEPH_FALLBACK_MODEL", OPUS)


# ── Subprocess timeouts ──────────────────────────────────────────────────────

PLAN_STAGE_TIMEOUT = 7200

# Defaults for the explicit CLI timeout options (#1657). The non-phase-
# differentiated agent phases (advise, address-review, ci-driver, follow-up)
# and the options-object fallbacks default to DEFAULT_AGENT_TIMEOUT; per-phase
# timeouts keep their #1642 values via the AGENT_* constants in
# ``hephaestus.constants``.
DEFAULT_AGENT_TIMEOUT: int = 7200
DEFAULT_THROUGHPUT_TIMEOUT: int = 1200
DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT: int = DEFAULT_THROUGHPUT_TIMEOUT
DEFAULT_CI_POLL_MAX_WAIT: int = DEFAULT_THROUGHPUT_TIMEOUT


def _read_int_env(name: str, default: int) -> int:
    """Return ``int(os.environ[name])`` or ``default`` if unset/invalid.

    Thin delegate to :func:`hephaestus.constants.read_timeout_env`, kept for the
    in-module callers; that helper logs and falls back on a non-integer value.
    """
    return read_timeout_env(name, default)


def agent_default_timeout() -> int:
    """Return the generic agent-invocation timeout in seconds (default 7200s).

    This is the phase-agnostic fallback for
    :func:`hephaestus.automation.claude_invoke.invoke_claude_with_session`
    when a caller omits ``timeout``. It must stay generic — no phase's own
    budget (planner/reviewer/implementer/...) may leak in here, since every
    agent type funnels through that single entry point (#1415).
    """
    return _read_int_env("HEPH_AGENT_DEFAULT_TIMEOUT", DEFAULT_AGENT_TIMEOUT)


def planner_claude_timeout() -> int:
    """Timeout for planner agent calls (default 1200s)."""
    return read_timeout_env(
        "HEPH_AGENT_PLAN_TIMEOUT",
        AGENT_PLAN_TIMEOUT,
        legacy_names=("HEPH_PLANNER_AGENT_TIMEOUT",),
    )


def plan_stage_timeout() -> int:
    """Timeout for the outer ``hephaestus-plan-issues`` stage (default 7200s)."""
    return read_timeout_env(
        "HEPH_PLAN_STAGE_TIMEOUT",
        PLAN_STAGE_TIMEOUT,
        legacy_names=("HEPH_PLANNER_AGENT_TIMEOUT",),
    )


def plan_reviewer_claude_timeout() -> int:
    """Timeout for agent calls inside the plan reviewer (default 1200s)."""
    return read_timeout_env(
        "HEPH_AGENT_REVIEW_TIMEOUT",
        AGENT_REVIEW_TIMEOUT,
        legacy_names=("HEPH_PLAN_REVIEWER_AGENT_TIMEOUT",),
    )


def implementer_claude_timeout() -> int:
    """Timeout for the implementer's agent invocation (default 1800s)."""
    return read_timeout_env(
        "HEPH_AGENT_IMPL_TIMEOUT",
        AGENT_IMPL_TIMEOUT,
        legacy_names=("HEPH_IMPLEMENTER_AGENT_TIMEOUT",),
    )


def advise_claude_timeout() -> int:
    """Timeout for advise agent calls (default 7200s)."""
    return _read_int_env("HEPH_ADVISE_AGENT_TIMEOUT", 7200)


def pr_reviewer_claude_timeout() -> int:
    """Timeout for the PR reviewer's agent analysis (default 1200s)."""
    return read_timeout_env(
        "HEPH_AGENT_REVIEW_TIMEOUT",
        AGENT_REVIEW_TIMEOUT,
        legacy_names=("HEPH_PR_REVIEWER_AGENT_TIMEOUT",),
    )


def address_review_claude_timeout() -> int:
    """Timeout for the address-review fix session (default 7200s)."""
    return _read_int_env("HEPH_ADDRESS_REVIEW_AGENT_TIMEOUT", 7200)


def ci_driver_claude_timeout() -> int:
    """Timeout for the CI-driver fix session (default 7200s)."""
    return _read_int_env("HEPH_CI_DRIVER_AGENT_TIMEOUT", 7200)


def learn_claude_timeout() -> int:
    """Timeout for ``/learn`` agent calls (default 1200s)."""
    return read_timeout_env(
        "HEPH_AGENT_LEARN_TIMEOUT",
        AGENT_LEARN_TIMEOUT,
        legacy_names=("HEPH_LEARN_AGENT_TIMEOUT",),
    )


def follow_up_claude_timeout() -> int:
    """Timeout for the follow-up-issue agent session (default 7200s)."""
    return _read_int_env("HEPH_FOLLOW_UP_AGENT_TIMEOUT", 7200)


def git_message_agent_timeout() -> int:
    """Timeout for the lightweight commit/PR message writer (default 1200s)."""
    return _read_int_env("HEPH_GIT_MESSAGE_AGENT_TIMEOUT", DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT)


def ci_poll_max_wait() -> int:
    """Wall-clock seconds for the CI-driver poll loops (default 1200s).

    Bounds the exponential-backoff wait in :mod:`ci_driver` while CI checks
    are still pending. Re-read on each invocation so tests and operators can
    tune it at runtime via ``HEPH_CI_POLL_MAX_WAIT``.
    """
    return _read_int_env("HEPH_CI_POLL_MAX_WAIT", DEFAULT_CI_POLL_MAX_WAIT)


# Re-exported from hephaestus.github.client so the gh-adapter timeout lives
# with the gh adapter; this alias preserves the legacy import path.
from hephaestus.github.client import gh_cli_timeout  # noqa: E402

# ── Session naming ───────────────────────────────────────────────────────────

# Agent identifiers — keep in sync with phase modules.
AGENT_PLANNER = "planner"
AGENT_PLAN_REVIEWER = "plan-reviewer"
AGENT_ADVISE = "advise"
AGENT_LEARNINGS = "learnings"
AGENT_IMPLEMENTER = "implementer"
AGENT_PR_REVIEWER = "pr-reviewer"
AGENT_ADDRESS_REVIEW = "address-review"
AGENT_CI_DRIVER = "ci-driver"
# Lightweight read-only metadata writers. They are deliberately separate from
# implementer/reviewer sessions so commit and PR text generation cannot inherit
# or mutate a code-producing transcript.
AGENT_COMMIT_MESSAGE = "commit-message"
AGENT_PR_MESSAGE = "pr-message"
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
        AGENT_COMMIT_MESSAGE,
        AGENT_PR_MESSAGE,
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
    return re.sub(r"[^A-Za-z0-9._-]", "-", model.strip())


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
        repo: Repository slug without owner (e.g. ``"ProjectScylla"``).
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
            f"or a per-iteration reviewer token (e.g. 'plan-reviewer-r0')"
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


def session_uuid(repo: str, issue: int | str, agent: str, model: str | None = None) -> str:
    """Return the deterministic UUIDv5 session ID for the (repo, issue, agent, model) key."""
    name = session_name(repo, issue, agent, model)
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


__all__ = [
    # Session naming
    "AGENT_ADDRESS_REVIEW",
    "AGENT_ADVISE",
    "AGENT_CI_DRIVER",
    "AGENT_COMMENT_CLASSIFIER",
    "AGENT_COMMIT_MESSAGE",
    "AGENT_IMPLEMENTER",
    # Timeouts
    "AGENT_IMPL_TIMEOUT",
    "AGENT_LEARNINGS",
    "AGENT_LEARN_TIMEOUT",
    "AGENT_PLANNER",
    "AGENT_PLAN_REVIEWER",
    "AGENT_PLAN_TIMEOUT",
    "AGENT_PR_MESSAGE",
    "AGENT_PR_REVIEWER",
    "AGENT_REVIEW_TIMEOUT",
    # Model selection
    "CODEX_ADVISE",
    "DEFAULT_AGENT_TIMEOUT",
    "DEFAULT_CI_POLL_MAX_WAIT",
    "DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT",
    "DEFAULT_THROUGHPUT_TIMEOUT",
    "FABLE",
    "FABLE_5",
    "HAIKU",
    "HAIKU_45",
    "MYTHOS",
    "OPUS",
    "OPUS_47",
    "OPUS_48",
    "PLAN_STAGE_TIMEOUT",
    "SONNET",
    "SONNET_5",
    "SONNET_46",
    "SONNET_50",
    "address_review_claude_timeout",
    "advise_claude_timeout",
    "advise_model",
    "agent_default_timeout",
    "ci_driver_claude_timeout",
    "ci_poll_max_wait",
    "codex_advise_model",
    "current_trunk_githash",
    "fallback_model",
    "follow_up_claude_timeout",
    "gh_cli_timeout",
    "git_message_agent_timeout",
    "git_message_model",
    "implementer_claude_timeout",
    "implementer_model",
    "learn_claude_timeout",
    "learn_model",
    "normalize_claude_model",
    "plan_reviewer_claude_timeout",
    "plan_stage_timeout",
    "planner_claude_timeout",
    "planner_model",
    "pr_reviewer_claude_timeout",
    "read_timeout_env",
    "reviewer_agent",
    "reviewer_model",
    "session_jsonl_path",
    "session_name",
    "session_uuid",
    "short_githash",
]
