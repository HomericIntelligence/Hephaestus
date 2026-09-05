"""Closed data contracts for Linux host verification.

This module owns only deterministic byte digests and JSON-shaped lease data.
It does not access the filesystem, submit work, or execute a verifier. Callers
must perform those separate trust-boundary checks before they use a request.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

SCHEMA_VERSION = 1
"""The only accepted Linux host-verification request and receipt schema."""

MAX_TIMEOUT_SECONDS = 86_400
"""The largest lease timeout that this schema accepts."""

MAX_DIAGNOSTIC_CHARS = 16_384
"""The largest bounded stdout or stderr tail in a receipt."""

_CANONICAL_DIGEST_PREFIX = b"hephaestus/linux-host-verification/canonical-digest/v1\x00"
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_HEAD_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9_]{0,127}")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_HOSTNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,253}")
_JOB_ID_RE = re.compile(r"[0-9]+(?:_[0-9]+)?(?:;[A-Za-z0-9][A-Za-z0-9._-]*)?")

_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "command_id",
        "expected_head_sha",
        "timeout_seconds",
        "submit_hostname",
        "source_archive_path",
        "source_extract_path",
        "source_archive_sha256",
        "git_metadata_archive_path",
        "git_metadata_extract_path",
        "git_metadata_archive_sha256",
        "image_path",
        "image_sha256",
        "image_manifest_sha256",
        "contract_root",
        "contract_sha256",
        "allocation_driver_sha256",
        "srun_path",
        "scratch_path",
        "stdout_path",
        "stderr_path",
        "receipt_path",
    }
)

_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "command_id",
        "expected_head_sha",
        "source_archive_sha256",
        "git_metadata_archive_sha256",
        "image_sha256",
        "image_manifest_sha256",
        "contract_sha256",
        "allocation_driver_sha256",
        "backend",
        "allocation_job_id",
        "submit_hostname",
        "allocation_hostname",
        "return_code",
        "timed_out",
        "outcome",
        "failure_classification",
        "boundary_checks",
        "stdout_tail",
        "stderr_tail",
    }
)

_BOUNDARY_CHECK_FIELDS = frozenset(
    {
        "workspace_read_only",
        "metadata_read_only",
        "scratch_writable",
        "root_read_only",
        "network_isolated",
        "credentials_scrubbed",
        "devices_restricted",
        "workspace_venv_absent",
        "installed_verifier_isolated",
    }
)

_FAILURE_CLASSIFICATIONS = frozenset(
    {"none", "validation", "scheduler", "timeout", "execution", "cleanup", "boundary"}
)


def _length_prefixed(value: bytes) -> bytes:
    """Encode bytes with an unambiguous unsigned length prefix."""
    if len(value) >= 1 << 64:
        raise ValueError("canonical digest value is too large")
    return len(value).to_bytes(8, byteorder="big") + value


def _validate_digest_domain(domain: str) -> bytes:
    """Return the bounded ASCII digest domain or reject ambiguous input."""
    if not isinstance(domain, str) or not domain or len(domain) > 128 or "\x00" in domain:
        raise ValueError("canonical digest domain is invalid")
    try:
        encoded = domain.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("canonical digest domain must be ASCII") from error
    return encoded


def _validate_logical_path(path: str) -> None:
    """Reject paths that would have more than one logical digest identity."""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError("canonical digest logical path is invalid")
    if path.startswith("/") or path.endswith("/") or "//" in path:
        raise ValueError("canonical digest logical path is not canonical")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("canonical digest logical path is not canonical")


def canonical_digest(domain: str, entries: Iterable[tuple[str, bytes]]) -> str:
    """Return a deterministic SHA-256 for sorted logical paths and raw bytes.

    The versioned prefix, domain, entry count, path length, and content length
    prevent concatenation and cross-contract collisions. Inputs are not read
    from the filesystem here, so callers must reject symlinks and non-regular
    files before they construct ``entries``.
    """
    encoded_domain = _validate_digest_domain(domain)
    normalized_entries: list[tuple[str, bytes]] = []
    seen_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise ValueError("canonical digest entry is invalid")
        path, content = entry
        _validate_logical_path(path)
        if type(content) is not bytes:
            raise ValueError("canonical digest content must be bytes")
        if path in seen_paths:
            raise ValueError("canonical digest has a duplicate logical path")
        seen_paths.add(path)
        normalized_entries.append((path, content))

    hasher = hashlib.sha256()
    hasher.update(_CANONICAL_DIGEST_PREFIX)
    hasher.update(_length_prefixed(encoded_domain))
    hasher.update(len(normalized_entries).to_bytes(8, byteorder="big"))
    for path, content in sorted(normalized_entries):
        hasher.update(_length_prefixed(path.encode("utf-8")))
        hasher.update(_length_prefixed(content))
    return hasher.hexdigest()


def _require_closed_fields(
    payload: Mapping[str, object], expected: frozenset[str], name: str
) -> None:
    """Reject missing or extra fields from one untrusted JSON object."""
    keys = set(payload)
    missing = expected - keys
    extra = keys - expected
    if missing or extra:
        raise ValueError(f"{name} schema fields are invalid")


def _require_schema_version(value: object, name: str) -> int:
    """Require the one supported schema version."""
    if type(value) is not int or value != SCHEMA_VERSION:
        raise ValueError(f"{name} schema version is invalid")
    return value


def _require_text(value: object, name: str, pattern: re.Pattern[str], maximum: int) -> str:
    """Require a bounded string that matches its closed grammar."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or pattern.fullmatch(value) is None
    ):
        raise ValueError(f"{name} is invalid")
    return value


