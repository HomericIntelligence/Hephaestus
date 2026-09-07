"""Frozen job specs and results for the pipeline worker pool.

Jobs are immutable value objects the coordinator freezes and hands to
:class:`~hephaestus.automation.pipeline.worker_pool.WorkerPool`. Prompts are
built IN the worker (several builders fetch diffs / issue bodies via ``gh`` and
must stay off the coordinator thread), so :class:`AgentJob` carries a
``prompt_builder`` callable rather than a pre-rendered string.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hephaestus.agents.execution_policy import ExecutionRequest
from hephaestus.agents.pi_session import AgentSessionBinding
from hephaestus.agents.workspace import WorkspaceBinding, validate_workspace_binding

from .git_jobs import GIT_OPS, WORKTREE_MATERIALIZED_KEY, GitJob
from .job_results import JobHandle, JobResult

if TYPE_CHECKING:
    from hephaestus.agents.codex_isolation import CodexIsolationRequestV1

__all__ = [
    "GIT_OPS",
    "WORKTREE_MATERIALIZED_KEY",
    "AgentJob",
    "BuildTestJob",
    "CompactJob",
    "GitJob",
    "JobHandle",
    "JobResult",
    "JobWorkspaceError",
    "validate_job_workspace",
]


class JobWorkspaceError(RuntimeError):
    """Raised when a job attempts to use an unbound source checkout."""


@dataclass(frozen=True)
class AgentJob:
    """Job to invoke an agent (Claude or other)."""

    repo: str
    issue: int | str
    agent: str
    model: str
    prompt_builder: Callable[..., str]
    cwd: Path
    timeout_s: int
    workspace: WorkspaceBinding | None = None
    disable_pi_automation: bool = False
    auth_status_timeout: int = 10
    pi_isolation_adapter: str | None = None
    pi_dir: Path | None = None
    session_selection_error: str | None = None
    codex_isolation_adapter: str | None = None
    codex_isolation_deployment_lock: Path | None = None
    codex_isolation_deployment_lock_sha256: str | None = None
    codex_isolation_request: CodexIsolationRequestV1 | None = None
    fallback_model: str | None = None
    plugin_skills_dir: Path | None = None
    session_agent: str = ""
    # Stable cycle-scoped identity.  Unlike ``session_agent`` this must not
    # be shared by separate issues or explicit planning cycles.
    session_key: str = ""
    # Durable cycles must not adopt a transcript from a previous start.
    require_new_session: bool = False
    # Direct-runner providers return an opaque session id.  The coordinator
    # stores it on the WorkItem and supplies it here on subsequent turns so
    # review/implementation context survives across loop iterations.
    resume_session_id: str | None = None
    resume_selection: tuple[str, str] | None = None
    prompt_kwargs: dict[str, Any] = field(default_factory=dict)
    output_format: str = "text"
    # Examples: review_audit.parse_review_audit or a label-native plan parser.
    # The deprecated textual verdict parser must not be attached here.
    parse: Callable[[str], Any] | None = None
    # Existing agent jobs retain the established write-capable default; callers
    # that only inspect repository state request ``read-only`` explicitly.
    sandbox: str = "workspace-write"
    # Optional Claude tool scope for a read-only job.  Most reviewers use the
    # conservative default assigned by WorkerPool; the full PR-review skill
    # declares the additional read-only helper capabilities it needs.
    allowed_tools: str | None = None
    # Pi uses this immutable operation request instead of inheriting the
    # compatibility ``sandbox``/``allowed_tools`` inputs used by Claude/Codex.
    execution_request: ExecutionRequest | None = None
    resume_binding: AgentSessionBinding | None = None
    # Invoked immediately after the provider returns its identity and before
    # output parsing, closing the restart window for durable conversations.
    session_checkpoint: Callable[[str, AgentSessionBinding | None], None] | None = None
    descr: str = ""
    deadline_s: float | None = None

    def __post_init__(self) -> None:
        """Validate an optional operation-wide monotonic deadline."""
        if self.deadline_s is not None and (
            isinstance(self.deadline_s, bool)
            or not isinstance(self.deadline_s, (int, float))
            or not math.isfinite(self.deadline_s)
            or self.deadline_s <= 0
        ):
            raise ValueError("deadline_s must be a finite positive monotonic time")


def validate_job_workspace(job: AgentJob) -> Path:
    """Resolve and fail-closed validate an agent job's execution directory."""
    tools = job.allowed_tools
    if tools is None and job.sandbox in {"read-only", "workspace-write"}:
        tools = "Read,Glob,Grep,Write,Edit,Bash"
    if job.workspace is not None:
        canonical = validate_workspace_binding(job.workspace, allowed_tools=tools or "")
        if canonical != job.cwd.resolve(strict=True):
            raise JobWorkspaceError("job cwd does not match its workspace binding")
        return canonical
    canonical = job.cwd.resolve(strict=True)
    source_capable = bool(
        {"Read", "Glob", "Grep", "Write", "Edit", "Bash"}
        & {value.strip() for value in (tools or "").split(",") if value.strip()}
    )
    # A primary checkout has a .git directory. Linked worktrees have a .git
    # file, so legacy isolated callers remain compatible while every ambient
    # reusable-root invocation fails before provider resolution.
    if source_capable and (canonical / ".git").is_dir():
        raise JobWorkspaceError("source-reading agent cannot use the reusable repository root")
    return job.cwd


