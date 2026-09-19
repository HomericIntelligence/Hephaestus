"""Provide host capability adapters outside the pure pipeline contracts."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, replace
from pathlib import Path

import hephaestus.automation.pipeline.host_verification_pyxis as host_verification_pyxis
from hephaestus.automation.direct_review_recovery import _write_receipt
from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.automation.pipeline.diagnostics import redact_diagnostic_text
from hephaestus.automation.pipeline.host_capabilities import (
    QUOTA_AVAILABLE_TOKEN,
    CapabilityDeadline,
    CapabilityReceiptTarget,
    CapabilityRequestTarget,
    HostCapabilityReceipt,
    QuotaBackend,
    SigningConfigurationError,
)
from hephaestus.automation.pipeline.jobs import JobResult
from hephaestus.automation.pyxis_artifact_io import CrossNodePathBinding
from hephaestus.automation.source_worktree import _PreparationDeadline
from hephaestus.config.child_environments import read_approved_parent_env

_DIAGNOSTIC_MAX = 4_000
_RECEIPT_MAX = 65_536

CommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]
HostProbe = Callable[[], tuple[str, bool]]


class QuotaLifecycleError(RuntimeError):
    """Preserve the primary quota failure and a separate cleanup failure."""

    def __init__(
        self,
        token: str,
        *,
        step: str = "backend",
        result: subprocess.CompletedProcess[bytes] | None = None,
        error: BaseException | None = None,
    ) -> None:
        """Keep bounded primary and cleanup evidence on one failure."""
        super().__init__(token)
        self.step = step
        self.result = result
        self.original_error = error
        self.cleanup_error = ""
        self.retained_root = ""


def _hdiutil_host() -> tuple[str, bool]:
    """Read the platform and fixed executable at the host boundary."""
    binary = Path("/usr/bin/hdiutil")
    return sys.platform, binary.is_file() and os.access(binary, os.X_OK)


def hdiutil_create_argv(image: Path, maximum_bytes: int) -> tuple[str, ...]:
    """Return the existing fixed-size quota image command."""
    return (
        "/usr/bin/hdiutil",
        "create",
        "-size",
        f"{maximum_bytes // (1024 * 1024)}m",
        "-fs",
        "HFS+",
        str(image),
    )


def _quota_path_identity(root: Path, mountpoint: Path) -> tuple[int, int, int, int]:
    """Bind physical host directories before commands can use their paths."""
    if (
        root.resolve(strict=True) != root
        or mountpoint.resolve(strict=True) != mountpoint
        or not mountpoint.is_relative_to(root)
    ):
        raise ValueError("The quota directory path is not confined.")
    root_info, mount_info = root.stat(), mountpoint.stat()
    if not stat.S_ISDIR(root_info.st_mode) or not stat.S_ISDIR(mount_info.st_mode):
        raise ValueError("The quota path is not a directory.")
    return root_info.st_dev, root_info.st_ino, mount_info.st_dev, mount_info.st_ino


def _quota_command(
    run: CommandRunner,
    argv: tuple[str, ...],
    step: str,
    timeout_s: float,
    error_type: type[QuotaLifecycleError],
) -> subprocess.CompletedProcess[bytes]:
    """Keep bounded diagnostics for one fixed host command."""
    token = (
        "host_verification_quota_cleanup_failed"
        if step == "detach"
        else f"host_verification_quota_{step}_failed"
    )
    try:
        result = run(
            argv,
            capture_output=True,
            timeout=timeout_s,
            check=False,
            env=read_approved_parent_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise error_type(token, step=step, error=error) from error
    if result.returncode != 0:
        raise error_type(token, step=step, result=result)
    return result


def _detach_quota(
    invoke: Callable[[tuple[str, ...], str], None],
    mountpoint: Path,
) -> QuotaLifecycleError | None:
    """Keep the existing two bounded forced-detach attempts."""
    failure = None
    for _attempt in range(2):
        try:
            invoke(("/usr/bin/hdiutil", "detach", "-force", str(mountpoint)), "detach")
            return None
        except QuotaLifecycleError as error:
            failure = error
    return failure


def _verify_detached_quota(
    root: Path,
    mountpoint: Path,
    expected: tuple[int, int, int, int],
    error_type: type[QuotaLifecycleError],
) -> None:
    """Require the original directory identity after a successful detach."""
    try:
        if _quota_path_identity(root, mountpoint) != expected:
            raise ValueError("The quota mount is still present.")
    except (OSError, ValueError) as error:
        raise error_type(
            "host_verification_quota_cleanup_failed", step="detach", error=error
        ) from error


def _require_quota_backend(
    host_probe: HostProbe | None, error_type: type[QuotaLifecycleError]
) -> None:
    """Reject an unsupported host before a quota command starts."""
    platform, executable = (host_probe or _hdiutil_host)()
    if platform != "darwin":
        raise error_type("host_verification_quota_backend_not_applicable")
    if not executable:
        raise error_type("host_verification_quota_unavailable")


def _attached_quota_identity(
    root: Path,
    mountpoint: Path,
    expected: tuple[int, int, int, int],
    primary: BaseException | None,
    error_type: type[QuotaLifecycleError],
) -> tuple[int, int, int, int]:
    """Inspect attachment without replacing an earlier command failure."""
    try:
        observed = _quota_path_identity(root, mountpoint)
        if observed[:2] != expected[:2]:
            raise ValueError("The quota root identity changed after attach.")
        return observed
    except (OSError, ValueError) as error:
        if primary is None:
            raise error_type(
                "host_verification_quota_attach_failed", step="attach", error=error
            ) from error
        if isinstance(primary, QuotaLifecycleError):
            primary.retained_root = str(root)
            primary.cleanup_error = _tail(str(error))
        else:
            primary.add_note(_tail(str(error)))
        return expected


@contextmanager
def quota_backed_volume(
    root: Path,
    image_name: str,
    mountpoint: Path,
    *,
    maximum_bytes: int = 512 * 1024 * 1024,
    timeout_s: int = 30,
    command_runner: CommandRunner | None = None,
    host_probe: HostProbe | None = None,
    error_type: type[QuotaLifecycleError] = QuotaLifecycleError,
    deadline: CapabilityDeadline | None = None,
    expected_identity: tuple[int, int, int, int] | None = None,
) -> Iterator[Path]:
    """Own the shared create, attach, and detach lifecycle for quota volumes."""
    _require_quota_backend(host_probe, error_type)
    if Path(image_name).name != image_name or image_name in {"", ".", ".."}:
        raise error_type("host_verification_quota_unavailable")
    identity = _quota_path_identity(root, mountpoint)
    if expected_identity is not None and identity != expected_identity:
        error = error_type("host_verification_quota_unavailable", step="backend")
        error.retained_root = str(root)
        raise error
    unmounted_identity = identity
    image = root / image_name
    if image.is_symlink():
        raise error_type("host_verification_quota_unavailable")
    run = command_runner or subprocess.run
    command_deadline = deadline

    def invoke(argv: tuple[str, ...], step: str) -> None:
        token = (
            "host_verification_quota_cleanup_failed"
            if step == "detach"
            else f"host_verification_quota_{step}_failed"
        )
        try:
            observed = _quota_path_identity(root, mountpoint)
            command_timeout = (
                min(float(timeout_s), command_deadline.remaining())
                if command_deadline is not None
                else float(timeout_s)
            )
        except (OSError, RuntimeError, ValueError) as error:
            raise error_type(token, step=step, error=error) from error
        allowed = (identity, unmounted_identity) if step == "detach" else (identity,)
        if observed not in allowed:
            raise error_type(token, step=step)
        _quota_command(run, argv, step, command_timeout, error_type)
        if step == "detach":
            _verify_detached_quota(root, mountpoint, unmounted_identity, error_type)

    invoke(hdiutil_create_argv(image, maximum_bytes), "create")
    primary: BaseException | None = None
    try:
        try:
            invoke(
                (
                    "/usr/bin/hdiutil",
                    "attach",
                    "-nobrowse",
                    "-mountpoint",
                    str(mountpoint),
                    str(image),
                ),
                "attach",
            )
        except BaseException as error:
            primary = error
            raise
        finally:
            identity = _attached_quota_identity(
                root, mountpoint, unmounted_identity, primary, error_type
            )
        yield mountpoint
    except BaseException as error:
        primary = error
        raise
    finally:
        # Cleanup shares one fixed budget across both attempts after a stop request.
        command_deadline = _PreparationDeadline(
            time.monotonic() + min(float(timeout_s), 30.0), time.monotonic
        )
        cleanup = _detach_quota(invoke, mountpoint)
        if cleanup is not None:
            if isinstance(primary, QuotaLifecycleError):
                primary.retained_root = str(root)
                primary.cleanup_error = _tail(
                    primary.cleanup_error
                    + "\n"
                    + _tail(
                        cleanup.result.stderr
                        if cleanup.result is not None
                        else str(cleanup.original_error or cleanup)
                    )
                ).strip()
            else:
                cleanup.retained_root = str(root)
                raise cleanup from primary


def _new_probe_directory(
    target: CapabilityRequestTarget,
) -> tuple[Path, tuple[int, int, int, int]]:
    """Create one private probe directory below verified no-follow parents."""
    root = target.repository_root
    path = root / "build" / ".host-verification" / target.request_id
    request_created = False
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        for name in ("build", ".host-verification"):
            with suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=descriptor)
            child = os.open(name, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            if name == ".host-verification":
                _private_entry(os.fstat(descriptor), directory=True)
            else:
                _protected_parent(os.fstat(descriptor))
        os.mkdir(target.request_id, 0o700, dir_fd=descriptor)
        request_created = True
        child = os.open(target.request_id, flags, dir_fd=descriptor)
        os.close(descriptor)
        descriptor = child
        request_info = os.fstat(descriptor)
        _private_entry(request_info, directory=True)
        os.mkdir("mount", 0o700, dir_fd=descriptor)
        mount = os.open("mount", flags, dir_fd=descriptor)
        try:
            mount_info = os.fstat(mount)
            _private_entry(mount_info, directory=True)
            identity = (
                request_info.st_dev,
                request_info.st_ino,
                mount_info.st_dev,
                mount_info.st_ino,
            )
        finally:
            os.close(mount)
    except (OSError, ValueError) as error:
        failure = QuotaLifecycleError(
            "host_verification_quota_unavailable", step="backend", error=error
        )
        if request_created:
            failure.retained_root = str(path)
        raise failure from error
    finally:
        os.close(descriptor)
    return path, identity


def _remove_probe_directory(root: Path, identity: tuple[int, int, int, int]) -> None:
    """Remove only this probe's fixed artifacts after confirmed detach."""
    if _quota_path_identity(root, root / "mount") != identity:
        raise ValueError("The quota directory identity changed before cleanup.")
    image = root / "preflight.dmg"
    if image.is_symlink():
        raise ValueError("The quota image path changed before cleanup.")
    image.unlink(missing_ok=True)
    (root / "mount").rmdir()
    root.rmdir()


