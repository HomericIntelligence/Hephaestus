"""Explicit, fail-closed host capability contracts for pipeline workers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding, WorkspaceKind

_DIAGNOSTIC_MAX = 4_000
_FULL_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_REQUEST_ID = re.compile(r"[0-9a-f]{32}")
QUOTA_AVAILABLE_TOKEN = "host_verification_quota_available"  # noqa: S105
QUOTA_CREATE_FAILED_TOKEN = "host_verification_quota_create_failed"  # noqa: S105
QUOTA_UNAVAILABLE_TOKEN = "host_verification_quota_unavailable"  # noqa: S105


def _absolute_path(value: object) -> bool:
    """Check path data without access to the filesystem."""
    return isinstance(value, Path) and value.is_absolute() and ".." not in value.parts


def _bounded_text(value: object, limit: int = _DIAGNOSTIC_MAX) -> bool:
    """Accept only bounded text fields in the closed result schema."""
    return type(value) is str and len(value) <= limit


@dataclass(frozen=True, slots=True)
class CapabilityRequestTarget:
    """Inputs that the stage knows before a worker uses a host capability."""

    repository: str
    issue_number: int
    pr_number: int | None
    repository_root: Path
    checkout_path: Path
    expected_head_sha: str
    phase: str
    purpose: str
    request_id: str
    capability: str = "quota"
    workspace: WorkspaceBinding = field(kw_only=True)
    generation: int = field(kw_only=True)

    def __post_init__(self) -> None:
        """Reject malformed targets before an external command starts."""
        if (
            type(self.repository) is not str
            or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?", self.repository)
            or type(self.issue_number) is not int
            or self.issue_number <= 0
            or not (
                (type(self.pr_number) is int and self.pr_number > 0)
                or (self.phase == "rebase" and self.pr_number is None)
            )
            or not _absolute_path(self.repository_root)
            or not _absolute_path(self.checkout_path)
            or type(self.expected_head_sha) is not str
            or _FULL_SHA.fullmatch(self.expected_head_sha) is None
            or type(self.request_id) is not str
            or _REQUEST_ID.fullmatch(self.request_id) is None
            or self.capability != "quota"
            or self.phase not in {"pr_review", "rebase"}
            or self.purpose not in {"scratch", "pi_smoke_logs"}
        ):
            raise ValueError("capability request target is invalid")
        if type(self.generation) is not int or self.generation <= 0:
            raise ValueError("The capability attempt generation is invalid.")
        _validate_workspace(self)


def _validate_workspace(target: CapabilityRequestTarget) -> None:
    """Require complete source ownership without filesystem access."""
    workspace = target.workspace
    if type(workspace) is not WorkspaceBinding:
        raise ValueError("The capability workspace type is invalid.")
    lane = SourceLane.REVIEW if target.phase == "pr_review" else SourceLane.IMPLEMENTATION
    repositories = {target.repository.casefold()}
    if target.phase == "rebase":
        repositories.add(target.repository.rsplit("/", 1)[-1].casefold())
    if (
        workspace.kind is not WorkspaceKind.SOURCE
        or workspace.lane is not lane
        or workspace.detached is not (target.phase == "pr_review")
        or type(workspace.schema_version) is not int
        or workspace.schema_version != 1
        or workspace.dirty_claim is not None
    ):
        raise ValueError("The capability workspace lane is invalid.")
    if (
        type(workspace.repository) is not str
        or workspace.repository.casefold() not in repositories
        or workspace.revision != target.expected_head_sha
        or type(workspace.item_number) is not int
        or workspace.item_number != target.issue_number
        or workspace.cwd != target.checkout_path
        or workspace.reusable_root != target.repository_root
    ):
        raise ValueError("The capability source binding does not match its request.")
    if (
        not _bounded_text(workspace.ownership_key, 256)
        or not workspace.ownership_key
        or type(workspace.generation) is not int
        or workspace.generation < 0
    ):
        raise ValueError("The capability source ownership is incomplete.")


@dataclass(frozen=True, slots=True)
class CapabilityReceiptTarget:
    """Worker-validated host facts that bind a capability receipt."""

    request: CapabilityRequestTarget
    canonical_repository_root: Path
    root_device: int
    execution_boundary_id: str
    source_head_sha: str
    backend: str = field(kw_only=True)

    def __post_init__(self) -> None:
        """Reject a receipt target that does not match its request."""
        if type(self.request) is not CapabilityRequestTarget:
            raise ValueError("The capability request type is invalid.")
        replace(self.request)
        if (
            not _absolute_path(self.canonical_repository_root)
            or self.canonical_repository_root != self.request.repository_root
            or type(self.root_device) is not int
            or self.root_device < 0
            or not _bounded_text(self.execution_boundary_id, 256)
            or not self.execution_boundary_id
            or self.backend not in {"hdiutil-v1", "pyxis-v1", "unavailable"}
            or type(self.source_head_sha) is not str
            or _FULL_SHA.fullmatch(self.source_head_sha) is None
            or (
                self.request.phase == "pr_review"
                and self.source_head_sha != self.request.expected_head_sha
            )
        ):
            raise ValueError("capability receipt target is invalid")


@dataclass(frozen=True, slots=True)
class HostCapabilityReceipt:
    """Bounded result from a host capability probe or volume operation."""

    available: bool
    token: str
    failed_step: str | None
    purpose: str
    receipt_id: str
    stdout_tail: str = ""
    stderr_tail: str = ""
    return_code: int | None = None
    operating_system_error: str = ""
    exception_type: str = ""
    cached: bool = False
    probe_receipt_id: str | None = None
    retained_root: str = ""
    target: CapabilityReceiptTarget = field(kw_only=True)
    cleanup_state: str = field(kw_only=True)
    persistence_error: str = ""
    persistence_exception_type: str = ""

    def __post_init__(self) -> None:
        """Keep durable receipt diagnostics safe and bounded."""
        if type(self.target) is not CapabilityReceiptTarget:
            raise ValueError("The capability receipt target is invalid.")
        replace(self.target)
        if self.purpose != self.target.request.purpose:
            raise ValueError("The capability receipt purpose does not match its target.")
        if (
            type(self.cleanup_state) is not str
            or self.cleanup_state not in {"not_started", "complete", "retained"}
            or (self.available and self.cleanup_state != "complete")
            or (self.cleanup_state == "retained") != bool(self.retained_root)
        ):
            raise ValueError("The capability cleanup state is invalid.")
        if type(self.receipt_id) is not str or not _REQUEST_ID.fullmatch(self.receipt_id):
            raise ValueError("capability receipt id is invalid")
        steps = {
            QUOTA_AVAILABLE_TOKEN: None,
            QUOTA_UNAVAILABLE_TOKEN: "backend",
            "host_verification_quota_backend_not_applicable": "backend",
            QUOTA_CREATE_FAILED_TOKEN: "create",
            "host_verification_quota_attach_failed": "attach",
            "host_verification_quota_detach_failed": "detach",
            "host_verification_quota_receipt_storage_failed": "storage",
        }
        if (
            type(self.available) is not bool
            or type(self.token) is not str
            or self.token not in steps
            or self.failed_step != steps[self.token]
            or self.available != (self.failed_step is None)
            or (self.return_code is not None and type(self.return_code) is not int)
            or type(self.cached) is not bool
            or self.cached != (self.probe_receipt_id is not None)
            or (
                self.probe_receipt_id is not None
                and (
                    type(self.probe_receipt_id) is not str
                    or _REQUEST_ID.fullmatch(self.probe_receipt_id) is None
                )
            )
        ):
            raise ValueError("The capability receipt outcome is invalid.")
        if self.purpose not in {"scratch", "pi_smoke_logs"}:
            raise ValueError("capability receipt purpose is invalid")
        for value in (
            self.stdout_tail,
            self.stderr_tail,
            self.operating_system_error,
            self.exception_type,
            self.retained_root,
            self.persistence_error,
            self.persistence_exception_type,
        ):
            if not _bounded_text(value):
                raise ValueError("capability receipt diagnostic is invalid")


@dataclass(frozen=True, slots=True)
class HostCapabilityRead:
    """Bind one worker callback to its submitted capability request."""

    request: CapabilityRequestTarget
    receipt: HostCapabilityReceipt | None = None
    failure: str = ""

    def __post_init__(self) -> None:
        """Reject an unowned or contradictory worker result."""
        if type(self.request) is not CapabilityRequestTarget:
            raise ValueError("The capability callback request is invalid.")
        replace(self.request)
        if self.receipt is None:
            if self.failure not in {"source_unavailable", "provider_result_invalid"}:
                raise ValueError("The capability callback failure is invalid.")
        elif (
            type(self.receipt) is not HostCapabilityReceipt
            or self.receipt.target.request != self.request
            or self.failure not in {"", "operation_deadline", "operation_cancelled"}
        ):
            raise ValueError("The capability callback receipt does not match its request.")
        else:
            replace(self.receipt)


class CapabilityDeadline(Protocol):
    """Supply one remaining operation budget and its cancellation check."""

    def remaining(self) -> float:
        """Return remaining seconds or raise when the operation must stop."""


class QuotaBackend(Protocol):
    """Provide the reviewed quota boundary for immutable host verification."""

    backend_id: str

    def preflight(
        self, target: CapabilityReceiptTarget, *, deadline: CapabilityDeadline
    ) -> HostCapabilityReceipt:
        """Return the quota availability for one stage target."""


class SigningConfigurationError(RuntimeError):
    """Indicate that the configured provider cannot authorize signed Git work."""


class SigningProvider(Protocol):
    """Supply a validated signing environment without an unsigned fallback."""

    def environment(
        self, cwd: Path, *, timeout: int, private_metadata: bool = False
    ) -> dict[str, str]:
        """Return the controlled environment or raise SigningConfigurationError."""


@dataclass(frozen=True, slots=True)
class WorkerCapabilities:
    """Explicit host capabilities available to one worker-pool instance."""

    quota_backend: QuotaBackend | None
    execution_boundary_id: str
    signing_provider: SigningProvider | None = None

    def __post_init__(self) -> None:
        """Reject an empty execution-boundary identity."""
        if not self.execution_boundary_id:
            raise ValueError("execution boundary id is required")


__all__ = [
    "QUOTA_AVAILABLE_TOKEN",
    "QUOTA_CREATE_FAILED_TOKEN",
    "QUOTA_UNAVAILABLE_TOKEN",
    "CapabilityDeadline",
    "CapabilityReceiptTarget",
    "CapabilityRequestTarget",
    "HostCapabilityRead",
    "HostCapabilityReceipt",
    "QuotaBackend",
    "SigningConfigurationError",
    "SigningProvider",
    "WorkerCapabilities",
]
