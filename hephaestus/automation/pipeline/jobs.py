"""Frozen job specs and results for the pipeline worker pool.

Jobs are immutable value objects the coordinator freezes and hands to
:class:`~hephaestus.automation.pipeline.worker_pool.WorkerPool`. Prompts are
built IN the worker (several builders fetch diffs / issue bodies via ``gh`` and
must stay off the coordinator thread), so :class:`AgentJob` carries a
``prompt_builder`` callable rather than a pre-rendered string.
"""

from __future__ import annotations

import hashlib
import json
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


def remediation_pretest_result_digest(value: object) -> str:
    """Hash the actual bounded JSON result without adding response fields."""
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("remediation pretest result is invalid") from exc
    if len(encoded) > 1024 * 1024:
        raise ValueError("remediation pretest result exceeds its byte limit")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class DirtyDirectPlanInput:
    """Keep the host-approved plan inputs separate from the workspace claim."""

    revision: int
    plan: str
    review_revision: int
    review: str
    allowed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RemediationPretestInput:
    """Freeze host source, review, and scope pins before remediation starts."""

    repository: str
    issue_number: int
    pr_number: int
    branch: str
    expected_remote_sha: str
    source_receipt_json: str
    source_receipt_sha256: str
    thread_snapshot_json: str
    batch_nonce: str
    allowed_paths: tuple[str, ...]
    approved_scope_sha256: str
    candidate_sequence: int
    expected_previous_record_sha256: str | None

    def __post_init__(self) -> None:
        """Reject mutable, malformed, or inconsistent host inputs."""
        from hephaestus.automation.remediation_prepublication import (
            canonical_source_receipt_json,
            source_receipt_digest,
        )
        from hephaestus.automation.remediation_recovery import RemediationReviewInput
        from hephaestus.automation.source_worktree import SourceWorkspaceReceipt

        if any(
            type(n) is not int or n <= 0
            for n in (self.issue_number, self.pr_number, self.candidate_sequence)
        ):
            raise ValueError("pretest identifiers must be positive integers")
        if (
            not isinstance(self.repository, str)
            or re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", self.repository) is None
        ):
            raise ValueError("pretest repository must be canonical")
        if (
            not isinstance(self.branch, str)
            or not self.branch
            or self.branch.startswith(("-", "/"))
            or ".." in self.branch
            or any(ord(c) < 32 for c in self.branch)
        ):
            raise ValueError("pretest branch is invalid")
        for value, pattern in (
            (self.expected_remote_sha, r"[0-9a-f]{40}(?:[0-9a-f]{24})?"),
            (self.batch_nonce, r"[0-9a-f]{32}"),
            (self.source_receipt_sha256, r"[0-9a-f]{64}"),
            (self.approved_scope_sha256, r"[0-9a-f]{64}"),
        ):
            if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
                raise ValueError("pretest digest or nonce is invalid")
        previous = self.expected_previous_record_sha256
        if (self.candidate_sequence == 1 and previous is not None) or (
            previous is not None
            and (not isinstance(previous, str) or re.fullmatch(r"[0-9a-f]{64}", previous) is None)
        ):
            raise ValueError("pretest predecessor is invalid")
        if (
            not isinstance(self.allowed_paths, tuple)
            or not self.allowed_paths
            or len(set(self.allowed_paths)) != len(self.allowed_paths)
            or any(
                not isinstance(p, str)
                or not p
                or "\0" in p
                or Path(p).is_absolute()
                or Path(p).as_posix() != p
                or any(part in {".", "..", ".git"} for part in Path(p).parts)
                for p in self.allowed_paths
            )
        ):
            raise ValueError("pretest approved paths are invalid")
        if any(
            not isinstance(s, str) or len(s.encode("utf-8")) > 1024 * 1024
            for s in (self.source_receipt_json, self.thread_snapshot_json)
        ):
            raise ValueError("pretest source or threads exceed their bound")
        receipt = SourceWorkspaceReceipt.from_dict(json.loads(self.source_receipt_json))
        if (
            canonical_source_receipt_json(receipt) != self.source_receipt_json
            or source_receipt_digest(receipt) != self.source_receipt_sha256
            or receipt.item_number != self.issue_number
            or receipt.branch != self.branch
            or receipt.revision != self.expected_remote_sha
            or receipt.detached
            or receipt.lane.value != "impl"
            or receipt.repository.casefold() not in {self.repository, self.repository.split("/")[1]}
            or receipt.dirty_claim is not None
            or receipt.ownership_key != f"{receipt.repository_identity}:{self.issue_number}:impl"
        ):
            raise ValueError("pretest source identity is invalid")
        if (
            RemediationReviewInput.canonical_thread_snapshot(json.loads(self.thread_snapshot_json))
            != self.thread_snapshot_json
        ):
            raise ValueError("pretest threads are not canonical")


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
    retryable: bool = True
    dirty_plan: DirtyDirectPlanInput | None = None
    remediation_pretest_nonce: str | None = None
    remediation_pretest_input: RemediationPretestInput | None = None

    def __post_init__(self) -> None:
        """Validate an optional operation-wide monotonic deadline."""
        if (self.remediation_pretest_nonce is None) != (self.remediation_pretest_input is None):
            raise ValueError("pretest nonce and input must be supplied together")
        if self.remediation_pretest_nonce is not None and (
            not isinstance(self.remediation_pretest_nonce, str)
            or re.fullmatch(r"[0-9a-f]{32}", self.remediation_pretest_nonce) is None
            or not isinstance(self.remediation_pretest_input, RemediationPretestInput)
        ):
            raise ValueError("pretest job identity is invalid")
        if self.deadline_s is not None and (
            isinstance(self.deadline_s, bool)
            or not isinstance(self.deadline_s, (int, float))
            or not math.isfinite(self.deadline_s)
            or self.deadline_s <= 0
        ):
            raise ValueError("deadline_s must be a finite positive monotonic time")


def validate_job_workspace(job: AgentJob, *, dirty_permit: object | None = None) -> Path:
    """Resolve and fail-closed validate an agent job's execution directory."""
    tools = job.allowed_tools
    if tools is None and job.sandbox in {"read-only", "workspace-write"}:
        tools = "Read,Glob,Grep,Write,Edit,Bash"
    if job.workspace is not None:
        canonical = validate_workspace_binding(
            job.workspace, allowed_tools=tools or "", dirty_permit=dirty_permit
        )
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
