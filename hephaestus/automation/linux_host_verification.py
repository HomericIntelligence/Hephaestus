"""Linux host-verification backend configuration.

This module owns the host-side configuration boundary for the optional Linux
verification backend. Scheduler submission and verifier execution are added in
later increments. Configuration parsing is deliberately closed so an
unreviewed setting cannot widen the host execution boundary.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from hephaestus.automation.linux_host_verification_contract import MAX_TIMEOUT_SECONDS

_CONFIG_FIELDS = frozenset(
    {
        "shared_root",
        "image_path",
        "image_manifest_path",
        "trusted_slurm_bin_dir",
        "timeout_seconds",
    }
)


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
