"""Trusted local Pyxis image and command helpers for Linux host checks."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from hephaestus.automation.pyxis_artifact_io import (
    PyxisArtifactIOError,
    effective_user_id,
    read_private_regular_file,
    stage_private_content_addressed_file,
    validate_private_capacity_root,
)

DEFAULT_HOST_VERIFICATION_PYXIS_IMAGE = Path("build/host-verification/hephaestus-ci.sqsh")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PYXIS_AUTHORITY_SCHEMA = "hephaestus-host-verification-pyxis-v2"
PYXIS_WRITABLE_FILESYSTEM_MAX_BYTES = 1024 * 1024 * 1024


class PyxisImageValidationError(ValueError):
    """Raised when a local Pyxis image cannot be trusted."""


@dataclass(frozen=True)
class PyxisImageMetadata:
    """Verified local squashfs metadata used by the worker and receipt."""

    path: Path
    sha256: str
    container_image_id: str
    container_image_reference: str
    containerfile_sha256: str
    source_revision: str
    container_runtime: str = "pyxis"


def validate_pyxis_image(
    image: Path,
    *,
    expected_sha256: str | None = None,
    provenance: Path | None = None,
) -> PyxisImageMetadata:
    """Validate one image against separate host-owned authority."""
    if expected_sha256 is None or provenance is None:
        raise PyxisImageValidationError("Pyxis image authority is missing")
    if not isinstance(expected_sha256, str) or _SHA256_RE.fullmatch(expected_sha256) is None:
        raise PyxisImageValidationError("Pyxis expected digest is invalid")
    resolved = _local_squashfs_image(image)
    authority = _read_authority(provenance)
    if authority["squashfs_sha256"] != expected_sha256:
        raise PyxisImageValidationError("Pyxis authority does not match expected digest")
    try:
        actual = image_sha256(resolved)
    except OSError as exc:
        raise PyxisImageValidationError("Pyxis image digest cannot be read") from exc
    if actual != expected_sha256:
        raise PyxisImageValidationError("Pyxis image digest does not match expected digest")
    return PyxisImageMetadata(
        path=resolved,
        sha256=actual,
        container_image_id=authority["container_image_id"],
        container_image_reference=authority["container_image_reference"],
        containerfile_sha256=authority["containerfile_sha256"],
        source_revision=authority["source_revision"],
    )


def _read_authority(path: Path) -> dict[str, str]:
    """Read one private host-owned image authority file."""
    try:
        value = json.loads(read_private_regular_file(path).decode("utf-8"))
    except (PyxisArtifactIOError, UnicodeError, json.JSONDecodeError) as exc:
        raise PyxisImageValidationError(f"Pyxis image authority is invalid: {exc}") from exc
    required = {
        "schema",
        "containerfile",
        "containerfile_sha256",
        "container_image_id",
        "container_image_reference",
        "source_revision",
        "squashfs_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise PyxisImageValidationError("Pyxis image authority has an invalid schema")
    if any(not isinstance(value[key], str) for key in required):
        raise PyxisImageValidationError("Pyxis image authority has invalid values")
    authority = {key: value[key] for key in required}
    image_id = authority["container_image_id"]
    if (
        authority["schema"] != PYXIS_AUTHORITY_SCHEMA
        or authority["containerfile"] != "ci/Containerfile"
        or _SHA256_RE.fullmatch(authority["containerfile_sha256"]) is None
        or _IMAGE_ID_RE.fullmatch(image_id) is None
        or authority["container_image_reference"]
        not in {f"podman://{image_id}", f"dockerd://{image_id}"}
        or _COMMIT_RE.fullmatch(authority["source_revision"]) is None
        or _SHA256_RE.fullmatch(authority["squashfs_sha256"]) is None
    ):
        raise PyxisImageValidationError("Pyxis image authority has invalid provenance")
    return authority


def _local_squashfs_image(image: Path) -> Path:
    """Return a verified local squashfs file for the Pyxis image."""
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", str(image)):
        raise PyxisImageValidationError("Pyxis image must be a local filesystem path")
    candidate = Path(image).expanduser()
    try:
        path_metadata = candidate.lstat()
        if (
            not stat.S_ISREG(path_metadata.st_mode)
            or path_metadata.st_uid != effective_user_id()
            or stat.S_IMODE(path_metadata.st_mode) & 0o277
        ):
            raise PyxisImageValidationError(
                "Pyxis image must be an owner-only, read-only regular local file"
            )
        resolved = candidate.resolve(strict=True)
        with resolved.open("rb") as stream:
            if stream.read(4) != b"hsqs":
                raise PyxisImageValidationError("Pyxis image is not a squashfs file")
    except PyxisImageValidationError:
        raise
    except OSError as exc:
        raise PyxisImageValidationError("Pyxis image is not a regular local file") from exc
    return resolved


def stage_verified_pyxis_image(
    image: PyxisImageMetadata, destination_root: Path
) -> PyxisImageMetadata:
    """Copy authorized bytes to one private content-addressed execution path."""
    try:
        target = stage_private_content_addressed_file(image.path, destination_root, image.sha256)
    except PyxisArtifactIOError as exc:
        raise PyxisImageValidationError(f"Pyxis image staging failed: {exc}") from exc
    return replace(image, path=target)


def build_pyxis_environment(*, source: Path, scratch: Path) -> dict[str, str]:
    """Return the scrubbed offline environment for the CI image."""
    source_path = source.expanduser().resolve()
    scratch_path = scratch.expanduser().resolve()
    temporary = scratch_path / "tmp"
    cache = scratch_path / "cache"
    return {
        "HOME": str((scratch_path / "home").resolve()),
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "XDG_CACHE_HOME": str(cache),
        "UV_CACHE_DIR": str((cache / "uv").resolve()),
        "UV_PROJECT_ENVIRONMENT": "/opt/hephaestus-venv",
        "UV_OFFLINE": "1",
        "UV_NO_SYNC": "1",
        "PYTHONPATH": str(source_path),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }


def build_pyxis_srun_command(
    *,
    image: PyxisImageMetadata,
    source: Path,
    git_metadata: Path,
    scratch: Path,
    pi_smoke_logs: Path,
    argv: tuple[str, ...],
    environment: Mapping[str, str],
    timeout_s: int,
) -> tuple[str, ...]:
    """Build one fixed ``srun`` command with Pyxis isolation flags."""
    if not argv:
        raise ValueError("host-verification argv cannot be empty")
    source_path = source.expanduser().resolve()
    metadata_path = git_metadata.expanduser().resolve()
    scratch_path = scratch.expanduser().resolve()
    logs_path = pi_smoke_logs.expanduser().resolve()
    container_argv = ("/usr/local/bin/uv", *argv[1:]) if argv[0] == "uv" else argv
    mount_entries = [f"{source_path}:{source_path}:ro"]
    mount_entries.extend(
        (
            f"{metadata_path}:{metadata_path}:ro",
            f"{scratch_path}:{scratch_path}:rw",
            f"{logs_path}:{source_path / 'pi-smoke-logs'}:rw",
        )
    )
    mounts = ",".join(mount_entries)
    environment_items = tuple(f"{key}={value}" for key, value in sorted(environment.items()))
    if timeout_s < 1:
        raise ValueError("host-verification timeout must be positive")
    hours, seconds = divmod(timeout_s, 3600)
    minutes, seconds = divmod(seconds, 60)
    slurm_time = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return (
        "srun",
        "--exclusive",
        "--nodes=1",
        "--ntasks=1",
        "--cpus-per-task=2",
        "--mem=4096M",
        "--time=" + slurm_time,
        "--kill-on-bad-exit=1",
        "--wait=10",
        "--propagate=CPU,FSIZE,NPROC,NOFILE",
        "--container-image=" + str(image.path),
        "--container-readonly",
        "--no-container-mount-home",
        "--container-unshare=net,ipc,uts",
        "--container-workdir=" + str(source_path),
        "--container-mounts=" + mounts,
        "--export=NONE",
        "/usr/bin/env",
        "-i",
        *environment_items,
        *container_argv,
    )


def validate_pyxis_quota_root(root: Path) -> Path:
    """Return a private filesystem whose total capacity is a hard limit."""
    try:
        resolved, total_bytes = validate_private_capacity_root(root)
    except PyxisArtifactIOError as exc:
        raise PyxisImageValidationError("Pyxis writable quota root is unavailable") from exc
    if total_bytes < 1 or total_bytes > PYXIS_WRITABLE_FILESYSTEM_MAX_BYTES:
        raise PyxisImageValidationError("Pyxis writable storage has no verified hard quota")
    return resolved


def image_sha256(image: Path) -> str:
    """Return the SHA-256 digest for one regular local image."""
    digest = hashlib.sha256()
    with image.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "DEFAULT_HOST_VERIFICATION_PYXIS_IMAGE",
    "PyxisImageMetadata",
    "PyxisImageValidationError",
    "build_pyxis_environment",
    "build_pyxis_srun_command",
    "image_sha256",
    "stage_verified_pyxis_image",
    "validate_pyxis_image",
    "validate_pyxis_quota_root",
]