@dataclass(frozen=True)
class BuildTestJob:
    """Job to run build/test commands.

    Security: ``argv`` MUST NOT carry untrusted (issue-body-derived) strings.
    It is executed directly as a subprocess argument vector, so only the
    coordinator may construct these jobs, from vetted command templates.
    """

    repo: str
    cwd: Path
    argv: tuple[str, ...]  # e.g. ("uv", "run", "pytest", "tests", "-q")
    timeout_s: int
    # A host-verification command runs from a disposable source snapshot
    # generated from this checkout-proven immutable commit, never directly
    # from the reviewer worktree.
    expected_head_sha: str = ""
    immutable_source: bool = False
    # A non-None value tells the worker to run ``argv`` through the host-owned
    # descriptor snapshot launcher. An empty value keeps fallback untrusted.
    verified_runner_source_revision: str | None = None
    descr: str = ""

    def __post_init__(self) -> None:
        """Normalize argv to a tuple so the job is deeply immutable/hashable."""
        if not isinstance(self.argv, tuple):
            # frozen dataclass: bypass the frozen __setattr__ for normalization
            object.__setattr__(self, "argv", tuple(self.argv))


@dataclass(frozen=True)
class CompactJob:
    """Best-effort compaction of one resumable agent session.

    ``CompactJob`` is deliberately distinct from :class:`AgentJob`: it sends
    ``/compact`` to an existing session and never runs an implementation or
    review prompt.  Claude resolves its deterministic session id from the
    logical ``session_agent``; direct runtimes receive the persisted id.
    """

    repo: str
    issue: int | str
    agent: str
    session_agent: str
    model: str
    cwd: Path
    timeout_s: int
    disable_pi_automation: bool = False
    auth_status_timeout: int = 10
    pi_isolation_adapter: str | None = None
    pi_dir: Path | None = None
    session_selection_error: str | None = None
    session_id: str | None = None
    # Direct-provider compaction only sends ``/compact`` and never needs write
    # access.  Keep the policy explicit so it cannot inherit user defaults.
    sandbox: str = "read-only"
    execution_request: ExecutionRequest | None = None
    session_binding: AgentSessionBinding | None = None
    descr: str = "compact_session"


def _valid_writer_publication_facts(value: object) -> bool:
    """Accept only closed ordinary-writer publication facts."""
    if not isinstance(value, dict) or set(value) != {
        "publication_state",
        "head_sha",
        "baseline_remote_sha",
        "observed_remote_sha",
        "pushed",
        "refresh_phase",
    }:
        return False
    state = value["publication_state"]
    if not isinstance(state, str) or state not in {
        "published",
        "remote_at_source",
        "remote_changed",
        "remote_unchanged",
        "probe_failed",
    }:
        return False
    head, baseline, observed = (
        value[key] for key in ("head_sha", "baseline_remote_sha", "observed_remote_sha")
    )
    for sha in (head, baseline, observed):
        if sha is not None and (
            not isinstance(sha, str) or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha) is None
        ):
            return False
    if (
        head is None
        or value["refresh_phase"] not in (None, "publish")
        or value["pushed"] is not (state in {"published", "remote_at_source"})
    ):
        return False
    if state in {"published", "remote_at_source"}:
        return bool(observed == head)
    if state == "remote_changed":
        return observed is not None and observed not in (head, baseline)
    if state == "remote_unchanged":
        return bool(observed == baseline and observed != head)
    return observed is None


def _writer_publication_matches_refresh(value: object, refresh: object) -> bool:
    """Bind closed publication facts to the exact pending refresh."""
    if not _valid_writer_publication_facts(value) or not isinstance(value, dict):
        return False
    if refresh is None:
        return value["refresh_phase"] is None
    if not isinstance(refresh, dict) or set(refresh) != {
        "phase",
        "source_sha",
        "expected_remote_sha",
    }:
        return False
    for key in ("source_sha", "expected_remote_sha"):
        sha = refresh[key]
        if not isinstance(sha, str) or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha) is None:
            return False
    return bool(
        refresh["phase"] in ("rebase", "publish")
        and value["refresh_phase"] == "publish"
        and value["baseline_remote_sha"] == refresh["expected_remote_sha"]
        and (refresh["phase"] != "publish" or value["head_sha"] == refresh["source_sha"])
    )
