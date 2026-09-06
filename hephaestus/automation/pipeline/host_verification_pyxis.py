"""Trusted local Pyxis image and command helpers for Linux host checks."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOST_VERIFICATION_PYXIS_IMAGE = Path("build/host-verification/hephaestus-ci.sqsh")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PyxisImageValidationError(ValueError):
    """Raised when a local Pyxis image cannot be trusted."""


@dataclass(frozen=True)
class PyxisImageMetadata:
    """Verified local squashfs metadata used by the worker and receipt."""

    path: Path
    sha256: str
    container_runtime: str = "pyxis"


def validate_pyxis_image(image: Path) -> PyxisImageMetadata:
    """Validate one local squashfs image and its immutable SHA-256 sidecar."""
    resolved = _local_squashfs_image(image)
    expected = _sidecar_digest(resolved)
    try:
        actual = image_sha256(resolved)
    except OSError as exc:
        raise PyxisImageValidationError("Pyxis image digest cannot be read") from exc
    if actual != expected:
        raise PyxisImageValidationError("Pyxis image digest does not match sidecar")
    return PyxisImageMetadata(path=resolved, sha256=actual)


def _local_squashfs_image(image: Path) -> Path:
    """Return a verified local squashfs file for the Pyxis image."""
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", str(image)):
        raise PyxisImageValidationError("Pyxis image must be a local filesystem path")
    candidate = Path(image).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise PyxisImageValidationError("Pyxis image must be a regular local file")
    try:
        resolved = candidate.resolve(strict=True)
        with resolved.open("rb") as stream:
            if stream.read(4) != b"hsqs":
                raise PyxisImageValidationError("Pyxis image is not a squashfs file")
    except PyxisImageValidationError:
        raise
    except OSError as exc:
        raise PyxisImageValidationError("Pyxis image is unavailable") from exc
    return resolved


def _sidecar_digest(image: Path) -> str:
    """Return the expected image digest from one strict local sidecar."""
    sidecar = image.with_name(f"{image.name}.sha256")
    if sidecar.is_symlink() or not sidecar.is_file():
        raise PyxisImageValidationError("Pyxis image digest sidecar is missing")
    try:
        lines = sidecar.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PyxisImageValidationError("Pyxis image digest sidecar is unreadable") from exc
    if len(lines) != 1:
        raise PyxisImageValidationError("Pyxis image digest sidecar is malformed")
    fields = lines[0].split()
    expected = fields[0] if fields else ""
    if not _SHA256_RE.fullmatch(expected) or len(fields) > 2:
        raise PyxisImageValidationError("Pyxis image digest sidecar is malformed")
    if len(fields) == 2 and fields[1].lstrip("*") != image.name:
        raise PyxisImageValidationError("Pyxis image digest sidecar names another image")
    return expected


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
    runtime_environment: Path,
    git_metadata: Path,
    scratch: Path,
    pi_smoke_logs: Path,
    argv: tuple[str, ...],
    environment: Mapping[str, str],
) -> tuple[str, ...]:
    """Build one fixed ``srun`` command with Pyxis isolation flags."""
    if not argv:
        raise ValueError("host-verification argv cannot be empty")
    source_path = source.expanduser().resolve()
    runtime_path = runtime_environment.expanduser().resolve()
    metadata_path = git_metadata.expanduser().resolve()
    scratch_path = scratch.expanduser().resolve()
    logs_path = pi_smoke_logs.expanduser().resolve()
    container_argv = ("/usr/local/bin/uv", *argv[1:]) if argv[0] == "uv" else argv
    mount_entries = [f"{source_path}:{source_path}:ro"]
    # The normal Linux path uses the sealed virtual environment baked into the
    # CI image at /opt/hephaestus-venv. Mount a host runtime only for callers
    # that provide one, which prevents an absent host path from masking the
    # image's trusted runtime.
    if runtime_path.exists():
        mount_entries.append(f"{runtime_path}:{runtime_path}:ro")
    mount_entries.extend(
        (
            f"{metadata_path}:{metadata_path}:ro",
            f"{scratch_path}:{scratch_path}:rw",
            f"{logs_path}:{logs_path}:rw",
        )
    )
    mounts = ",".join(mount_entries)
    environment_items = tuple(f"{key}={value}" for key, value in sorted(environment.items()))
    return (
        "srun",
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
    "validate_pyxis_image",
]
