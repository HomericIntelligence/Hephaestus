"""Explicit, fail-closed host capability contracts for pipeline workers."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, cast

from hephaestus.config.child_environments import build_git_signing_env

from .diagnostics import redact_diagnostic_text

_DIAGNOSTIC_MAX = 4_000
_FULL_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_REQUEST_ID = re.compile(r"[0-9a-f]{32}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_CAPABILITIES = frozenset({"quota"})
_PHASES = frozenset({"pr_review", "rebase"})
_PURPOSES = frozenset({"scratch", "pi_smoke_logs"})
_STEPS = frozenset({"backend", "create", "attach", "detach"})
_VOLUME_PATHS = {
    "scratch": ("scratch.dmg", "scratch"),
    "pi_smoke_logs": ("pi-smoke-logs.dmg", "pi-smoke-logs"),
}
_RECEIPT_KEYS = frozenset(
    {
        "target",
        "outcome",
        "available",
        "token",
        "failed_step",
        "purpose",
        "receipt_id",
        "stdout_tail",
        "stderr_tail",
        "return_code",
        "operating_system_error",
        "exception_type",
        "cached",
        "probe_receipt_id",
        "retained_root",
    }
)

QUOTA_AVAILABLE_TOKEN = "host_verification_quota_available"  # noqa: S105
QUOTA_BACKEND_NOT_APPLICABLE_TOKEN = "host_verification_quota_backend_not_applicable"  # noqa: S105
QUOTA_CREATE_FAILED_TOKEN = "host_verification_quota_create_failed"  # noqa: S105
QUOTA_ATTACH_FAILED_TOKEN = "host_verification_quota_attach_failed"  # noqa: S105
QUOTA_DETACH_FAILED_TOKEN = "host_verification_quota_detach_failed"  # noqa: S105
QUOTA_UNAVAILABLE_TOKEN = "host_verification_quota_unavailable"  # noqa: S105
RECEIPT_FAILED_TOKEN = "host_capability_receipt_failed"  # noqa: S105


def _positive_integer(value: object) -> bool:
    return type(value) is int and cast(int, value) > 0


def _absolute_path(value: object) -> bool:
    return isinstance(value, Path) and value.is_absolute()


@dataclass(frozen=True, slots=True)
class CapabilityRequestTarget:
    """Inputs that a stage knows before a worker uses a host capability."""

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

    def __post_init__(self) -> None:
        """Reject malformed target data before an external command starts."""
        if (
            not isinstance(self.repository, str)
            or _REPOSITORY.fullmatch(self.repository) is None
            or not _positive_integer(self.issue_number)
            or (self.pr_number is not None and not _positive_integer(self.pr_number))
            or not _absolute_path(self.repository_root)
            or not _absolute_path(self.checkout_path)
            or _FULL_SHA.fullmatch(self.expected_head_sha) is None
            or _REQUEST_ID.fullmatch(self.request_id) is None
            or self.capability not in _CAPABILITIES
            or self.phase not in _PHASES
            or self.purpose not in _PURPOSES
        ):
            raise ValueError("capability request target is invalid")

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible strict request representation."""
        value = asdict(self)
        value["repository_root"] = str(self.repository_root)
        value["checkout_path"] = str(self.checkout_path)
        return value

    @classmethod
    def from_dict(cls, value: object) -> CapabilityRequestTarget:
        """Parse one strict request representation."""
        keys = {
            "repository",
            "issue_number",
            "pr_number",
            "repository_root",
            "checkout_path",
            "expected_head_sha",
            "phase",
            "purpose",
            "request_id",
            "capability",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError("capability request target schema is invalid")
        data = dict(value)
        data["repository_root"] = Path(str(data["repository_root"]))
        data["checkout_path"] = Path(str(data["checkout_path"]))
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class CapabilityReceiptTarget:
    """Worker-validated facts that bind one capability receipt."""

    request: CapabilityRequestTarget
    canonical_repository_root: Path
    root_device: int
    execution_boundary_id: str
    source_head_sha: str

    def __post_init__(self) -> None:
        """Reject a receipt target that does not match its request."""
        if (
            not _absolute_path(self.canonical_repository_root)
            or self.canonical_repository_root != self.request.repository_root.resolve()
            or type(self.root_device) is not int
            or self.root_device < 0
            or not isinstance(self.execution_boundary_id, str)
            or not self.execution_boundary_id
            or len(self.execution_boundary_id) > 128
            or _FULL_SHA.fullmatch(self.source_head_sha) is None
            or (
                self.request.phase != "rebase"
                and self.source_head_sha != self.request.expected_head_sha
            )
        ):
            raise ValueError("capability receipt target is invalid")

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible strict target representation."""
        return {
            "request": self.request.to_dict(),
            "canonical_repository_root": str(self.canonical_repository_root),
            "root_device": self.root_device,
            "execution_boundary_id": self.execution_boundary_id,
            "source_head_sha": self.source_head_sha,
        }

    @classmethod
    def from_dict(cls, value: object) -> CapabilityReceiptTarget:
        """Parse one strict receipt-target representation."""
        keys = {
            "request",
            "canonical_repository_root",
            "root_device",
            "execution_boundary_id",
            "source_head_sha",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError("capability receipt target schema is invalid")
        return cls(
            request=CapabilityRequestTarget.from_dict(value["request"]),
            canonical_repository_root=Path(str(value["canonical_repository_root"])),
            root_device=value["root_device"],  # type: ignore[arg-type]
            execution_boundary_id=value["execution_boundary_id"],  # type: ignore[arg-type]
            source_head_sha=value["source_head_sha"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class HostCapabilityReceipt:
    """A durable, target-bound result from one host capability operation."""

    target: CapabilityReceiptTarget
    outcome: str
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
        """Keep receipt identity, outcome, and diagnostics strict."""
        if (
            self.outcome not in {"available", "unavailable"}
            or self.available is not (self.outcome == "available")
            or not isinstance(self.token, str)
            or not self.token
            or self.failed_step not in ({None} | _STEPS)
            or self.purpose != self.target.request.purpose
            or _REQUEST_ID.fullmatch(self.receipt_id) is None
            or (
                self.probe_receipt_id is not None
                and _REQUEST_ID.fullmatch(self.probe_receipt_id) is None
            )
            or (self.cached and self.probe_receipt_id is None)
            or (not self.cached and self.probe_receipt_id is not None)
            or (self.available and self.failed_step is not None)
            or (not self.available and self.failed_step is None)
            or (
                self.return_code is not None
                and (type(self.return_code) is not int or self.return_code < 0)
            )
        ):
            raise ValueError("host capability receipt is invalid")
        for value in (
            self.stdout_tail,
            self.stderr_tail,
            self.operating_system_error,
            self.exception_type,
            self.retained_root,
        ):
            if not isinstance(value, str) or len(value) > _DIAGNOSTIC_MAX:
                raise ValueError("host capability receipt diagnostic is invalid")

    @classmethod
    def available_receipt(
        cls,
        target: CapabilityReceiptTarget,
        *,
        receipt_id: str | None = None,
        stdout_tail: str = "",
        stderr_tail: str = "",
        return_code: int | None = 0,
    ) -> HostCapabilityReceipt:
        """Create one successful target-bound receipt."""
        return cls(
            target=target,
            outcome="available",
            available=True,
            token=QUOTA_AVAILABLE_TOKEN,
            failed_step=None,
            purpose=target.request.purpose,
            receipt_id=receipt_id or uuid.uuid4().hex,
            stdout_tail=_tail(stdout_tail),
            stderr_tail=_tail(stderr_tail),
            return_code=return_code,
        )

    def to_dict(self) -> dict[str, object]:
        """Return a strict JSON-compatible receipt representation."""
        value = asdict(self)
        value["target"] = self.target.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: object) -> HostCapabilityReceipt:
        """Parse and validate one exact receipt schema."""
        if not isinstance(value, dict) or set(value) != _RECEIPT_KEYS:
            raise ValueError("host capability receipt schema is invalid")
        data = dict(value)
        data["target"] = CapabilityReceiptTarget.from_dict(data["target"])
        return cls(**data)  # type: ignore[arg-type]


class HostCapabilityError(RuntimeError):
    """Report one typed host capability failure with its complete receipt."""

    def __init__(self, receipt: HostCapabilityReceipt) -> None:
        """Initialize the error from its strict receipt."""
        super().__init__(receipt.token)
        self.receipt = receipt


class QuotaBackend(Protocol):
    """Provide the reviewed quota boundary for immutable host verification."""

    backend_id: str

    def preflight(self, target: CapabilityReceiptTarget) -> HostCapabilityReceipt:
        """Return quota availability for one worker-validated target."""

    def volume(
        self,
        target: CapabilityReceiptTarget,
        purpose: str,
        *,
        mountpoint: Path | None = None,
    ) -> AbstractContextManager[Path]:
        """Open one disposable quota volume for the selected purpose."""


class GitSigningProvider(Protocol):
    """Provide a validated, controlled environment for signed Git writes."""

    def environment(self, cwd: Path, *, timeout: int) -> dict[str, str] | None:
        """Return the complete controlled environment or no capability."""


CommandRunner = Callable[..., subprocess.CompletedProcess[Any]]


def _tail(value: bytes | str | None) -> str:
    """Return a redacted bounded diagnostic tail."""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    return redact_diagnostic_text(text)[-_DIAGNOSTIC_MAX:]


def bind_receipt_target(
    request: CapabilityRequestTarget, *, execution_boundary_id: str, source_head_sha: str
) -> CapabilityReceiptTarget:
    """Add worker-read filesystem, boundary, and source facts to a request."""
    canonical_root = request.repository_root.resolve(strict=True)
    return CapabilityReceiptTarget(
        request=request,
        canonical_repository_root=canonical_root,
        root_device=canonical_root.stat().st_dev,
        execution_boundary_id=execution_boundary_id,
        source_head_sha=source_head_sha,
    )


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _open_secure_subdirectory(  # noqa: C901
    root: Path, components: tuple[str, ...], *, create: bool
) -> tuple[Path, int]:
    """Open a repository-confined directory through no-follow descriptors."""
    canonical_root = root.resolve(strict=True)
    root_fd = os.open(canonical_root, _directory_flags())
    current_fd = root_fd
    current_path = canonical_root
    try:
        for component in components:
            if not component or component in {".", ".."} or "/" in component:
                raise ValueError("host capability path is unsafe")
            try:
                next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                try:
                    next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
                except OSError as error:
                    raise ValueError("host capability path is unsafe") from error
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError("host capability path is unsafe") from error
                raise
            if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                os.close(next_fd)
                raise ValueError("host capability path is unsafe")
            os.fchmod(next_fd, 0o700)
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
            current_path /= component
        return current_path, current_fd
    except BaseException:
        if current_fd != root_fd:
            os.close(current_fd)
        raise
    finally:
        if current_fd != root_fd:
            os.close(root_fd)


class HostCapabilityReceiptStore:
    """Persist strict receipts below one repository through no-follow paths."""

    _COMPONENTS = ("build", ".issue_implementer", "host-capability-receipts")

    def __init__(self, repository_root: Path) -> None:
        """Bind the store to one canonical repository root."""
        self._repository_root = repository_root.resolve(strict=True)

    def store(self, receipt: HostCapabilityReceipt) -> HostCapabilityReceipt:
        """Atomically store and read back one private receipt."""
        if receipt.target.canonical_repository_root != self._repository_root:
            raise ValueError("host capability receipt root does not match store")
        _directory, directory_fd = _open_secure_subdirectory(
            self._repository_root, self._COMPONENTS, create=True
        )
        name = f"{receipt.receipt_id}.json"
        temporary = f".{name}.{secrets.token_hex(16)}.tmp"
        content = json.dumps(receipt.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            lock_fd = self._lock(directory_fd)
            fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    fd = -1
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if fd >= 0:
                    os.close(fd)
            os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
            stored = self._read_from_fd(directory_fd, name)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory_fd)
            raise
        finally:
            if "lock_fd" in locals():
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            os.close(directory_fd)
        if stored != receipt:
            raise ValueError("host capability receipt readback failed")
        return stored

    def read(self, receipt_id: str) -> HostCapabilityReceipt:
        """Read one strict regular receipt without following a symlink."""
        if _REQUEST_ID.fullmatch(receipt_id) is None:
            raise ValueError("host capability receipt id is invalid")
        _directory, directory_fd = _open_secure_subdirectory(
            self._repository_root, self._COMPONENTS, create=False
        )
        try:
            lock_fd = self._lock(directory_fd)
            try:
                return self._read_from_fd(directory_fd, f"{receipt_id}.json")
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _lock(directory_fd: int) -> int:
        """Take the private receipt-directory lock without following links."""
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        lock_fd = os.open(".lock", flags, 0o600, dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise ValueError("host capability receipt lock is not a regular file")
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            return lock_fd
        except BaseException:
            os.close(lock_fd)
            raise

    @staticmethod
    def _read_from_fd(directory_fd: int, name: str) -> HostCapabilityReceipt:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(name, flags, dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("host capability receipt is not a regular file")
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                return HostCapabilityReceipt.from_dict(json.load(handle))
        finally:
            if fd >= 0:
                os.close(fd)


class ProcessLocalPreflightCache:
    """Cache probes only inside one process and exact execution boundary."""

    def __init__(self) -> None:
        """Create an empty process-local cache."""
        self._lock = threading.Lock()
        self._receipts: dict[tuple[object, ...], HostCapabilityReceipt] = {}

    @staticmethod
    def _key(backend: QuotaBackend, target: CapabilityReceiptTarget) -> tuple[object, ...]:
        return (
            os.getpid(),
            target.execution_boundary_id,
            str(target.canonical_repository_root),
            target.root_device,
            target.request.capability,
            backend.backend_id,
        )

    def preflight(
        self, backend: QuotaBackend, target: CapabilityReceiptTarget
    ) -> HostCapabilityReceipt:
        """Return a fresh target receipt from one scoped cached probe."""
        key = self._key(backend, target)
        with self._lock:
            probe = self._receipts.get(key)
            if probe is None:
                probe = backend.preflight(target)
                self._receipts[key] = probe
                return probe
            return replace(
                probe,
                target=target,
                purpose=target.request.purpose,
                receipt_id=uuid.uuid4().hex,
                cached=True,
                probe_receipt_id=probe.receipt_id,
            )


class HdiutilQuotaBackend:
    """Provide disposable quota volumes through fixed hdiutil operations."""

    backend_id = "hdiutil-v2"

    def __init__(self, command_runner: CommandRunner | None = None) -> None:
        """Use the fixed system tool or an injected test runner."""
        self._command_runner = command_runner or subprocess.run

    def preflight(self, target: CapabilityReceiptTarget) -> HostCapabilityReceipt:
        """Create, attach, and detach a probe volume in the secure request root."""
        try:
            with self._open_volume(target, "scratch", probe=True):
                pass
        except HostCapabilityError as error:
            return error.receipt
        return HostCapabilityReceipt.available_receipt(target)

    @contextmanager
    def volume(
        self,
        target: CapabilityReceiptTarget,
        purpose: str,
        *,
        mountpoint: Path | None = None,
    ) -> Iterator[Path]:
        """Yield one bounded volume and preserve a failed-detach root."""
        with self._open_volume(target, purpose, probe=False, mountpoint=mountpoint) as mounted:
            yield mounted

    @contextmanager
    def _open_volume(  # noqa: C901
        self,
        target: CapabilityReceiptTarget,
        purpose: str,
        *,
        probe: bool,
        mountpoint: Path | None = None,
    ) -> Iterator[Path]:
        if purpose not in _PURPOSES or target.request.purpose != purpose:
            raise ValueError("quota volume purpose does not match target")
        receipt_id = uuid.uuid4().hex
        simulated = self._command_runner is not subprocess.run
        binary = Path("/usr/bin/hdiutil")
        if not simulated and sys.platform != "darwin":
            raise HostCapabilityError(
                self._failure(target, receipt_id, QUOTA_BACKEND_NOT_APPLICABLE_TOKEN, "backend")
            )
        if not simulated and (not binary.is_file() or not os.access(binary, os.X_OK)):
            raise HostCapabilityError(
                self._failure(target, receipt_id, QUOTA_UNAVAILABLE_TOKEN, "backend")
            )
        try:
            parent, parent_fd = _open_secure_subdirectory(
                target.canonical_repository_root,
                ("build", ".host-verification"),
                create=True,
            )
        except (OSError, ValueError) as error:
            raise HostCapabilityError(
                self._failure(
                    target,
                    receipt_id,
                    QUOTA_UNAVAILABLE_TOKEN,
                    "backend",
                    error=error,
                )
            ) from error
        request_name = target.request.request_id
        request_root = parent / request_name
        try:
            try:
                os.mkdir(request_name, 0o700, dir_fd=parent_fd)
            except (FileExistsError, OSError) as error:
                raise HostCapabilityError(
                    self._failure(
                        target,
                        receipt_id,
                        QUOTA_UNAVAILABLE_TOKEN,
                        "backend",
                        error=error,
                    )
                ) from error
            request_fd = os.open(request_name, _directory_flags(), dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        os.fchmod(request_fd, 0o700)
        image_name, mount_name = ("preflight.dmg", "preflight") if probe else _VOLUME_PATHS[purpose]
        image = request_root / image_name
        mount = request_root / mount_name if mountpoint is None else mountpoint
        mount_parent_fd = request_fd
        mount_leaf = mount_name
        external_mount = mountpoint is not None
        attached = False
        retain = False
        try:
            try:
                if external_mount:
                    if (
                        not mount.is_absolute()
                        or mount.name != mount_name
                        or mount.exists()
                        or mount.is_symlink()
                    ):
                        raise ValueError("quota volume mountpoint is unsafe")
                    parent = mount.parent.resolve(strict=True)
                    if parent != mount.parent:
                        raise ValueError("quota volume mountpoint parent is unsafe")
                    mount_parent_fd = os.open(parent, _directory_flags())
                    mount_leaf = mount.name
                os.mkdir(mount_leaf, 0o700, dir_fd=mount_parent_fd)
                self._assert_leaf(mount_parent_fd, mount_leaf, directory=True)
                self._assert_abs_path(request_root, image)
                if not external_mount:
                    self._assert_abs_path(request_root, mount)
            except (OSError, ValueError) as error:
                raise HostCapabilityError(
                    self._failure(
                        target,
                        receipt_id,
                        QUOTA_UNAVAILABLE_TOKEN,
                        "backend",
                        error=error,
                    )
                ) from error
            self._run(
                (
                    str(binary),
                    "create",
                    "-size",
                    "512m",
                    "-fs",
                    "HFS+",
                    str(image),
                ),
                target,
                receipt_id,
                "create",
                QUOTA_CREATE_FAILED_TOKEN,
            )
            self._assert_leaf(request_fd, image_name, directory=False, allow_missing=simulated)
            self._run(
                (
                    str(binary),
                    "attach",
                    "-nobrowse",
                    "-mountpoint",
                    str(mount),
                    str(image),
                ),
                target,
                receipt_id,
                "attach",
                QUOTA_ATTACH_FAILED_TOKEN,
            )
            attached = True
            try:
                yield mount
            finally:
                if attached:
                    try:
                        self._assert_leaf(mount_parent_fd, mount_leaf, directory=True)
                    except (OSError, ValueError) as error:
                        raise HostCapabilityError(
                            self._failure(
                                target,
                                receipt_id,
                                QUOTA_DETACH_FAILED_TOKEN,
                                "detach",
                                error=error,
                                retained_root=str(request_root),
                            )
                        ) from error
                    self._run(
                        (str(binary), "detach", "-force", str(mount)),
                        target,
                        receipt_id,
                        "detach",
                        QUOTA_DETACH_FAILED_TOKEN,
                        retained_root=str(request_root),
                    )
                    attached = False
        except HostCapabilityError as error:
            retain = error.receipt.failed_step == "detach"
            raise
        finally:
            if mount_parent_fd != request_fd:
                os.close(mount_parent_fd)
            os.close(request_fd)
            if attached:
                retain = True
            if not retain:
                if external_mount:
                    shutil.rmtree(mount, ignore_errors=True)
                shutil.rmtree(request_root, ignore_errors=True)

    def _run(
        self,
        argv: tuple[str, ...],
        target: CapabilityReceiptTarget,
        receipt_id: str,
        step: str,
        token: str,
        *,
        retained_root: str = "",
    ) -> subprocess.CompletedProcess[Any]:
        try:
            result = self._command_runner(
                argv,
                capture_output=True,
                check=False,
                timeout=30,
                env={"PATH": os.defpath},
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HostCapabilityError(
                self._failure(
                    target,
                    receipt_id,
                    token,
                    step,
                    error=error,
                    retained_root=retained_root,
                )
            ) from error
        if result.returncode != 0:
            raise HostCapabilityError(
                self._failure(
                    target,
                    receipt_id,
                    token,
                    step,
                    result=result,
                    retained_root=retained_root,
                )
            )
        return result

    @staticmethod
    def _assert_abs_path(root: Path, candidate: Path) -> None:
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ValueError("quota volume path escaped its request root") from error

    @staticmethod
    def _assert_leaf(
        directory_fd: int,
        name: str,
        *,
        directory: bool,
        allow_missing: bool = False,
    ) -> None:
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            if allow_missing:
                return
            raise ValueError("quota volume leaf disappeared") from None
        expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if stat.S_ISLNK(info.st_mode) or not expected:
            raise ValueError("quota volume leaf is unsafe")

    @staticmethod
    def _failure(
        target: CapabilityReceiptTarget,
        receipt_id: str,
        token: str,
        step: str,
        *,
        result: subprocess.CompletedProcess[Any] | None = None,
        error: BaseException | None = None,
        retained_root: str = "",
    ) -> HostCapabilityReceipt:
        return HostCapabilityReceipt(
            target=target,
            outcome="unavailable",
            available=False,
            token=token,
            failed_step=step,
            purpose=target.request.purpose,
            receipt_id=receipt_id,
            stdout_tail=_tail(None if result is None else result.stdout),
            stderr_tail=_tail(None if result is None else result.stderr),
            return_code=None if result is None else result.returncode,
            operating_system_error=_tail(str(error)) if error is not None else "",
            exception_type="" if error is None else type(error).__name__,
            retained_root=_tail(retained_root),
        )


class DirectoryQuotaBackend:
    """Use one operator-supplied bounded directory for Pyxis workers."""

    def __init__(self, root: Path) -> None:
        """Bind the backend to one private absolute quota root."""
        if not root.is_absolute():
            raise ValueError("quota directory root is unsafe")
        self._root = root
        self.backend_id = f"directory-v1:{root}"

    def preflight(self, target: CapabilityReceiptTarget) -> HostCapabilityReceipt:
        """Prove that one private request directory can be created and removed."""
        try:
            with self.volume(target, target.request.purpose):
                pass
        except HostCapabilityError as error:
            return error.receipt
        except (OSError, ValueError) as error:
            return HostCapabilityReceipt(
                target=target,
                outcome="unavailable",
                available=False,
                token=QUOTA_CREATE_FAILED_TOKEN,
                failed_step="create",
                purpose=target.request.purpose,
                receipt_id=uuid.uuid4().hex,
                operating_system_error=_tail(str(error)),
                exception_type=type(error).__name__,
            )
        return HostCapabilityReceipt.available_receipt(target)

    @contextmanager
    def volume(
        self,
        target: CapabilityReceiptTarget,
        purpose: str,
        *,
        mountpoint: Path | None = None,
    ) -> Iterator[Path]:
        """Yield one private purpose directory below the bounded root."""
        if mountpoint is not None:
            raise ValueError("the directory backend does not support a mountpoint")
        if target.request.purpose != purpose or purpose not in _PURPOSES:
            raise ValueError("quota directory purpose does not match target")
        receipt_id = uuid.uuid4().hex
        request_root = self._root / target.request.request_id
        volume = request_root / _VOLUME_PATHS[purpose][1]
        root_fd = -1
        request_fd = -1
        try:
            canonical_root = self._root.resolve(strict=True)
            if canonical_root != self._root:
                raise ValueError("quota directory root is unsafe")
            root_fd = os.open(canonical_root, _directory_flags())
            root_info = os.fstat(root_fd)
            if root_info.st_uid != os.geteuid() or stat.S_IMODE(root_info.st_mode) & 0o077:
                raise ValueError("quota directory root is not private")
            os.mkdir(target.request.request_id, 0o700, dir_fd=root_fd)
            request_fd = os.open(
                target.request.request_id,
                _directory_flags(),
                dir_fd=root_fd,
            )
            os.fchmod(request_fd, 0o700)
            volume_name = _VOLUME_PATHS[purpose][1]
            os.mkdir(volume_name, 0o700, dir_fd=request_fd)
            volume_fd = os.open(volume_name, _directory_flags(), dir_fd=request_fd)
            try:
                os.fchmod(volume_fd, 0o700)
            finally:
                os.close(volume_fd)
            yield volume
        except HostCapabilityError:
            raise
        except (OSError, ValueError) as error:
            receipt = HostCapabilityReceipt(
                target=target,
                outcome="unavailable",
                available=False,
                token=QUOTA_CREATE_FAILED_TOKEN,
                failed_step="create",
                purpose=target.request.purpose,
                receipt_id=uuid.uuid4().hex,
                operating_system_error=_tail(str(error)),
                exception_type=type(error).__name__,
            )
            raise HostCapabilityError(receipt) from error
        finally:
            if request_fd >= 0:
                os.close(request_fd)
            if root_fd >= 0:
                try:
                    shutil.rmtree(target.request.request_id, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise HostCapabilityError(
                        HostCapabilityReceipt(
                            target=target,
                            outcome="unavailable",
                            available=False,
                            token=QUOTA_DETACH_FAILED_TOKEN,
                            failed_step="detach",
                            purpose=target.request.purpose,
                            receipt_id=receipt_id,
                            operating_system_error=_tail(str(error)),
                            exception_type=type(error).__name__,
                            retained_root=str(request_root),
                        )
                    ) from error
                finally:
                    os.close(root_fd)


class FakeGitSigningProvider:
    """Provide deterministic signing data for worker tests."""

    def __init__(self, environment: Mapping[str, str] | None) -> None:
        """Store one deterministic environment for tests."""
        self._environment = None if environment is None else dict(environment)

    def environment(self, cwd: Path, *, timeout: int) -> dict[str, str] | None:
        """Return a copy of the injected environment."""
        del cwd, timeout
        return None if self._environment is None else dict(self._environment)


class FakeQuotaBackend:
    """Provide deterministic preflight and volume behavior for worker tests."""

    backend_id = "fake-quota-v1"

    def __init__(
        self,
        volume_factory: Callable[[CapabilityReceiptTarget, str], AbstractContextManager[Path]],
        *,
        preflight_receipt: HostCapabilityReceipt | None = None,
    ) -> None:
        """Store deterministic preflight and volume seams for tests."""
        self._volume_factory = volume_factory
        self._preflight_receipt = preflight_receipt

    def preflight(self, target: CapabilityReceiptTarget) -> HostCapabilityReceipt:
        """Return the injected receipt or one available receipt."""
        if self._preflight_receipt is None:
            return HostCapabilityReceipt.available_receipt(target)
        return replace(
            self._preflight_receipt,
            target=target,
            purpose=target.request.purpose,
            receipt_id=uuid.uuid4().hex,
            cached=False,
            probe_receipt_id=None,
        )

    def volume(
        self,
        target: CapabilityReceiptTarget,
        purpose: str,
        *,
        mountpoint: Path | None = None,
    ) -> AbstractContextManager[Path]:
        """Return the injected volume context manager."""
        if target.request.purpose != purpose:
            raise ValueError("fake quota volume purpose does not match target")
        if mountpoint is not None:
            return self._external_volume(mountpoint, self._volume_factory(target, purpose))
        return self._volume_factory(target, purpose)

    @staticmethod
    @contextmanager
    def _external_volume(mountpoint: Path, backing: AbstractContextManager[Path]) -> Iterator[Path]:
        """Create one private fake mount at the requested test path."""
        if mountpoint.exists() or mountpoint.is_symlink():
            raise ValueError("fake quota volume mountpoint is unsafe")
        with backing:
            mountpoint.mkdir(mode=0o700)
            try:
                yield mountpoint
            finally:
                shutil.rmtree(mountpoint)


class ProductionGitSigningProvider:
    """Read and validate the minimum production SSH signing configuration."""

    _KEYS = ("user.name", "user.email", "gpg.format", "user.signingkey")

    def __init__(
        self,
        command_runner: CommandRunner | None = None,
        *,
        global_config: Path | None = None,
    ) -> None:
        """Use the system Git configuration or injected test seams."""
        self._command_runner = command_runner or subprocess.run
        self._global_config = global_config or (Path.home() / ".gitconfig")

    def environment(self, cwd: Path, *, timeout: int) -> dict[str, str] | None:
        """Return a finite controlled Git environment for signed writes."""
        expression = "^(user\\.name|user\\.email|gpg\\.format|user\\.signingkey)$"
        try:
            result = self._command_runner(
                ("git", "config", "--global", "--null", "--get-regexp", expression),
                cwd=str(cwd),
                env=build_git_signing_env(global_config=self._global_config),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0 or not isinstance(result.stdout, str):
            return None
        values = self._parse(result.stdout)
        if values is None:
            return None
        signing_key = self._validated_key(values["user.signingkey"])
        signing_program = shutil.which("ssh-keygen", path=os.defpath)
        if signing_key is None or signing_program is None:
            return None
        environment = build_git_signing_env()
        injected = {
            **values,
            "user.signingkey": str(signing_key),
            "commit.gpgsign": "true",
            "gpg.ssh.program": str(Path(signing_program).resolve()),
        }
        environment["GIT_CONFIG_COUNT"] = str(len(injected))
        for index, (key, value) in enumerate(injected.items()):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
        return environment

    @classmethod
    def _parse(cls, raw: str) -> dict[str, str] | None:
        values: dict[str, str] = {}
        for entry in raw.split("\0"):
            if not entry:
                continue
            key, separator, value = entry.partition("\n")
            if not separator or key not in cls._KEYS or key in values:
                return None
            values[key] = value
        if set(values) != set(cls._KEYS) or values["gpg.format"] != "ssh":
            return None
        return values

    @staticmethod
    def _validated_key(value: str) -> Path | None:
        try:
            path = Path(value).expanduser()
            resolved = path.resolve(strict=True)
            mode = resolved.stat().st_mode
        except (OSError, RuntimeError, ValueError):
            return None
        if not path.is_absolute() or path.is_symlink() or not resolved.is_file() or mode & 0o022:
            return None
        return resolved


@dataclass(frozen=True, slots=True)
class WorkerCapabilities:
    """Explicit host capabilities available to one worker-pool instance."""

    quota_backend: QuotaBackend | None
    execution_boundary_id: str
    signing_provider: GitSigningProvider | None = None
    preflight_cache: ProcessLocalPreflightCache = field(default_factory=ProcessLocalPreflightCache)
    receipt_store_factory: Callable[[Path], HostCapabilityReceiptStore] = HostCapabilityReceiptStore

    def __post_init__(self) -> None:
        """Reject an empty or overlong execution-boundary identifier."""
        if not self.execution_boundary_id or len(self.execution_boundary_id) > 128:
            raise ValueError("execution boundary id is required")


__all__ = [
    "QUOTA_ATTACH_FAILED_TOKEN",
    "QUOTA_AVAILABLE_TOKEN",
    "QUOTA_BACKEND_NOT_APPLICABLE_TOKEN",
    "QUOTA_CREATE_FAILED_TOKEN",
    "QUOTA_DETACH_FAILED_TOKEN",
    "QUOTA_UNAVAILABLE_TOKEN",
    "RECEIPT_FAILED_TOKEN",
    "CapabilityReceiptTarget",
    "CapabilityRequestTarget",
    "FakeGitSigningProvider",
    "GitSigningProvider",
    "HdiutilQuotaBackend",
    "HostCapabilityError",
    "HostCapabilityReceipt",
    "HostCapabilityReceiptStore",
    "ProcessLocalPreflightCache",
    "ProductionGitSigningProvider",
    "QuotaBackend",
    "WorkerCapabilities",
    "bind_receipt_target",
]
