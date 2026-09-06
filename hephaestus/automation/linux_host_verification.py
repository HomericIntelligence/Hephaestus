"""Linux host-verification backend configuration.

This module owns the host-side configuration boundary for the optional Linux
verification backend. Scheduler submission and verifier execution are added in
later increments. Configuration parsing is deliberately closed so an
unreviewed setting cannot widen the host execution boundary.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
import tomllib
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from hephaestus.automation.linux_host_verification_contract import (
    MAX_TIMEOUT_SECONDS,
    LinuxHostVerificationReceipt,
    LinuxHostVerificationRequest,
)
from hephaestus.automation.pipeline.job_results import JobResult

_CONFIG_FIELDS = frozenset(
    {
        "shared_root",
        "image_path",
        "image_manifest_path",
        "trusted_slurm_bin_dir",
        "timeout_seconds",
    }
)
_CONFIG_TABLE = "linux_host_verification"
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
MAX_STAGED_ARCHIVE_BYTES = 1 << 30
"""The largest immutable archive accepted for one Linux verification lease."""


def _absolute_path(value: object, field_name: str) -> str:
    """Return one canonical, non-root absolute path or raise ``ValueError``."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field_name} is invalid")
    if value == "/" or value.startswith("//") or not os.path.isabs(value):
        raise ValueError(f"{field_name} must be an absolute non-root path")
    if value.endswith("/") or os.path.normpath(value) != value:
        raise ValueError(f"{field_name} must be canonical")
    return value


def _outside(path: str, boundary: str, field_name: str) -> None:
    """Reject a path that is equal to or nested under a writable boundary."""
    if os.path.commonpath((path, boundary)) == boundary:
        raise ValueError(f"{field_name} must be outside shared_root")


def _absolute_ancestry(path: str) -> tuple[str, ...]:
    """Return the root-to-leaf path components for one absolute path."""
    current = Path("/")
    ancestors = [str(current)]
    for part in Path(path).parts[1:]:
        current /= part
        ancestors.append(str(current))
    return tuple(ancestors)


def load_linux_host_verification_config(config_path: Path) -> LinuxHostVerificationConfig:
    """Load one sealed Linux backend configuration from a TOML file.

    The operator-controlled file is a security boundary. It must be an
    absolute, non-symlink regular file that group and other users cannot
    modify. The file contains exactly one named table, so unrelated settings
    cannot silently affect backend behavior.
    """
    if not isinstance(config_path, Path) or not config_path.is_absolute():
        raise ValueError("Linux host-verification config path must be absolute")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(config_path, flags)
    except OSError as error:
        raise ValueError("Linux host-verification config cannot be opened") from error
    try:
        file_status = os.fstat(descriptor)
        if not stat.S_ISREG(file_status.st_mode) or file_status.st_mode & 0o022:
            raise ValueError("Linux host-verification config file is unsafe")
        with os.fdopen(descriptor, "rb", closefd=False) as config_file:
            parsed = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError("Linux host-verification config is invalid") from error
    finally:
        os.close(descriptor)
    if set(parsed) != {_CONFIG_TABLE}:
        raise ValueError("Linux host-verification config tables are invalid")
    table = parsed[_CONFIG_TABLE]
    if not isinstance(table, dict):
        raise ValueError("Linux host-verification config table is invalid")
    return LinuxHostVerificationConfig.from_mapping(table)


def linux_host_verification_job_result(
    request: LinuxHostVerificationRequest,
    receipt: LinuxHostVerificationReceipt,
) -> JobResult:
    """Convert one request-bound Linux allocation receipt into ``JobResult``.

    A receipt is untrusted until every identity and digest agrees with its
    request. A mismatched receipt exposes no diagnostic text because it could
    belong to another allocation. A valid failed receipt retains only the
    contract-bounded diagnostic tails.
    """
    try:
        receipt.validate_against_request(request)
    except ValueError:
        return JobResult(ok=False, error="linux_host_verification_receipt_invalid")
    value: dict[str, object] = {
        "head_sha": request.expected_head_sha,
        "immutable_source": True,
        "backend": receipt.backend,
        "command_id": receipt.command_id,
        "allocation_job_id": receipt.allocation_job_id,
        "allocation_hostname": receipt.allocation_hostname,
        "failure_kind": receipt.failure_classification,
        "status": receipt.outcome,
    }
    if receipt.outcome == "passed":
        return JobResult(
            ok=True,
            value=value,
            stdout_tail=receipt.stdout_tail,
            stderr_tail=receipt.stderr_tail,
        )
    return JobResult(
        ok=False,
        value=value,
        error=f"linux_host_verification_{receipt.failure_classification}",
        stdout_tail=receipt.stdout_tail,
        stderr_tail=receipt.stderr_tail,
    )


