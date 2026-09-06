"""Tests for the Linux host-verification backend configuration."""

from __future__ import annotations

import pytest


def test_config_accepts_closed_absolute_distinct_paths() -> None:
    """A backend configuration accepts one complete safe path boundary."""
    from hephaestus.automation.linux_host_verification import LinuxHostVerificationConfig

    config = LinuxHostVerificationConfig.from_mapping(
        {
            "shared_root": "/srv/hephaestus-runs",
            "image_path": "/srv/hephaestus-images/verify.sqsh",
            "image_manifest_path": "/srv/hephaestus-images/verify.manifest.json",
            "trusted_slurm_bin_dir": "/usr/bin",
            "timeout_seconds": 900,
        }
    )

    assert config.shared_root == "/srv/hephaestus-runs"
    assert config.image_path == "/srv/hephaestus-images/verify.sqsh"
    assert config.image_manifest_path == "/srv/hephaestus-images/verify.manifest.json"
    assert config.trusted_slurm_bin_dir == "/usr/bin"
    assert config.timeout_seconds == 900


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("shared_root", "relative/runs"),
        ("image_path", "/srv/hephaestus-runs"),
        ("image_manifest_path", "/srv/hephaestus-images/../verify.manifest.json"),
        ("trusted_slurm_bin_dir", "/usr/bin/"),
        ("timeout_seconds", 0),
        ("timeout_seconds", True),
    ],
)
def test_config_rejects_noncanonical_or_unsafe_values(field: str, value: object) -> None:
    """A backend configuration fails closed for unsafe values."""
    from hephaestus.automation.linux_host_verification import LinuxHostVerificationConfig

    payload: dict[str, object] = {
        "shared_root": "/srv/hephaestus-runs",
        "image_path": "/srv/hephaestus-images/verify.sqsh",
        "image_manifest_path": "/srv/hephaestus-images/verify.manifest.json",
        "trusted_slurm_bin_dir": "/usr/bin",
        "timeout_seconds": 900,
    }
    payload[field] = value

    with pytest.raises(ValueError):
        LinuxHostVerificationConfig.from_mapping(payload)


def test_config_rejects_unknown_keys_and_colliding_paths() -> None:
    """The configuration schema is closed and its path roles are distinct."""
    from hephaestus.automation.linux_host_verification import LinuxHostVerificationConfig

    payload = {
        "shared_root": "/srv/hephaestus-runs",
        "image_path": "/srv/hephaestus-images/verify.sqsh",
        "image_manifest_path": "/srv/hephaestus-images/verify.manifest.json",
        "trusted_slurm_bin_dir": "/usr/bin",
        "timeout_seconds": 900,
        "extra": "not accepted",
    }
    with pytest.raises(ValueError):
        LinuxHostVerificationConfig.from_mapping(payload)

    payload.pop("extra")
    payload["image_manifest_path"] = "/srv/hephaestus-images/verify.sqsh"
    with pytest.raises(ValueError):
        LinuxHostVerificationConfig.from_mapping(payload)
