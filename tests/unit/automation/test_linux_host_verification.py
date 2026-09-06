"""Tests for the Linux host-verification backend configuration."""

from __future__ import annotations

import stat
from types import SimpleNamespace

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


def test_trusted_slurm_executable_requires_fixed_root_owned_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a root-owned executable in the configured directory is accepted."""
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
    directory = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
    executable = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o755)
    states = {
        "/": directory,
        "/usr": directory,
        "/usr/bin": directory,
        "/usr/bin/sbatch": executable,
    }
    monkeypatch.setattr("os.lstat", states.__getitem__)

    assert config.trusted_slurm_executable("sbatch") == "/usr/bin/sbatch"


@pytest.mark.parametrize(
    ("name", "state"),
    [
        ("squeue", None),
        ("sbatch", SimpleNamespace(st_uid=1000, st_mode=stat.S_IFREG | 0o755)),
        ("sbatch", SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o775)),
        ("sbatch", SimpleNamespace(st_uid=0, st_mode=stat.S_IFLNK | 0o777)),
        ("sbatch", SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o644)),
    ],
)
def test_trusted_slurm_executable_rejects_untrusted_binary(
    monkeypatch: pytest.MonkeyPatch, name: str, state: SimpleNamespace | None
) -> None:
    """The resolver rejects unknown, writable, linked, and non-executable files."""
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
    directory = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
    states = {"/": directory, "/usr": directory, "/usr/bin": directory}
    if state is not None:
        states["/usr/bin/sbatch"] = state
    monkeypatch.setattr("os.lstat", states.__getitem__)

    with pytest.raises(ValueError):
        config.trusted_slurm_executable(name)


def test_trusted_slurm_executable_rejects_writable_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolver rejects a binary below a writable ancestor directory."""
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
    directory = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
    states = {
        "/": directory,
        "/usr": SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o775),
        "/usr/bin": directory,
        "/usr/bin/sbatch": SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o755),
    }
    monkeypatch.setattr("os.lstat", states.__getitem__)

    with pytest.raises(ValueError):
        config.trusted_slurm_executable("sbatch")
