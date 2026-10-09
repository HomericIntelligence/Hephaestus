"""Trusted local Pyxis image and command helpers for Linux host checks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

from hephaestus.automation.pyxis_artifact_io import (
    CrossNodePathBinding,
    PyxisArtifactIOError,
    read_private_regular_file,
    stage_private_content_addressed_file,
    validate_private_capacity_root,
    validate_private_squashfs_file,
)

DEFAULT_HOST_VERIFICATION_PYXIS_IMAGE = Path("build/host-verification/hephaestus-ci.sqsh")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PYXIS_AUTHORITY_SCHEMA = "hephaestus-host-verification-pyxis-v2"
PYXIS_WRITABLE_FILESYSTEM_MAX_BYTES = 1024 * 1024 * 1024
_TRUSTED_SRUN = Path("/usr/bin/srun")
_PYXIS_PARENT_BOOTSTRAP = (
    "import json, os, socket, sys\n"
    "names = ('user', 'net', 'ipc', 'uts')\n"
    "parent_namespaces = {name: os.readlink('/proc/self/ns/' + name) for name in names}\n"
    "node = socket.gethostname()\n"
    "required = {\n"
    "    'job_id': os.environ.get('SLURM_JOB_ID', ''),\n"
    "    'step_id': os.environ.get('SLURM_STEP_ID', ''),\n"
    "    'step_nodelist': os.environ.get('SLURM_STEP_NODELIST', ''),\n"
    "    'step_nodes': os.environ.get('SLURM_STEP_NUM_NODES', ''),\n"
    "    'cpus_per_task': os.environ.get('SLURM_CPUS_PER_TASK', ''),\n"
    "    'job_start_time': os.environ.get('SLURM_JOB_START_TIME', ''),\n"
    "    'job_end_time': os.environ.get('SLURM_JOB_END_TIME', ''),\n"
    "}\n"
    "if (required['step_nodelist'] != node or required['step_nodes'] != '1' "
    "or required['cpus_per_task'] != '2'):\n"
    "    raise SystemExit('Slurm step evidence is invalid')\n"
    "if any(not value.isdecimal() for key, value in required.items() "
    "if key != 'step_nodelist'):\n"
    "    raise SystemExit('Slurm numeric evidence is invalid')\n"
    "if int(required['job_end_time']) <= int(required['job_start_time']):\n"
    "    raise SystemExit('Slurm wall-time evidence is invalid')\n"
    "argv = sys.argv[1:]\n"
    "try:\n"
    "    env_index = argv.index('/usr/bin/env')\n"
    "except ValueError:\n"
    "    raise SystemExit('scrubbed environment boundary is missing') from None\n"
    "if argv[env_index + 1:env_index + 2] != ['-i']:\n"
    "    raise SystemExit('scrubbed environment boundary is invalid')\n"
    "with socket.socket() as listener:\n"
    "    listener.bind(('127.0.0.1', 0))\n"
    "    listener.listen(1)\n"
    "    listener.set_inheritable(True)\n"
    "    evidence = [\n"
    "        'HEPHAESTUS_PYXIS_PARENT_NAMESPACES=' + json.dumps(parent_namespaces),\n"
    "        'HEPHAESTUS_PYXIS_PARENT_NODE=' + node,\n"
    "        'HEPHAESTUS_PYXIS_PARENT_PORT=' + str(listener.getsockname()[1]),\n"
    "        'HEPHAESTUS_PYXIS_SLURM_EVIDENCE=' + json.dumps(required),\n"
    "    ]\n"
    "    argv[env_index + 2:env_index + 2] = evidence\n"
    "    os.execv(argv[0], argv)\n"
)


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
    launch_binding: CrossNodePathBinding | None = field(
        default=None,
        compare=False,
        repr=False,
    )


def resolve_trusted_srun_executable() -> str | None:
    """Return the fixed trusted Slurm launcher when it is safe to execute."""
    try:
        resolved = _TRUSTED_SRUN.resolve(strict=True)
        metadata = resolved.stat()
    except OSError:
        return None
    if (
        not resolved.is_file()
        or not os.access(resolved, os.X_OK)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        return None
    return str(resolved)


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
    resolved, actual = _local_squashfs_image(image)
    authority = _read_authority(provenance)
    if authority["squashfs_sha256"] != expected_sha256:
        raise PyxisImageValidationError("Pyxis authority does not match expected digest")
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


def _local_squashfs_image(image: Path) -> tuple[Path, str]:
    """Return one verified local squashfs path and digest."""
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", str(image)):
        raise PyxisImageValidationError("Pyxis image must be a local filesystem path")
    try:
        return validate_private_squashfs_file(image)
    except PyxisArtifactIOError as exc:
        if str(exc) == "artifact is not a squashfs file":
            raise PyxisImageValidationError("Pyxis image is not a squashfs file") from exc
        if "changed" in str(exc):
            raise PyxisImageValidationError("Pyxis image changed during validation") from exc
        raise PyxisImageValidationError("Pyxis image is not a regular local file") from exc


def stage_verified_pyxis_image(
    image: PyxisImageMetadata, destination_root: Path
) -> PyxisImageMetadata:
    """Copy authorized bytes to one private content-addressed execution path."""
    try:
        binding = cast(
            CrossNodePathBinding,
            stage_private_content_addressed_file(
                image.path,
                destination_root,
                image.sha256,
                retain_binding=True,
            ),
        )
    except PyxisArtifactIOError as exc:
        raise PyxisImageValidationError(f"Pyxis image staging failed: {exc}") from exc
    return replace(image, path=binding.path / f"sha256-{image.sha256}.sqsh", launch_binding=binding)


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
    srun_executable: Path,
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
    if not srun_executable.is_absolute():
        raise ValueError("srun executable must be absolute")
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
        str(srun_executable),
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
        "--no-container-remap-root",
        "--container-workdir=" + str(source_path),
        "--container-mounts=" + mounts,
        "--export=NONE",
        "/usr/local/bin/python",
        "-I",
        "-S",
        "-c",
        _PYXIS_PARENT_BOOTSTRAP,
        # Older Pyxis versions do not supply a namespace option. The trusted
        # image must create all required namespaces before the payload starts.
        "/usr/bin/unshare",
        "--user",
        "--map-current-user",
        "--net",
        "--ipc",
        "--uts",
        "--",
        "/usr/bin/env",
        "-i",
        *environment_items,
        *container_argv,
    )


def validate_pyxis_quota_root(
    root: Path, *, retain_binding: bool = False
) -> Path | CrossNodePathBinding:
    """Return a private filesystem whose total capacity is a hard limit."""
    try:
        validated, total_bytes = validate_private_capacity_root(
            root,
            retain_binding=retain_binding,
        )
    except PyxisArtifactIOError as exc:
        raise PyxisImageValidationError("Pyxis writable quota root is unavailable") from exc
    if total_bytes < 1 or total_bytes > PYXIS_WRITABLE_FILESYSTEM_MAX_BYTES:
        if isinstance(validated, CrossNodePathBinding):
            validated.close()
        raise PyxisImageValidationError("Pyxis writable storage has no verified hard quota")
    if retain_binding:
        return cast(CrossNodePathBinding, validated)
    return cast(Path, validated)


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
    "resolve_trusted_srun_executable",
    "stage_verified_pyxis_image",
    "validate_pyxis_image",
    "validate_pyxis_quota_root",
]
