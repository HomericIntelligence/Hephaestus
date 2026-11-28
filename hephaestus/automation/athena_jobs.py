"""Provider-neutral Athena skill jobs and learning delivery contracts."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from hephaestus.agents.workspace import (
    WorkspaceBinding,
    WorkspaceKind,
    validate_workspace_binding,
)
from hephaestus.git.source_worktree import (
    SourceWorkspaceManager,
    _PreparationDeadline,
)

__all__ = [
    "AthenaSkillExecutor",
    "AthenaSkillJob",
    "AthenaSkillKind",
    "AthenaSkillRequest",
    "AthenaSkillResult",
    "DeliveryReceipt",
    "LearnDeliveryError",
    "SourceWorkspaceManager",
    "WorkspaceBinding",
    "WorkspaceKind",
    "_PreparationDeadline",
    "athena_workspace_lease",
    "build_athena_skill_request",
    "cleanup_athena_workspace",
    "valid_delivery_receipt",
    "validate_workspace_binding",
]

AthenaSkillKind = Literal["advise", "learn"]


class LearnDeliveryError(RuntimeError):
    """Neutral base for failures that stop learning delivery."""


@runtime_checkable
class DeliveryReceipt(Protocol):
    """Structural shape of a PR-backed learning delivery receipt."""

    @property
    def pr_url(self) -> str:
        """Return the URL of the PR that proves delivery."""

    @property
    def pr_number(self) -> int:
        """Return the PR number that proves delivery."""

    @property
    def commit_sha(self) -> str:
        """Return the commit that was pushed for delivery."""

    @property
    def readback_head_sha(self) -> str:
        """Return the head SHA read back from the PR."""

    @property
    def local_only(self) -> bool:
        """Return True when the receipt has no remote PR."""


@dataclass(frozen=True)
class AthenaSkillRequest:
    """Closed request schema for Athena-equivalent skill execution."""

    kind: AthenaSkillKind | str
    repo: str
    issue: int | str
    agent: str
    model: str
    cwd: Path
    timeout_s: int
    workspace: WorkspaceBinding | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False


@dataclass(frozen=True)
class AthenaSkillResult:
    """Closed result schema returned by host-owned Athena execution."""

    kind: str
    context: str = ""
    receipt: dict[str, Any] = field(default_factory=dict)
    delivery_receipt: dict[str, Any] | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Return True when the result has no failure envelope."""
        return self.error is None


class AthenaSkillExecutor(Protocol):
    """Worker-facing executor for typed Athena skill requests."""

    def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
        """Execute a typed Athena skill request."""

    def cancel(self) -> None:
        """Stop active host subprocess groups."""


def build_athena_skill_request(
    *,
    kind: str,
    repo: str,
    issue: int | str,
    agent: str,
    model: str,
    cwd: Path,
    timeout_s: int,
    workspace: WorkspaceBinding | None = None,
    payload: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> AthenaSkillRequest:
    """Build the provider-neutral request shape used by direct and pipeline paths."""
    return AthenaSkillRequest(
        kind=kind,
        repo=repo,
        issue=issue,
        agent=agent,
        model=model,
        cwd=cwd,
        timeout_s=timeout_s,
        workspace=workspace,
        payload=dict(payload or {}),
        dry_run=dry_run,
    )


@dataclass(frozen=True)
class AthenaSkillJob:
    """Worker job that runs advise/learn through host-owned contracts."""

    request: AthenaSkillRequest
    descr: str = ""

    @property
    def repo(self) -> str:
        """Repository used by worker claim logging."""
        return self.request.repo

    @property
    def issue(self) -> int | str:
        """Issue identifier used by worker claim logging."""
        return self.request.issue

    @property
    def timeout_s(self) -> int:
        """Timeout carried by the underlying request."""
        return self.request.timeout_s


@contextmanager
def athena_workspace_lease(
    job: AthenaSkillJob, *, deadline: _PreparationDeadline
) -> Iterator[Path]:
    """Validate one host skill under its source lease and execution deadline."""
    request = job.request
    binding = request.workspace
    deadline.remaining()
    if binding is None:
        raise RuntimeError("Athena request requires a workspace binding")
    WorkspaceBinding.from_dict(binding.to_dict())
    if binding.cwd != request.cwd:
        raise RuntimeError("Athena request cwd does not match its workspace binding")
    if binding.kind is not WorkspaceKind.SOURCE:
        yield validate_workspace_binding(
            binding,
            allowed_tools="Read,Glob,Grep",
            remaining_timeout=deadline.remaining,
            shutdown=deadline.shutdown,
        )
        return
    if binding.reusable_root is None or binding.repository is None:
        raise RuntimeError("source workspace binding is incomplete")
    manager = SourceWorkspaceManager(
        binding.reusable_root,
        repository=binding.repository,
        base_dir=binding.cwd.parent,
    )
    with manager.acquire(binding, allowed_tools="Read,Glob,Grep", deadline=deadline) as leased:
        yield leased


def cleanup_athena_workspace(job: AthenaSkillJob) -> None:
    """Remove the exact source workspace that one host skill job owned."""
    binding = job.request.workspace
    if binding is None or binding.kind is not WorkspaceKind.SOURCE:
        return
    WorkspaceBinding.from_dict(binding.to_dict())
    if binding.cwd != job.request.cwd:
        raise RuntimeError("Athena request cwd does not match its workspace binding")
    if (
        binding.reusable_root is None
        or binding.repository is None
        or binding.item_number is None
        or binding.lane is None
        or binding.revision is None
    ):
        raise RuntimeError("source workspace binding is incomplete")
    manager = SourceWorkspaceManager(
        binding.reusable_root,
        repository=binding.repository,
        base_dir=binding.cwd.parent,
    )
    manager.cleanup(
        binding.item_number,
        binding.lane,
        expected_revision=binding.revision,
        expected_detached=binding.detached,
        expected_generation=binding.generation,
        missing_ok=True,
        deadline=_PreparationDeadline(time.monotonic() + 45.0, time.monotonic),
    )


def valid_delivery_receipt(
    value: DeliveryReceipt | dict[str, Any] | None,
) -> bool:
    """Return True when a value is a PR-backed learning receipt."""
    if isinstance(value, DeliveryReceipt):
        return bool(
            value.pr_url
            and value.pr_number > 0
            and value.commit_sha
            and value.readback_head_sha == value.commit_sha
            and not value.local_only
        )
    if isinstance(value, dict):
        commit_sha = value.get("commit_sha")
        return bool(
            value.get("pr_url")
            and isinstance(value.get("pr_number"), int)
            and int(value.get("pr_number", 0)) > 0
            and isinstance(commit_sha, str)
            and value.get("readback_head_sha") == commit_sha
            and value.get("local_only") is not True
        )
    return False
