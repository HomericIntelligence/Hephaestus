"""Tests for the Linux host-verification backend configuration."""

from __future__ import annotations

import stat
from pathlib import Path
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


def test_load_config_reads_one_closed_nonwritable_toml_table(tmp_path: Path) -> None:
    """The loader accepts exactly one sealed backend configuration table."""
    from hephaestus.automation.linux_host_verification import load_linux_host_verification_config

    config_path = tmp_path / "linux-host-verification.toml"
    config_path.write_text(
        """[linux_host_verification]
shared_root = \"/srv/hephaestus-runs\"
image_path = \"/srv/hephaestus-images/verify.sqsh\"
image_manifest_path = \"/srv/hephaestus-images/verify.manifest.json\"
trusted_slurm_bin_dir = \"/usr/bin\"
timeout_seconds = 900
""",
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    config = load_linux_host_verification_config(config_path)

    assert config.shared_root == "/srv/hephaestus-runs"
    assert config.timeout_seconds == 900


@pytest.mark.parametrize(
    ("contents", "mode"),
    [
        ("", 0o600),
        ("[linux_host_verification]\nunknown = 1\n", 0o600),
        ("[linux_host_verification]\ntimeout_seconds = 0\n", 0o600),
        ("[other]\nvalue = 1\n", 0o600),
        ("[linux_host_verification]\n", 0o622),
    ],
)
def test_load_config_rejects_malformed_or_writable_file(
    tmp_path: Path, contents: str, mode: int
) -> None:
    """The loader fails closed when config data or file mode is unsafe."""
    from hephaestus.automation.linux_host_verification import load_linux_host_verification_config

    config_path = tmp_path / "linux-host-verification.toml"
    config_path.write_text(contents, encoding="utf-8")
    config_path.chmod(mode)

    with pytest.raises(ValueError):
        load_linux_host_verification_config(config_path)


def test_prepare_run_creates_private_shared_lease_layout(tmp_path: Path) -> None:
    """A run receives one private shared-root layout with no ambient paths."""
    from hephaestus.automation.linux_host_verification import (
        LinuxHostVerificationConfig,
        prepare_linux_host_verification_run,
    )

    shared_root = tmp_path / "shared"
    shared_root.mkdir(mode=0o700)
    config = LinuxHostVerificationConfig.from_mapping(
        {
            "shared_root": str(shared_root),
            "image_path": "/srv/hephaestus-images/verify.sqsh",
            "image_manifest_path": "/srv/hephaestus-images/verify.manifest.json",
            "trusted_slurm_bin_dir": "/usr/bin",
            "timeout_seconds": 900,
        }
    )

    run = prepare_linux_host_verification_run(config, "run-20260906-01234567")

    assert run.root == shared_root / "run-20260906-01234567"
    assert run.request_path.parent == run.root
    assert run.receipt_path.parent == run.root
    assert run.source_archive_path.parent == run.root
    assert run.git_metadata_archive_path.parent == run.root
    assert run.source_extract_path.parent == run.root
    assert run.git_metadata_extract_path.parent == run.root
    assert run.scratch_path.parent == run.root
    for directory in (
        run.root,
        run.source_extract_path,
        run.git_metadata_extract_path,
        run.scratch_path,
    ):
        assert directory.is_dir()
        assert directory.stat().st_mode & 0o077 == 0


def test_prepare_run_rejects_existing_or_unsafe_shared_root(tmp_path: Path) -> None:
    """Run staging fails closed instead of reusing or widening a shared path."""
    from hephaestus.automation.linux_host_verification import (
        LinuxHostVerificationConfig,
        prepare_linux_host_verification_run,
    )

    shared_root = tmp_path / "shared"
    shared_root.mkdir(mode=0o700)
    config = LinuxHostVerificationConfig.from_mapping(
        {
            "shared_root": str(shared_root),
            "image_path": "/srv/hephaestus-images/verify.sqsh",
            "image_manifest_path": "/srv/hephaestus-images/verify.manifest.json",
            "trusted_slurm_bin_dir": "/usr/bin",
            "timeout_seconds": 900,
        }
    )

    prepare_linux_host_verification_run(config, "run-20260906-01234567")
    with pytest.raises(ValueError):
        prepare_linux_host_verification_run(config, "run-20260906-01234567")

    unsafe_root = tmp_path / "unsafe"
    unsafe_root.mkdir(mode=0o755)
    unsafe_config = LinuxHostVerificationConfig.from_mapping(
        {
            "shared_root": str(unsafe_root),
            "image_path": "/srv/hephaestus-images/verify.sqsh",
            "image_manifest_path": "/srv/hephaestus-images/verify.manifest.json",
            "trusted_slurm_bin_dir": "/usr/bin",
            "timeout_seconds": 900,
        }
    )
    with pytest.raises(ValueError):
        prepare_linux_host_verification_run(unsafe_config, "run-20260906-76543210")


def test_cleanup_run_removes_only_the_exact_private_run(tmp_path: Path) -> None:
    """Cleanup releases one verified run without touching a sibling or root."""
    from hephaestus.automation.linux_host_verification import (
        LinuxHostVerificationConfig,
        cleanup_linux_host_verification_run,
        prepare_linux_host_verification_run,
    )

    shared_root = tmp_path / "shared"
    shared_root.mkdir(mode=0o700)
    config = LinuxHostVerificationConfig.from_mapping(
        {
            "shared_root": str(shared_root),
            "image_path": "/srv/hephaestus-images/verify.sqsh",
            "image_manifest_path": "/srv/hephaestus-images/verify.manifest.json",
            "trusted_slurm_bin_dir": "/usr/bin",
            "timeout_seconds": 900,
        }
    )
    run = prepare_linux_host_verification_run(config, "run-20260906-01234567")
    sibling = shared_root / "other-run"
    sibling.mkdir(mode=0o700)

    cleanup_linux_host_verification_run(config, run)

    assert not run.root.exists()
    assert shared_root.is_dir()
    assert sibling.is_dir()


def test_cleanup_run_rejects_foreign_or_unverified_path(tmp_path: Path) -> None:
    """Cleanup fails closed if a caller supplies paths outside its exact run."""
    from hephaestus.automation.linux_host_verification import (
        LinuxHostVerificationConfig,
        LinuxHostVerificationRun,
        cleanup_linux_host_verification_run,
        prepare_linux_host_verification_run,
    )

    shared_root = tmp_path / "shared"
    shared_root.mkdir(mode=0o700)
    config = LinuxHostVerificationConfig.from_mapping(
        {
            "shared_root": str(shared_root),
            "image_path": "/srv/hephaestus-images/verify.sqsh",
            "image_manifest_path": "/srv/hephaestus-images/verify.manifest.json",
            "trusted_slurm_bin_dir": "/usr/bin",
            "timeout_seconds": 900,
        }
    )
    run = prepare_linux_host_verification_run(config, "run-20260906-01234567")
    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o700)
    forged = LinuxHostVerificationRun(
        run_id=run.run_id,
        root=foreign,
        source_archive_path=foreign / "source.tar",
        source_extract_path=foreign / "source",
        git_metadata_archive_path=foreign / "git-metadata.tar",
        git_metadata_extract_path=foreign / "git-metadata",
        scratch_path=foreign / "scratch",
        stdout_path=foreign / "stdout.log",
        stderr_path=foreign / "stderr.log",
        request_path=foreign / "request.json",
        receipt_path=foreign / "receipt.json",
    )

    with pytest.raises(ValueError):
        cleanup_linux_host_verification_run(config, forged)
    assert foreign.is_dir()
    assert run.root.is_dir()
