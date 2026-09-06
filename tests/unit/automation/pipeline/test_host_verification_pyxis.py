"""Tests for the Linux Pyxis host-verification boundary."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hephaestus.automation.pipeline.host_verification_pyxis import (
    PyxisImageValidationError,
    build_pyxis_environment,
    build_pyxis_srun_command,
    validate_pyxis_image,
)
from hephaestus.automation.pipeline.stages.pr_review_receipts import (
    _host_verification_receipt_matches,
)
from hephaestus.automation.pipeline.stages.pr_review_verification import _HostVerificationSpec


def _image(tmp_path: Path, *, sidecar: bool = True) -> tuple[Path, str]:
    """Create a small squashfs-shaped image fixture and its digest sidecar."""
    image = tmp_path / "host-verification.sqsh"
    image.write_bytes(b"hsqs" + b"fixture image")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    if sidecar:
        image.with_name(f"{image.name}.sha256").write_text(
            f"{digest}  {image.name}\n", encoding="utf-8"
        )
    return image, digest


def test_validate_pyxis_image_binds_local_squashfs_to_sidecar(tmp_path: Path) -> None:
    """A valid local image returns its verified absolute path and digest."""
    image, digest = _image(tmp_path)

    metadata = validate_pyxis_image(image)

    assert metadata.path == image.resolve()
    assert metadata.sha256 == digest
    assert metadata.container_runtime == "pyxis"


def test_validate_pyxis_image_rejects_missing_sidecar(tmp_path: Path) -> None:
    """An image without the matching digest sidecar fails closed."""
    image, _ = _image(tmp_path, sidecar=False)

    with pytest.raises(PyxisImageValidationError, match="sidecar"):
        validate_pyxis_image(image)


def test_validate_pyxis_image_rejects_mutable_symlink(tmp_path: Path) -> None:
    """A symlink cannot select a mutable image after validation."""
    image, _ = _image(tmp_path)
    link = tmp_path / "link.sqsh"
    link.symlink_to(image)

    with pytest.raises(PyxisImageValidationError, match="regular"):
        validate_pyxis_image(link)


def test_validate_pyxis_image_rejects_registry_uri(tmp_path: Path) -> None:
    """The worker accepts only a local filesystem image path."""
    with pytest.raises(PyxisImageValidationError, match="local"):
        validate_pyxis_image(Path("docker://registry.example/image:latest"))


def test_validate_pyxis_image_rejects_digest_mismatch(tmp_path: Path) -> None:
    """A changed image cannot reuse an old digest sidecar."""
    image, _ = _image(tmp_path)
    image.write_bytes(b"hsqs" + b"changed")

    with pytest.raises(PyxisImageValidationError, match="digest"):
        validate_pyxis_image(image)


def test_build_pyxis_srun_command_uses_read_only_source_and_no_network(
    tmp_path: Path,
) -> None:
    """Pyxis receives fixed isolation flags and explicit mount modes."""
    image, digest = _image(tmp_path)
    source = tmp_path / "source"
    runtime = tmp_path / "runtime"
    metadata = tmp_path / "metadata.git"
    scratch = tmp_path / "scratch"
    logs = source / "pi-smoke-logs"
    for path in (source, runtime, metadata, scratch, logs):
        path.mkdir(parents=True)

    command = build_pyxis_srun_command(
        image=validate_pyxis_image(image),
        source=source,
        runtime_environment=runtime,
        git_metadata=metadata,
        scratch=scratch,
        pi_smoke_logs=logs,
        argv=("uv", "run", "pytest", "tests/unit"),
        environment={"HOME": str(scratch / "home"), "UV_OFFLINE": "1"},
    )

    assert command[0] == "srun"
    assert "--container-readonly" in command
    assert "--no-container-mount-home" in command
    assert "--container-unshare=net,ipc,uts" in command
    assert "--export=NONE" in command
    assert f"--container-image={image.resolve()}" in command
    assert f"--container-workdir={source.resolve()}" in command
    mounts = next(value for value in command if value.startswith("--container-mounts="))
    assert f"{source.resolve()}:{source.resolve()}:ro" in mounts
    assert f"{runtime.resolve()}:{runtime.resolve()}:ro" in mounts
    assert f"{metadata.resolve()}:{metadata.resolve()}:ro" in mounts
    assert f"{scratch.resolve()}:{scratch.resolve()}:rw" in mounts
    assert f"{logs.resolve()}:{logs.resolve()}:rw" in mounts
    assert command[command.index("/usr/bin/env") + 1] == "-i"
    assert "/usr/local/bin/uv" in command
    assert digest in validate_pyxis_image(image).sha256


def test_build_pyxis_environment_is_scrubbed_and_source_bound(tmp_path: Path) -> None:
    """The container receives only fixed offline variables and source path."""
    source = tmp_path / "source"
    scratch = tmp_path / "scratch"
    environment = build_pyxis_environment(source=source, scratch=scratch)

    assert environment == {
        "HOME": str((scratch / "home").resolve()),
        "TMPDIR": str((scratch / "tmp").resolve()),
        "TMP": str((scratch / "tmp").resolve()),
        "TEMP": str((scratch / "tmp").resolve()),
        "XDG_CACHE_HOME": str((scratch / "cache").resolve()),
        "UV_CACHE_DIR": str((scratch / "cache" / "uv").resolve()),
        "UV_PROJECT_ENVIRONMENT": "/opt/hephaestus-venv",
        "UV_OFFLINE": "1",
        "UV_NO_SYNC": "1",
        "PYTHONPATH": str(source.resolve()),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    assert "GH_TOKEN" not in environment
    assert "GITHUB_TOKEN" not in environment


def test_linux_pyxis_receipt_requires_exact_image_digest(tmp_path: Path) -> None:
    """Only a passed Linux receipt with local image evidence can match."""
    spec = _HostVerificationSpec(
        changed_path=None,
        argv=("uv", "run", "pytest", "tests/unit"),
        descr="review_python_tests",
    )
    receipt = {
        "argv": list(spec.argv),
        "head_sha": "a" * 40,
        "immutable_source": True,
        "ok": True,
        "platform": "linux",
        "status": "passed",
        "container_runtime": "pyxis",
        "container_image": str((tmp_path / "host-verification.sqsh").resolve()),
        "container_image_sha256": "b" * 64,
        "stdout_tail": "",
        "stderr_tail": "",
    }

    assert _host_verification_receipt_matches(receipt, spec, "a" * 40)
    assert not _host_verification_receipt_matches(
        {**receipt, "container_image_sha256": ""}, spec, "a" * 40
    )
    assert not _host_verification_receipt_matches(
        {**receipt, "container_image": "docker://image"}, spec, "a" * 40
    )
    assert not _host_verification_receipt_matches({**receipt, "status": "skipped"}, spec, "a" * 40)
