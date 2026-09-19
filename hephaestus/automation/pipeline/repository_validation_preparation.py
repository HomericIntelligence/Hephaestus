"""Define closed requests and results for validation preparation workers."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding, WorkspaceKind

from .repository_validation import (
    RepositoryValidationExecution,
    RepositoryValidationInvocation,
    RepositoryValidationPlan,
    validate_repository_validation_execution,
    validate_repository_validation_invocation,
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _deadline(value: float) -> None:
    _require(
        type(value) in {int, float} and math.isfinite(value) and value > 0,
        "The preparation deadline is invalid.",
    )


def _hex(value: object, length: int) -> bool:
    return type(value) is str and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


@dataclass(frozen=True, slots=True)
class RepositoryValidationSourceRequest:
    """Bind source inspection to one complete review checkout manifest."""

    repository: str
    issue_number: int | None
    pr_number: int
    workspace: WorkspaceBinding
    reviewed_head: str
    reviewed_base: str
    diff_base_sha: str
    changes: tuple[tuple[str, str], ...]
    generation: int
    request_nonce: str
    deadline_s: float

    def __post_init__(self) -> None:
        """Reject incomplete source identity and mutable change records."""
        _deadline(self.deadline_s)
        _require(self.repository == "LLM360/comet", "The source repository is unsupported.")
        _require(type(self.pr_number) is int and self.pr_number > 0, "The PR number is invalid.")
        _require(
            self.issue_number is None or (type(self.issue_number) is int and self.issue_number > 0),
            "The issue number is invalid.",
        )
        _require(
            all(
                _hex(value, 40)
                for value in (self.reviewed_head, self.reviewed_base, self.diff_base_sha)
            )
            and type(self.generation) is int
            and self.generation > 0
            and _hex(self.request_nonce, 32),
            "The source attempt identity is invalid.",
        )
        binding = self.workspace
        _require(type(binding) is WorkspaceBinding, "The source binding type is invalid.")
        _require(
            binding.kind is WorkspaceKind.SOURCE
            and binding.lane is SourceLane.REVIEW
            and binding.detached is True
            and type(binding.schema_version) is int
            and binding.schema_version == 1
            and binding.dirty_claim is None
            and type(binding.repository) is str
            and binding.repository.casefold() == self.repository.casefold()
            and binding.revision == self.reviewed_head
            and type(binding.item_number) is int
            and binding.item_number == (self.issue_number or self.pr_number)
            and binding.cwd.is_absolute()
            and binding.reusable_root is not None
            and binding.reusable_root.is_absolute()
            and type(binding.ownership_key) is str
            and bool(binding.ownership_key)
            and type(binding.generation) is int
            and binding.generation >= 0,
            "The source binding does not match the detached review.",
        )
        _require(
            type(self.changes) is tuple and len(self.changes) <= 4096,
            "The change inventory is invalid.",
        )
        paths: set[str] = set()
        for record in self.changes:
            _require(type(record) is tuple and len(record) == 2, "The change record is invalid.")
            status, path = record
            _require(
                type(status) is str
                and status in {"A", "M", "D"}
                and type(path) is str
                and bool(path)
                and len(path.encode("utf-8")) <= 4096
                and not path.startswith("/")
                and "\\" not in path
                and "\0" not in path
                and all(part not in {"", ".", ".."} for part in path.split("/"))
                and path not in paths,
                "The change path is invalid or repeated.",
            )
            paths.add(path)


@dataclass(frozen=True, slots=True)
class RepositoryValidationSourceRead:
    """Return the owned source plan or a closed failure code."""

    request: RepositoryValidationSourceRequest
    plan: RepositoryValidationPlan | None = None
    failure: str = ""

    def __post_init__(self) -> None:
        """Check the result against every source request field."""
        _require(
            type(self.request) is RepositoryValidationSourceRequest,
            "The source request type is invalid.",
        )
        replace(self.request)
        _require(self.failure in {"", "source_preparation_failed"}, "Unknown source failure.")
        _require((self.plan is None) == bool(self.failure), "The source result is incomplete.")
        if self.plan is None:
            return
        plan, request = self.plan, self.request
        _require(
            type(plan) is RepositoryValidationPlan and replace(plan) == plan,
            "The source plan changed.",
        )
        _require(
            (
                plan.repository,
                plan.issue_number,
                plan.pr_number,
                plan.source_workspace,
                plan.reviewed_head,
                plan.reviewed_base,
                plan.diff_base_sha,
                plan.changes,
            )
            == (
                request.repository,
                request.issue_number,
                request.pr_number,
                request.workspace,
                request.reviewed_head,
                request.reviewed_base,
                request.diff_base_sha,
                request.changes,
            ),
            "The source plan does not match the checkout manifest.",
        )


@dataclass(frozen=True, slots=True)
class RepositoryValidationRuntimeRequest:
    """Request runtime admission without permission to execute a check."""

    invocation: RepositoryValidationInvocation
    deadline_s: float

    def __post_init__(self) -> None:
        """Require one local check and a bounded operation deadline."""
        _deadline(self.deadline_s)
        validate_repository_validation_invocation(self.invocation)
        _require(
            self.invocation.evidence_kind == "local"
            and len(self.invocation.check_ids) == 1
            and self.invocation.plan.execution_allowed,
            "Runtime admission requires one executable local check.",
        )


@dataclass(frozen=True, slots=True)
class RepositoryValidationRuntimeRead:
    """Return admitted execution metadata without validation evidence."""

    request: RepositoryValidationRuntimeRequest
    execution: RepositoryValidationExecution | None = None
    failure: str = ""

    def __post_init__(self) -> None:
        """Bind runtime metadata to the reserved local invocation."""
        _require(
            type(self.request) is RepositoryValidationRuntimeRequest,
            "The runtime request type is invalid.",
        )
        replace(self.request)
        _require(self.failure in {"", "runtime_preparation_failed"}, "Unknown runtime failure.")
        _require(
            (self.execution is None) == bool(self.failure), "The runtime result is incomplete."
        )
        if self.execution is None:
            return
        validate_repository_validation_execution(self.execution)
        invocation = self.request.invocation
        _require(
            (
                self.execution.plan,
                self.execution.check_id,
                self.execution.attempt_generation,
                self.execution.request_nonce,
            )
            == (
                invocation.plan,
                invocation.check_ids[0],
                invocation.generation,
                invocation.request_nonce,
            ),
            "The runtime result does not match its invocation.",
        )
