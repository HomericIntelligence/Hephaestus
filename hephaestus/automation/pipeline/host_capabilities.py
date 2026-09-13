"""Explicit, fail-closed host capability contracts for pipeline workers."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .diagnostics import redact_diagnostic_text

_DIAGNOSTIC_MAX = 4_000
_FULL_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_REQUEST_ID = re.compile(r"[0-9a-f]{32}")
QUOTA_AVAILABLE_TOKEN = "host_verification_quota_available"  # noqa: S105
QUOTA_CREATE_FAILED_TOKEN = "host_verification_quota_create_failed"  # noqa: S105
QUOTA_UNAVAILABLE_TOKEN = "host_verification_quota_unavailable"  # noqa: S105


@dataclass(frozen=True, slots=True)
class CapabilityRequestTarget:
    """Inputs that the stage knows before a worker uses a host capability."""

    repository: str
    issue_number: int
    pr_number: int
    repository_root: Path
    checkout_path: Path
    expected_head_sha: str
    phase: str
    purpose: str
    request_id: str
    capability: str = "quota"

    def __post_init__(self) -> None:
        """Reject malformed targets before an external command starts."""
        if (
            not re.fullmatch(r"[a-z0-9_.-]+(?:/[a-z0-9_.-]+)?", self.repository)
            or type(self.issue_number) is not int
            or self.issue_number <= 0
            or type(self.pr_number) is not int
            or self.pr_number <= 0
            or _FULL_SHA.fullmatch(self.expected_head_sha) is None
            or _REQUEST_ID.fullmatch(self.request_id) is None
            or self.capability != "quota"
            or self.phase not in {"pr_review", "rebase"}
            or self.purpose not in {"scratch", "pi_smoke_logs"}
        ):
            raise ValueError("capability request target is invalid")


@dataclass(frozen=True, slots=True)
class CapabilityReceiptTarget:
    """Worker-validated host facts that bind a capability receipt."""

    request: CapabilityRequestTarget
    canonical_repository_root: Path
    root_device: int
    execution_boundary_id: str
    source_head_sha: str

    def __post_init__(self) -> None:
        """Reject a receipt target that does not match its request."""
        if (
            self.canonical_repository_root != self.request.repository_root.resolve()
            or type(self.root_device) is not int
            or not self.execution_boundary_id
            or _FULL_SHA.fullmatch(self.source_head_sha) is None
            or self.source_head_sha != self.request.expected_head_sha
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

    def __post_init__(self) -> None:
        """Keep durable receipt diagnostics safe and bounded."""
        if not _REQUEST_ID.fullmatch(self.receipt_id):
            raise ValueError("capability receipt id is invalid")
        if self.failed_step not in {None, "backend", "create", "attach", "detach"}:
            raise ValueError("capability receipt step is invalid")
        if self.purpose not in {"scratch", "pi_smoke_logs"}:
            raise ValueError("capability receipt purpose is invalid")
        for value in (self.stdout_tail, self.stderr_tail, self.operating_system_error):
            if not isinstance(value, str) or len(value) > _DIAGNOSTIC_MAX:
                raise ValueError("capability receipt diagnostic is invalid")


class QuotaBackend(Protocol):
    """Provide the reviewed quota boundary for immutable host verification."""

    def preflight(self, target: CapabilityRequestTarget) -> HostCapabilityReceipt:
        """Return the quota availability for one stage target."""


CommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]


def _tail(value: bytes | str | None) -> str:
    """Return a redacted bounded diagnostic tail."""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    return redact_diagnostic_text(text)[-_DIAGNOSTIC_MAX:]


class HdiutilQuotaBackend:
    """Use hdiutil only when it can create a disposable quota image."""

    backend_id = "hdiutil-v1"

    def __init__(self, command_runner: CommandRunner | None = None) -> None:
        """Initialize the backend with an optional controlled command seam."""
        self._command_runner = command_runner or subprocess.run

    def preflight(self, target: CapabilityRequestTarget) -> HostCapabilityReceipt:
        """Probe quota-image creation and return a typed receipt."""
        receipt_id = uuid.uuid4().hex
        simulated = self._command_runner is not subprocess.run
        binary = Path("/usr/bin/hdiutil")
        if not simulated and sys.platform != "darwin":
            return self._failure(
                target, receipt_id, "host_verification_quota_backend_not_applicable", "backend"
            )
        if not simulated and (not binary.is_file() or not os.access(binary, os.X_OK)):
            return self._failure(
                target, receipt_id, "host_verification_quota_unavailable", "backend"
            )
        root = target.repository_root / "build" / ".host-verification" / target.request_id
        image = root / "preflight.dmg"
        try:
            root.mkdir(parents=True, mode=0o700, exist_ok=False)
            result = self._command_runner(
                (str(binary), "create", "-size", "512m", "-fs", "HFS+", str(image)),
                capture_output=True,
                check=False,
                timeout=30,
                env={"PATH": os.defpath},
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return self._failure(
                target, receipt_id, QUOTA_CREATE_FAILED_TOKEN, "create", error=error
            )
        if result.returncode != 0:
            return self._failure(
                target,
                receipt_id,
                QUOTA_CREATE_FAILED_TOKEN,
                "create",
                result=result,
            )
        return HostCapabilityReceipt(
            available=True,
            token=QUOTA_AVAILABLE_TOKEN,
            failed_step=None,
            purpose=target.purpose,
            receipt_id=receipt_id,
            stdout_tail=_tail(result.stdout),
            stderr_tail=_tail(result.stderr),
            return_code=result.returncode,
        )

    @staticmethod
    def _failure(
        target: CapabilityRequestTarget,
        receipt_id: str,
        token: str,
        step: str,
        *,
        result: subprocess.CompletedProcess[bytes] | None = None,
        error: BaseException | None = None,
    ) -> HostCapabilityReceipt:
        """Create one bounded typed failure receipt."""
        return HostCapabilityReceipt(
            available=False,
            token=token,
            failed_step=step,
            purpose=target.purpose,
            receipt_id=receipt_id,
            stdout_tail=_tail(None if result is None else result.stdout),
            stderr_tail=_tail(None if result is None else result.stderr),
            return_code=None if result is None else result.returncode,
            operating_system_error=_tail(str(error)) if error is not None else "",
            exception_type="" if error is None else type(error).__name__,
        )


@dataclass(frozen=True, slots=True)
class WorkerCapabilities:
    """Explicit host capabilities available to one worker-pool instance."""

    quota_backend: QuotaBackend | None
    execution_boundary_id: str

    def __post_init__(self) -> None:
        """Reject an empty execution-boundary identity."""
        if not self.execution_boundary_id:
            raise ValueError("execution boundary id is required")


__all__ = [
    "QUOTA_AVAILABLE_TOKEN",
    "QUOTA_CREATE_FAILED_TOKEN",
    "QUOTA_UNAVAILABLE_TOKEN",
    "CapabilityReceiptTarget",
    "CapabilityRequestTarget",
    "HdiutilQuotaBackend",
    "HostCapabilityReceipt",
    "QuotaBackend",
    "WorkerCapabilities",
]
