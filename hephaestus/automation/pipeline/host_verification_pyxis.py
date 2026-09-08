"""Trusted local Pyxis image and command helpers for Linux host checks."""

from __future__ import annotations

import hashlib
import json
import re
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
from hephaestus.config.child_environments import build_host_verification_env

DEFAULT_HOST_VERIFICATION_PYXIS_IMAGE = Path("build/host-verification/hephaestus-ci.sqsh")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HOST_KEYS = [
    "container_runtime",
    "container_image",
    "container_image_sha256",
    "container_image_id",
    "container_image_reference",
    "containerfile_sha256",
    "container_source_revision",
]
PYXIS_AUTHORITY_SCHEMA = "hephaestus-host-verification-pyxis-v2"
PYXIS_WRITABLE_FILESYSTEM_MAX_BYTES = 1024 * 1024 * 1024


def pyxis_receipt_metadata_matches(receipt: Mapping[str, object]) -> bool:
    """Check image metadata without granting a host-verification pass."""
    image = receipt.get("container_image")
    image_digest = receipt.get("container_image_sha256")
    image_id = receipt.get("container_image_id")
    image_reference = receipt.get("container_image_reference")
    return bool(
        receipt.get("container_runtime") == "pyxis"
        and isinstance(image, str)
        and image.startswith("/")
        and "://" not in image
        and _SHA256_RE.fullmatch(str(image_digest or "")) is not None
        and image.endswith(f"/sha256-{image_digest}.sqsh")
        and _IMAGE_ID_RE.fullmatch(str(image_id or "")) is not None
        and image_reference in {f"podman://{image_id}", f"dockerd://{image_id}"}
        and _SHA256_RE.fullmatch(str(receipt.get("containerfile_sha256") or "")) is not None
        and _COMMIT_RE.fullmatch(str(receipt.get("container_source_revision") or "")) is not None
    )


class PyxisImageValidationError(ValueError):
    """Raised when a local Pyxis image cannot be trusted."""


@dataclass(frozen=True)
class PyxisExecutionPlacement:
    """Bind execution to one host-selected allocation and node."""

    allocation_id: str
    node: str

    def __post_init__(self) -> None:
        """Reject allocation or node values that can select multiple targets."""
        if (
            not isinstance(self.allocation_id, str)
            or re.fullmatch(r"[1-9][0-9]*", self.allocation_id) is None
        ):
            raise ValueError("Pyxis allocation ID must be a positive decimal string")
        if (
            not isinstance(self.node, str)
            or len(self.node) > 253
            or any(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) is None
                for label in self.node.split(".")
            )
        ):
            raise ValueError("Pyxis node must be one hostname")


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
    home = scratch_path / "home"
    for directory in (home, temporary, cache):
        directory.mkdir(parents=True, exist_ok=True)
    environment = build_host_verification_env(
        home=home,
        temporary=temporary,
        cache=cache,
        runtime_environment=Path("/opt/hephaestus-venv"),
        executable=Path("/usr/local/bin/uv"),
    )
    environment["PYTHONPATH"] = str(source_path)
    environment["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    return environment


def pyxis_help_supports_container_execution(help_text: str) -> bool:
    """Check standard Pyxis options; the image must enforce isolation at launch."""
    required = (
        "container-image",
        "container-readonly",
        "no-container-mount-home",
        "container-workdir",
        "container-mounts",
    )
    return all(
        re.search(rf"(?m)^[ \t]*--{name}(?=[= \t\r\n]|$)", help_text) is not None
        for name in required
    )


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
    placement: PyxisExecutionPlacement | None = None,
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
    placement_options = (
        (f"--jobid={placement.allocation_id}", f"--nodelist={placement.node}")
        if placement is not None
        else ()
    )
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
        *placement_options,
        "--container-image=" + str(image.path),
        "--container-readonly",
        "--no-container-mount-home",
        "--container-workdir=" + str(source_path),
        "--container-mounts=" + mounts,
        "--export=NONE",
        "/usr/bin/env",
        "-i",
        *environment_items,
        # The verified image supplies these tools. Drop namespace capabilities
        # before candidate code starts; a setup failure cannot run the command.
        "/usr/bin/unshare",
        "--user",
        "--map-root-user",
        "--net",
        "--ipc",
        "--uts",
        "--",
        "/usr/bin/setpriv",
        "--no-new-privs",
        "--bounding-set=-all",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--",
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
    "PyxisExecutionPlacement",
    "PyxisImageMetadata",
    "PyxisImageValidationError",
    "build_pyxis_environment",
    "build_pyxis_srun_command",
    "image_sha256",
    "stage_verified_pyxis_image",
    "validate_pyxis_image",
    "validate_pyxis_quota_root",
]