@dataclass(frozen=True)
class LinuxHostVerificationConfig:
    """Validated static configuration for the Linux verification backend."""

    shared_root: str
    image_path: str
    image_manifest_path: str
    trusted_slurm_bin_dir: str
    timeout_seconds: int

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> LinuxHostVerificationConfig:
        """Parse the one supported configuration schema and reject extra data."""
        if set(payload) != _CONFIG_FIELDS:
            raise ValueError("Linux host-verification configuration fields are invalid")
        shared_root = _absolute_path(payload["shared_root"], "shared_root")
        image_path = _absolute_path(payload["image_path"], "image_path")
        image_manifest_path = _absolute_path(
            payload["image_manifest_path"], "image_manifest_path"
        )
        trusted_slurm_bin_dir = _absolute_path(
            payload["trusted_slurm_bin_dir"], "trusted_slurm_bin_dir"
        )
        timeout_seconds = payload["timeout_seconds"]
        if (
            type(timeout_seconds) is not int
            or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds is invalid")
        if len({shared_root, image_path, image_manifest_path, trusted_slurm_bin_dir}) != 4:
            raise ValueError("Linux host-verification configuration paths must be distinct")
        _outside(image_path, shared_root, "image_path")
        _outside(image_manifest_path, shared_root, "image_manifest_path")
        _outside(trusted_slurm_bin_dir, shared_root, "trusted_slurm_bin_dir")
        return cls(
            shared_root=shared_root,
            image_path=image_path,
            image_manifest_path=image_manifest_path,
            trusted_slurm_bin_dir=trusted_slurm_bin_dir,
            timeout_seconds=timeout_seconds,
        )

    def trusted_slurm_executable(self, executable_name: str) -> str:
        """Return a verified absolute ``sbatch`` or ``srun`` executable path.

        Every component from the filesystem root through the executable must
        be root-owned, non-writable by group or other users, and free of
        symlinks. This prevents configuration or path traversal from selecting
        a scheduler binary outside the reviewed trust boundary.
        """
        if executable_name not in {"sbatch", "srun"}:
            raise ValueError("Slurm executable name is not trusted")
        executable_path = f"{self.trusted_slurm_bin_dir}/{executable_name}"
        ancestors = _absolute_ancestry(executable_path)
        for index, path in enumerate(ancestors):
            try:
                file_status = os.lstat(path)
            except OSError as error:
                raise ValueError("trusted Slurm executable path is unavailable") from error
            if file_status.st_uid != 0 or file_status.st_mode & 0o022:
                raise ValueError("trusted Slurm executable path ownership is unsafe")
            if stat.S_ISLNK(file_status.st_mode):
                raise ValueError("trusted Slurm executable path must not contain symlinks")
            is_leaf = index == len(ancestors) - 1
            if is_leaf:
                if not stat.S_ISREG(file_status.st_mode) or file_status.st_mode & 0o111 == 0:
                    raise ValueError("trusted Slurm executable is invalid")
            elif not stat.S_ISDIR(file_status.st_mode):
                raise ValueError("trusted Slurm executable ancestor is not a directory")
        return executable_path


@dataclass(frozen=True)
class LinuxHostVerificationRun:
    """Private shared-filesystem paths for one Linux verification lease."""

    run_id: str
    root: Path
    source_archive_path: Path
    source_extract_path: Path
    git_metadata_archive_path: Path
    git_metadata_extract_path: Path
    scratch_path: Path
    stdout_path: Path
    stderr_path: Path
    request_path: Path
    receipt_path: Path


def prepare_linux_host_verification_run(
    config: LinuxHostVerificationConfig, run_id: str
) -> LinuxHostVerificationRun:
    """Create one private, non-reusable shared-filesystem lease layout."""
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("Linux host-verification run ID is invalid")
    shared_root = Path(config.shared_root)
    try:
        root_status = os.lstat(shared_root)
    except OSError as error:
        raise ValueError("Linux host-verification shared root is unavailable") from error
    if (
        not stat.S_ISDIR(root_status.st_mode)
        or stat.S_ISLNK(root_status.st_mode)
        or root_status.st_mode & 0o077
    ):
        raise ValueError("Linux host-verification shared root is unsafe")
    root = shared_root / run_id
    try:
        root.mkdir(mode=0o700)
        os.chmod(root, 0o700)
        source_extract_path = root / "source"
        git_metadata_extract_path = root / "git-metadata"
        scratch_path = root / "scratch"
        for directory in (source_extract_path, git_metadata_extract_path, scratch_path):
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
    except FileExistsError as error:
        raise ValueError("Linux host-verification run already exists") from error
    except OSError as error:
        raise ValueError("Linux host-verification run staging failed") from error
    return LinuxHostVerificationRun(
        run_id=run_id,
        root=root,
        source_archive_path=root / "source.tar",
        source_extract_path=source_extract_path,
        git_metadata_archive_path=root / "git-metadata.tar",
        git_metadata_extract_path=git_metadata_extract_path,
        scratch_path=scratch_path,
        stdout_path=root / "stdout.log",
        stderr_path=root / "stderr.log",
        request_path=root / "request.json",
        receipt_path=root / "receipt.json",
    )


def cleanup_linux_host_verification_run(
    config: LinuxHostVerificationConfig, run: LinuxHostVerificationRun
) -> None:
    """Remove only the exact private run directory that this config created."""
    if not isinstance(run, LinuxHostVerificationRun):
        raise ValueError("Linux host-verification run is invalid")
    expected_root = Path(config.shared_root) / run.run_id
    expected_paths = {
        "root": expected_root,
        "source_archive_path": expected_root / "source.tar",
        "source_extract_path": expected_root / "source",
        "git_metadata_archive_path": expected_root / "git-metadata.tar",
        "git_metadata_extract_path": expected_root / "git-metadata",
        "scratch_path": expected_root / "scratch",
        "stdout_path": expected_root / "stdout.log",
        "stderr_path": expected_root / "stderr.log",
        "request_path": expected_root / "request.json",
        "receipt_path": expected_root / "receipt.json",
    }
    if any(getattr(run, name) != path for name, path in expected_paths.items()):
        raise ValueError("Linux host-verification run paths are not exact")
    try:
        root_status = os.lstat(expected_root)
    except OSError as error:
        raise ValueError("Linux host-verification run is unavailable") from error
    if (
        not stat.S_ISDIR(root_status.st_mode)
        or stat.S_ISLNK(root_status.st_mode)
        or root_status.st_mode & 0o077
        or not shutil.rmtree.avoids_symlink_attacks
    ):
        raise ValueError("Linux host-verification run cleanup is unsafe")
    try:
        shutil.rmtree(expected_root)
    except OSError as error:
        raise ValueError("Linux host-verification run cleanup failed") from error


def stage_linux_host_verification_archive(
    run: LinuxHostVerificationRun, archive_kind: str, payload: bytes
) -> str:
    """Atomically stage one exact immutable archive and return its SHA-256."""
    destinations = {
        "source": run.source_archive_path,
        "git_metadata": run.git_metadata_archive_path,
    }
    destination = destinations.get(archive_kind)
    if destination is None or type(payload) is not bytes or len(payload) > MAX_STAGED_ARCHIVE_BYTES:
        raise ValueError("Linux host-verification archive is invalid")
    expected = run.root / destination.name
    if destination != expected:
        raise ValueError("Linux host-verification archive path is not exact")
    try:
        root_status = os.lstat(run.root)
        os.lstat(destination)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise ValueError("Linux host-verification archive path is unavailable") from error
    else:
        if not stat.S_ISDIR(root_status.st_mode) or stat.S_ISLNK(root_status.st_mode):
            raise ValueError("Linux host-verification archive root is unsafe")
        raise ValueError("Linux host-verification archive already exists")
    try:
        root_status = os.lstat(run.root)
    except OSError as error:
        raise ValueError("Linux host-verification archive root is unavailable") from error
    if (
        not stat.S_ISDIR(root_status.st_mode)
        or stat.S_ISLNK(root_status.st_mode)
        or root_status.st_mode & 0o077
    ):
        raise ValueError("Linux host-verification archive root is unsafe")
    descriptor = -1
    temporary_path = ""
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=run.root, text=False
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as archive_file:
            archive_file.write(payload)
            archive_file.flush()
            os.fsync(archive_file.fileno())
        os.link(temporary_path, destination, follow_symlinks=False)
        os.unlink(temporary_path)
        temporary_path = ""
    except OSError as error:
        raise ValueError("Linux host-verification archive staging failed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path:
            with suppress(OSError):
                os.unlink(temporary_path)
    return hashlib.sha256(payload).hexdigest()