def _protected_parent(info: os.stat_result) -> None:
    """Accept owned shared directories only without shared write access."""
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError("The capability parent directory permissions are unsafe.")


def _private_entry(info: os.stat_result, *, directory: bool) -> None:
    """Reject public or foreign state entries before use."""
    correct_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not correct_kind
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or (not directory and info.st_nlink != 1)
    ):
        raise ValueError("The capability receipt entry is not private and owned.")


@contextmanager
def _receipt_directory(
    root: Path, *, create: bool = True
) -> Iterator[tuple[Path, int, tuple[tuple[int, int], ...]]]:
    """Keep receipt access below no-follow directory descriptors."""
    if root.resolve(strict=True) != root:
        raise ValueError("The capability repository root is not canonical.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    path = root
    try:
        info = os.fstat(descriptor)
        identities = [(info.st_dev, info.st_ino)]
        for component in (*Path(DEFAULT_STATE_DIR).parts, "host-capability-receipts"):
            if create:
                with suppress(FileExistsError):
                    os.mkdir(component, 0o700, dir_fd=descriptor)
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            path /= component
            info = os.fstat(descriptor)
            identities.append((info.st_dev, info.st_ino))
            if component != "build":
                _private_entry(info, directory=True)
            else:
                _protected_parent(info)
        yield path, descriptor, tuple(identities)
    finally:
        os.close(descriptor)


def _require_receipt_namespace(root: Path, identities: tuple[tuple[int, int], ...]) -> None:
    """Require the original directory identities at their live paths."""
    with _receipt_directory(root, create=False) as (_, _, current):
        if current != identities:
            raise ValueError("The capability receipt directory identity changed.")


def _require_receipt_lock(descriptor: int, lock: int) -> None:
    """Require the held lock to identify the live private lock file."""
    current = os.stat("receipts.lock", dir_fd=descriptor, follow_symlinks=False)
    _private_entry(current, directory=False)
    if not os.path.samestat(os.fstat(lock), current):
        raise ValueError("The capability receipt lock identity changed.")


@contextmanager
def _receipt_lock(descriptor: int, deadline: CapabilityDeadline) -> Iterator[None]:
    """Serialize receipt replacement through an owned no-follow lock."""
    import fcntl

    lock = os.open(
        "receipts.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=descriptor
    )
    try:
        _private_entry(os.fstat(lock), directory=False)
        while True:
            deadline.remaining()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                time.sleep(min(0.05, deadline.remaining()))
            else:
                break
        try:
            _require_receipt_lock(descriptor, lock)
            yield
            _require_receipt_lock(descriptor, lock)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        os.close(lock)


def _receipt_readback(descriptor: int, name: str, expected: str) -> None:
    """Require exact bounded readback from one private regular file."""
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
    try:
        _private_entry(os.fstat(handle), directory=False)
        data = bytearray()
        while len(data) <= _RECEIPT_MAX:
            chunk = os.read(handle, min(8192, _RECEIPT_MAX + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if bytes(data) != expected.encode("utf-8"):
            raise ValueError("The capability receipt readback does not match.")
    finally:
        os.close(handle)


def _persist_receipt(
    target: CapabilityReceiptTarget,
    receipt: HostCapabilityReceipt,
    deadline: CapabilityDeadline,
) -> None:
    """Persist one bounded target and result before it can authorize execution."""
    replace(target)
    replace(receipt)
    if receipt.target != target:
        raise ValueError("The capability receipt target changed.")
    root = target.canonical_repository_root
    payload = {
        "schema_version": 1,
        "target": asdict(target),
        "receipt": asdict(receipt),
    }
    content = json.dumps(payload, default=str, sort_keys=True) + "\n"
    if len(content.encode("utf-8")) > _RECEIPT_MAX:
        raise ValueError("The capability receipt is too large.")
    with (
        _receipt_directory(root) as (directory, descriptor, identities),
        _receipt_lock(descriptor, deadline),
    ):
        name = f"{receipt.receipt_id}.json"
        try:
            existing = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _private_entry(existing, directory=False)
        deadline.remaining()
        _require_receipt_namespace(root, identities)
        _write_receipt(directory, descriptor, directory / name, content)
        _receipt_readback(descriptor, name, content)
        _require_receipt_namespace(root, identities)
        deadline.remaining()


def _tail(value: bytes | str | None) -> str:
    """Return a redacted bounded diagnostic tail."""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    return redact_diagnostic_text(text)[-_DIAGNOSTIC_MAX:]


@contextmanager
def _capability_cache_lock(lock: threading.Lock, deadline: CapabilityDeadline) -> Iterator[None]:
    """Bound cache admission by the same deadline and cancellation check."""
    while not lock.acquire(timeout=min(0.05, deadline.remaining())):
        pass
    try:
        deadline.remaining()
        yield
    finally:
        lock.release()


class _QuotaBackend:
    """Keep receipt storage and process-local cache ownership in one place."""

    backend_id: str

    def __init__(
        self,
        execution_boundary_id: str | None = None,
    ) -> None:
        """Bind the cache to this provider instance and execution boundary."""
        self._boundary = execution_boundary_id
        self._cache: dict[tuple[Path, int, str, str, str, int], HostCapabilityReceipt] = {}
        self._lock = threading.Lock()

    def preflight(
        self, target: CapabilityReceiptTarget, *, deadline: CapabilityDeadline | None = None
    ) -> HostCapabilityReceipt:
        """Bind each request to a durable result from this process's quota probe."""
        replace(target)
        operation_deadline = deadline or _PreparationDeadline(
            time.monotonic() + 90.0, time.monotonic
        )
        receipt: HostCapabilityReceipt | None = None
        with _capability_cache_lock(self._lock, operation_deadline):
            try:
                root = target.canonical_repository_root
                if (
                    root.resolve(strict=True) != root
                    or root.stat().st_dev != target.root_device
                    or target.backend != self.backend_id
                    or (
                        self._boundary is not None
                        and target.execution_boundary_id != self._boundary
                    )
                ):
                    raise ValueError("The capability repository root is not canonical.")
                key = (
                    root,
                    root.stat().st_dev,
                    target.request.capability,
                    self.backend_id,
                    target.execution_boundary_id,
                    os.getpid(),
                )
                probe = self._cache.get(key)
                if probe is None:
                    receipt = self._probe(target, operation_deadline)
                else:
                    receipt = replace(
                        probe,
                        purpose=target.request.purpose,
                        target=target,
                        receipt_id=uuid.uuid4().hex,
                        cached=True,
                        probe_receipt_id=probe.receipt_id,
                    )
                _persist_receipt(target, receipt, operation_deadline)
                if probe is None:
                    self._cache[key] = receipt
                return receipt
            except (OSError, RuntimeError, ValueError) as error:
                if receipt is not None:
                    token = (
                        "host_verification_quota_receipt_storage_failed"
                        if receipt.available
                        else receipt.token
                    )
                    return replace(
                        receipt,
                        available=False,
                        token=token,
                        failed_step="storage" if receipt.available else receipt.failed_step,
                        persistence_error=_tail(str(error)),
                        persistence_exception_type=type(error).__name__,
                    )
                return self._failure(
                    target,
                    uuid.uuid4().hex,
                    "host_verification_quota_receipt_storage_failed",
                    "storage",
                    error=error,
                )

    def _probe(
        self, target: CapabilityReceiptTarget, deadline: CapabilityDeadline
    ) -> HostCapabilityReceipt:
        """Require a concrete platform owner before a probe can succeed."""
        raise NotImplementedError

    @staticmethod
    def _failure(
        target: CapabilityReceiptTarget,
        receipt_id: str,
        token: str,
        step: str,
        *,
        result: subprocess.CompletedProcess[bytes] | None = None,
        error: BaseException | None = None,
    ) -> HostCapabilityReceipt:
        """Create one bounded typed failure receipt."""
        if result is None and isinstance(error, subprocess.TimeoutExpired):
            stdout, stderr = error.stdout, error.stderr
        else:
            stdout = None if result is None else result.stdout
            stderr = None if result is None else result.stderr
        return HostCapabilityReceipt(
            available=False,
            token=token,
            failed_step=step,
            purpose=target.request.purpose,
            target=target,
            cleanup_state="not_started",
            receipt_id=receipt_id,
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(stderr),
            return_code=None if result is None else result.returncode,
            operating_system_error=_tail(str(error)) if error is not None else "",
            exception_type="" if error is None else type(error).__name__,
        )


class UnavailableQuotaBackend(_QuotaBackend):
    """Persist an unavailable-provider result through the shared receipt owner."""

    backend_id = "unavailable"

    def _probe(
        self, target: CapabilityReceiptTarget, deadline: CapabilityDeadline
    ) -> HostCapabilityReceipt:
        """Return failure without a host command or source execution."""
        deadline.remaining()
        return self._failure(
            target, uuid.uuid4().hex, "host_verification_quota_unavailable", "backend"
        )


class HdiutilQuotaBackend(_QuotaBackend):
    """Use the shared macOS volume owner to establish quota availability."""

    backend_id = "hdiutil-v1"

    def __init__(
        self,
        command_runner: CommandRunner | None = None,
        *,
        execution_boundary_id: str | None = None,
        host_probe: HostProbe | None = None,
    ) -> None:
        """Supply the existing host command boundary and process cache."""
        super().__init__(execution_boundary_id)
        self._command_runner = command_runner or subprocess.run
        self._host_probe = host_probe or _hdiutil_host

    def _probe(
        self, target: CapabilityReceiptTarget, deadline: CapabilityDeadline
    ) -> HostCapabilityReceipt:
        """Use the execution owner's complete quota lifecycle for one probe."""
        receipt_id = uuid.uuid4().hex
        root: Path | None = None
        retained = False
        cleanup_error: OSError | ValueError | None = None
        try:
            root, identity = _new_probe_directory(target.request)
            with quota_backed_volume(
                root,
                "preflight.dmg",
                root / "mount",
                command_runner=self._command_runner,
                host_probe=self._host_probe,
                deadline=deadline,
                expected_identity=identity,
            ):
                pass
            receipt = HostCapabilityReceipt(
                True,
                QUOTA_AVAILABLE_TOKEN,
                None,
                target.request.purpose,
                receipt_id,
                target=target,
                cleanup_state="complete",
            )
        except QuotaLifecycleError as error:
            retained = bool(error.retained_root)
            token = (
                "host_verification_quota_detach_failed" if error.step == "detach" else str(error)
            )
            receipt = self._failure(
                target,
                receipt_id,
                token,
                error.step,
                result=error.result,
                error=error.original_error,
            )
            receipt = replace(
                receipt,
                retained_root=error.retained_root,
                cleanup_state=(
                    "retained"
                    if retained
                    else "complete"
                    if error.step in {"attach", "detach"}
                    else "not_started"
                ),
                operating_system_error=_tail(
                    receipt.operating_system_error + "\n" + error.cleanup_error
                ).strip(),
            )
        except (OSError, ValueError) as error:
            receipt = self._failure(
                target,
                receipt_id,
                "host_verification_quota_unavailable",
                "backend",
                error=error,
            )
        finally:
            if root is not None and not retained:
                try:
                    _remove_probe_directory(root, identity)
                except (OSError, ValueError) as error:
                    cleanup_error = error
        if cleanup_error is not None:
            receipt = replace(
                receipt,
                available=False,
                token="host_verification_quota_unavailable" if receipt.available else receipt.token,
                failed_step="backend" if receipt.available else receipt.failed_step,
                retained_root=str(root),
                cleanup_state="retained",
                operating_system_error=_tail(
                    receipt.operating_system_error + "\n" + str(cleanup_error)
                ).strip(),
                exception_type=receipt.exception_type or type(cleanup_error).__name__,
            )
        return receipt


class PyxisQuotaBackend(_QuotaBackend):
    """Bind the configured Linux quota owner without an hdiutil dependency."""

    backend_id = "pyxis-v1"

    def __init__(
        self,
        *,
        image: Path,
        image_sha256: str,
        authority: Path,
        quota_root: Path,
        runtime_available: Callable[[], bool],
        execution_boundary_id: str,
    ) -> None:
        """Keep host configuration separate from source-owned request data."""
        super().__init__(execution_boundary_id)
        self._image = image
        self._image_sha256 = image_sha256
        self._authority = authority
        self._quota_root = quota_root
        self._runtime_available = runtime_available

    def _probe(
        self, target: CapabilityReceiptTarget, deadline: CapabilityDeadline
    ) -> HostCapabilityReceipt:
        """Reuse the runtime, image, and retained hard-quota owners on Linux."""
        receipt_id = uuid.uuid4().hex
        try:
            deadline.remaining()
            if not self._runtime_available():
                raise ValueError("The configured Pyxis runtime is unavailable.")
            deadline.remaining()
            host_verification_pyxis.validate_pyxis_image(
                self._image, expected_sha256=self._image_sha256, provenance=self._authority
            )
            deadline.remaining()
            binding = host_verification_pyxis.validate_pyxis_quota_root(
                self._quota_root, retain_binding=True
            )
            if not isinstance(binding, CrossNodePathBinding):
                raise ValueError("The quota owner did not retain its filesystem binding.")
            try:
                deadline.remaining()
                binding.revalidate()
            finally:
                binding.close()
            return HostCapabilityReceipt(
                True,
                QUOTA_AVAILABLE_TOKEN,
                None,
                target.request.purpose,
                receipt_id,
                target=target,
                cleanup_state="complete",
            )
        except (OSError, RuntimeError, ValueError) as error:
            return self._failure(
                target, receipt_id, "host_verification_quota_unavailable", "backend", error=error
            )


def select_quota_backend(
    *,
    image: Path,
    image_sha256: str | None,
    authority: Path | None,
    quota_root: Path | None,
    runtime_available: Callable[[], bool],
    execution_boundary_id: str,
) -> QuotaBackend | None:
    """Select only a supported backend with explicit host configuration."""
    if sys.platform == "darwin":
        return HdiutilQuotaBackend(execution_boundary_id=execution_boundary_id)
    if (
        sys.platform == "linux"
        and image_sha256
        and authority is not None
        and quota_root is not None
    ):
        return PyxisQuotaBackend(
            image=image,
            image_sha256=image_sha256,
            authority=authority,
            quota_root=quota_root,
            runtime_available=runtime_available,
            execution_boundary_id=execution_boundary_id,
        )
    return None


class ValidatedSigningProvider:
    """Delegate signed Git admission to the existing controlled validator."""

    def __init__(self, validator: Callable[..., dict[str, str] | JobResult]) -> None:
        """Accept the host-owned signing validator explicitly."""
        self._validator = validator

    def environment(
        self, cwd: Path, *, timeout: int, private_metadata: bool = False
    ) -> dict[str, str]:
        """Keep the validator's failure cause and return only a controlled environment."""
        result = self._validator(cwd, timeout=timeout, private_metadata=private_metadata)
        if isinstance(result, JobResult):
            raise SigningConfigurationError(_tail(result.error or "Signing is unavailable."))
        if not isinstance(result, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in result.items()
        ):
            raise SigningConfigurationError("The signing environment is invalid.")
        return dict(result)