def _require_sha256(value: object, name: str) -> str:
    """Require a lowercase full SHA-256 digest."""
    return _require_text(value, name, _DIGEST_RE, 64)


def _require_head_sha(value: object) -> str:
    """Require one complete SHA-1 or SHA-256 Git object identifier."""
    return _require_text(value, "expected head SHA", _HEAD_SHA_RE, 64)


def _require_absolute_path(value: object, name: str) -> str:
    """Require an already normalized, non-root absolute POSIX path."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} path is invalid")
    if value == "/" or value.startswith("//") or not os.path.isabs(value):
        raise ValueError(f"{name} path is not absolute")
    if os.path.normpath(value) != value or value.endswith("/"):
        raise ValueError(f"{name} path is not normalized")
    return value


def _require_timeout(value: object) -> int:
    """Require a bounded positive timeout without accepting booleans."""
    if type(value) is not int or not 0 < value <= MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout seconds is invalid")
    return value


def _require_bool(value: object, name: str) -> bool:
    """Require an actual JSON boolean."""
    if type(value) is not bool:
        raise ValueError(f"{name} is invalid")
    return value


def _require_tail(value: object, name: str) -> str:
    """Require bounded text diagnostics with no embedded NUL."""
    if not isinstance(value, str) or "\x00" in value or len(value) > MAX_DIAGNOSTIC_CHARS:
        raise ValueError(f"{name} is invalid")
    return value


@dataclass(frozen=True)
class LinuxHostVerificationRequest:
    """Validated lease data for one Linux host-verification allocation."""

    schema_version: int
    run_id: str
    command_id: str
    expected_head_sha: str
    timeout_seconds: int
    submit_hostname: str
    source_archive_path: str
    source_extract_path: str
    source_archive_sha256: str
    git_metadata_archive_path: str
    git_metadata_extract_path: str
    git_metadata_archive_sha256: str
    image_path: str
    image_sha256: str
    image_manifest_sha256: str
    contract_root: str
    contract_sha256: str
    allocation_driver_sha256: str
    srun_path: str
    scratch_path: str
    stdout_path: str
    stderr_path: str
    receipt_path: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> LinuxHostVerificationRequest:
        """Parse one complete, closed request schema without filesystem access."""
        _require_closed_fields(payload, _REQUEST_FIELDS, "request")
        path_names = (
            "source_archive_path",
            "source_extract_path",
            "git_metadata_archive_path",
            "git_metadata_extract_path",
            "image_path",
            "contract_root",
            "srun_path",
            "scratch_path",
            "stdout_path",
            "stderr_path",
            "receipt_path",
        )
        paths = {
            name: _require_absolute_path(payload[name], name.removesuffix("_path"))
            for name in path_names
        }
        if len(set(paths.values())) != len(paths):
            raise ValueError("request paths must be distinct")
        return cls(
            schema_version=_require_schema_version(payload["schema_version"], "request"),
            run_id=_require_text(payload["run_id"], "run ID", _RUN_ID_RE, 128),
            command_id=_require_text(payload["command_id"], "command ID", _IDENTIFIER_RE, 128),
            expected_head_sha=_require_head_sha(payload["expected_head_sha"]),
            timeout_seconds=_require_timeout(payload["timeout_seconds"]),
            submit_hostname=_require_text(
                payload["submit_hostname"], "submit hostname", _HOSTNAME_RE, 254
            ),
            source_archive_path=paths["source_archive_path"],
            source_extract_path=paths["source_extract_path"],
            source_archive_sha256=_require_sha256(
                payload["source_archive_sha256"], "source archive SHA-256"
            ),
            git_metadata_archive_path=paths["git_metadata_archive_path"],
            git_metadata_extract_path=paths["git_metadata_extract_path"],
            git_metadata_archive_sha256=_require_sha256(
                payload["git_metadata_archive_sha256"], "Git metadata archive SHA-256"
            ),
            image_path=paths["image_path"],
            image_sha256=_require_sha256(payload["image_sha256"], "image SHA-256"),
            image_manifest_sha256=_require_sha256(
                payload["image_manifest_sha256"], "image manifest SHA-256"
            ),
            contract_root=paths["contract_root"],
            contract_sha256=_require_sha256(payload["contract_sha256"], "contract SHA-256"),
            allocation_driver_sha256=_require_sha256(
                payload["allocation_driver_sha256"], "allocation driver SHA-256"
            ),
            srun_path=paths["srun_path"],
            scratch_path=paths["scratch_path"],
            stdout_path=paths["stdout_path"],
            stderr_path=paths["stderr_path"],
            receipt_path=paths["receipt_path"],
        )

    def to_mapping(self) -> dict[str, object]:
        """Return JSON-ready request fields in their stable schema form."""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "command_id": self.command_id,
            "expected_head_sha": self.expected_head_sha,
            "timeout_seconds": self.timeout_seconds,
            "submit_hostname": self.submit_hostname,
            "source_archive_path": self.source_archive_path,
            "source_extract_path": self.source_extract_path,
            "source_archive_sha256": self.source_archive_sha256,
            "git_metadata_archive_path": self.git_metadata_archive_path,
            "git_metadata_extract_path": self.git_metadata_extract_path,
            "git_metadata_archive_sha256": self.git_metadata_archive_sha256,
            "image_path": self.image_path,
            "image_sha256": self.image_sha256,
            "image_manifest_sha256": self.image_manifest_sha256,
            "contract_root": self.contract_root,
            "contract_sha256": self.contract_sha256,
            "allocation_driver_sha256": self.allocation_driver_sha256,
            "srun_path": self.srun_path,
            "scratch_path": self.scratch_path,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "receipt_path": self.receipt_path,
        }


@dataclass(frozen=True)
class LinuxHostVerificationReceipt:
    """Validated, bounded outcome data for one Linux host-verification lease."""

    schema_version: int
    run_id: str
    command_id: str
    expected_head_sha: str
    source_archive_sha256: str
    git_metadata_archive_sha256: str
    image_sha256: str
    image_manifest_sha256: str
    contract_sha256: str
    allocation_driver_sha256: str
    backend: str
    allocation_job_id: str
    submit_hostname: str
    allocation_hostname: str
    return_code: int
    timed_out: bool
    outcome: str
    failure_classification: str
    boundary_checks: Mapping[str, bool]
    stdout_tail: str
    stderr_tail: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> LinuxHostVerificationReceipt:
        """Parse one complete, closed receipt schema without trusting its result."""
        _require_closed_fields(payload, _RECEIPT_FIELDS, "receipt")
        raw_checks = payload["boundary_checks"]
        if not isinstance(raw_checks, Mapping):
            raise ValueError("receipt boundary checks are invalid")
        _require_closed_fields(raw_checks, _BOUNDARY_CHECK_FIELDS, "receipt boundary checks")
        boundary_checks = {
            name: _require_bool(raw_checks[name], f"boundary check {name}")
            for name in sorted(_BOUNDARY_CHECK_FIELDS)
        }
        outcome = payload["outcome"]
        classification = payload["failure_classification"]
        return_code = payload["return_code"]
        timed_out = _require_bool(payload["timed_out"], "timed out")
        if outcome not in {"passed", "failed"}:
            raise ValueError("receipt outcome is invalid")
        if classification not in _FAILURE_CLASSIFICATIONS:
            raise ValueError("receipt failure classification is invalid")
        if type(return_code) is not int or not 0 <= return_code <= 255:
            raise ValueError("receipt return code is invalid")
        if outcome == "passed" and (
            return_code != 0
            or timed_out
            or classification != "none"
            or not all(boundary_checks.values())
        ):
            raise ValueError("passed receipt has invalid execution or boundary state")
        if outcome == "failed" and classification == "none":
            raise ValueError("failed receipt requires a failure classification")
        if timed_out != (classification == "timeout"):
            raise ValueError("receipt timeout state does not match failure classification")
        return cls(
            schema_version=_require_schema_version(payload["schema_version"], "receipt"),
            run_id=_require_text(payload["run_id"], "run ID", _RUN_ID_RE, 128),
            command_id=_require_text(payload["command_id"], "command ID", _IDENTIFIER_RE, 128),
            expected_head_sha=_require_head_sha(payload["expected_head_sha"]),
            source_archive_sha256=_require_sha256(
                payload["source_archive_sha256"], "source archive SHA-256"
            ),
            git_metadata_archive_sha256=_require_sha256(
                payload["git_metadata_archive_sha256"], "Git metadata archive SHA-256"
            ),
            image_sha256=_require_sha256(payload["image_sha256"], "image SHA-256"),
            image_manifest_sha256=_require_sha256(
                payload["image_manifest_sha256"], "image manifest SHA-256"
            ),
            contract_sha256=_require_sha256(payload["contract_sha256"], "contract SHA-256"),
            allocation_driver_sha256=_require_sha256(
                payload["allocation_driver_sha256"], "allocation driver SHA-256"
            ),
            backend=_require_text(payload["backend"], "backend", re.compile(r"linux-pyxis"), 11),
            allocation_job_id=_require_text(
                payload["allocation_job_id"], "allocation job ID", _JOB_ID_RE, 255
            ),
            submit_hostname=_require_text(
                payload["submit_hostname"], "submit hostname", _HOSTNAME_RE, 254
            ),
            allocation_hostname=_require_text(
                payload["allocation_hostname"], "allocation hostname", _HOSTNAME_RE, 254
            ),
            return_code=return_code,
            timed_out=timed_out,
            outcome=outcome,
            failure_classification=classification,
            boundary_checks=MappingProxyType(boundary_checks),
            stdout_tail=_require_tail(payload["stdout_tail"], "stdout tail"),
            stderr_tail=_require_tail(payload["stderr_tail"], "stderr tail"),
        )

    def validate_against_request(self, request: LinuxHostVerificationRequest) -> None:
        """Require that this receipt binds every request identity and lease digest."""
        binding_fields = (
            "run_id",
            "command_id",
            "expected_head_sha",
            "source_archive_sha256",
            "git_metadata_archive_sha256",
            "image_sha256",
            "image_manifest_sha256",
            "contract_sha256",
            "allocation_driver_sha256",
            "submit_hostname",
        )
        for field in binding_fields:
            if getattr(self, field) != getattr(request, field):
                raise ValueError(f"receipt does not match request {field}")

    def to_mapping(self) -> dict[str, object]:
        """Return JSON-ready receipt fields in their stable schema form."""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "command_id": self.command_id,
            "expected_head_sha": self.expected_head_sha,
            "source_archive_sha256": self.source_archive_sha256,
            "git_metadata_archive_sha256": self.git_metadata_archive_sha256,
            "image_sha256": self.image_sha256,
            "image_manifest_sha256": self.image_manifest_sha256,
            "contract_sha256": self.contract_sha256,
            "allocation_driver_sha256": self.allocation_driver_sha256,
            "backend": self.backend,
            "allocation_job_id": self.allocation_job_id,
            "submit_hostname": self.submit_hostname,
            "allocation_hostname": self.allocation_hostname,
            "return_code": self.return_code,
            "timed_out": self.timed_out,
            "outcome": self.outcome,
            "failure_classification": self.failure_classification,
            "boundary_checks": dict(self.boundary_checks),
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


__all__ = [
    "MAX_DIAGNOSTIC_CHARS",
    "MAX_TIMEOUT_SECONDS",
    "SCHEMA_VERSION",
    "LinuxHostVerificationReceipt",
    "LinuxHostVerificationRequest",
    "canonical_digest",
]
