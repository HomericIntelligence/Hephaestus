"""Tests for the Linux Pyxis host-verification boundary."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import hephaestus.automation.pipeline.host_verification_pyxis as pyxis_boundary
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


def _authority(image: Path, digest: str, **updates: str) -> Path:
    """Create one private host authority for an image fixture."""
    image.chmod(0o400)
    payload = {
        "schema": "hephaestus-host-verification-pyxis-v2",
        "containerfile": "ci/Containerfile",
        "containerfile_sha256": "c" * 64,
        "container_image_id": "sha256:" + ("d" * 64),
        "container_image_reference": "podman://sha256:" + ("d" * 64),
        "source_revision": "e" * 40,
        "squashfs_sha256": digest,
    }
    payload.update(updates)
    authority = image.with_suffix(".authority.json")
    authority.write_text(json.dumps(payload), encoding="utf-8")
    authority.chmod(0o400)
    return authority


def test_validate_pyxis_image_binds_local_squashfs_to_sidecar(tmp_path: Path) -> None:
    """A valid local image returns its host-authorized digest and provenance."""
    image, digest = _image(tmp_path)
    authority = _authority(image, digest)

    metadata = validate_pyxis_image(
        image,
        expected_sha256=digest,
        provenance=authority,
    )

    assert metadata.path == image.resolve()
    assert metadata.sha256 == digest
    assert metadata.container_runtime == "pyxis"
    assert metadata.container_image_id == "sha256:" + ("d" * 64)
    assert metadata.source_revision == "e" * 40


def test_validate_pyxis_image_rejects_self_attested_sidecar(tmp_path: Path) -> None:
    """An image and adjacent digest file are not independent authority."""
    image, _ = _image(tmp_path)

    with pytest.raises(PyxisImageValidationError, match="authority"):
        validate_pyxis_image(image)


def test_validate_pyxis_image_rejects_forged_image_and_sidecar_pair(tmp_path: Path) -> None:
    """A forged pair cannot replace the separately configured expected digest."""
    image, digest = _image(tmp_path)
    authority = _authority(image, digest)

    with pytest.raises(PyxisImageValidationError, match="expected digest"):
        validate_pyxis_image(
            image,
            expected_sha256="f" * 64,
            provenance=authority,
        )


def test_validate_pyxis_image_rejects_writable_authority(tmp_path: Path) -> None:
    """A group-writable authority cannot grant execution permission."""
    image, digest = _image(tmp_path)
    authority = _authority(image, digest)
    authority.chmod(0o620)

    with pytest.raises(PyxisImageValidationError, match="permissions"):
        validate_pyxis_image(
            image,
            expected_sha256=digest,
            provenance=authority,
        )


def test_validate_pyxis_image_rejects_wrong_authority_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authority must belong to the effective host user."""
    image, digest = _image(tmp_path)
    authority = _authority(image, digest)
    real_fstat = os.fstat
    authority_inode = authority.stat().st_ino

    def wrong_owner(descriptor: int) -> os.stat_result:
        result = real_fstat(descriptor)
        if result.st_ino != authority_inode:
            return result
        values = list(result)
        values[4] = result.st_uid + 1
        return os.stat_result(values)

    monkeypatch.setattr(
        "hephaestus.automation.pyxis_artifact_io.os.fstat",
        wrong_owner,
    )
    with pytest.raises(PyxisImageValidationError, match="owner"):
        validate_pyxis_image(
            image,
            expected_sha256=digest,
            provenance=authority,
        )


def test_stage_verified_pyxis_image_uses_content_addressed_read_only_bytes(
    tmp_path: Path,
) -> None:
    """The dispatched image is a private copy with the authorized digest."""
    image, digest = _image(tmp_path)
    authority = _authority(image, digest)
    stage_root = tmp_path / "stage"
    stage_root.mkdir(mode=0o700)
    metadata = validate_pyxis_image(
        image,
        expected_sha256=digest,
        provenance=authority,
    )

    staged = pyxis_boundary.stage_verified_pyxis_image(metadata, stage_root)

    assert staged.path == stage_root / f"sha256-{digest}.sqsh"
    assert hashlib.sha256(staged.path.read_bytes()).hexdigest() == digest
    assert staged.path.stat().st_mode & 0o777 == 0o400


def test_stage_verified_pyxis_image_reuses_verified_digest_target(tmp_path: Path) -> None:
    """A retry reuses an unchanged private digest target."""
    image, digest = _image(tmp_path)
    authority = _authority(image, digest)
    stage_root = tmp_path / "stage"
    stage_root.mkdir(mode=0o700)
    metadata = validate_pyxis_image(
        image,
        expected_sha256=digest,
        provenance=authority,
    )

    first = pyxis_boundary.stage_verified_pyxis_image(metadata, stage_root)
    second = pyxis_boundary.stage_verified_pyxis_image(metadata, stage_root)

    assert second.path == first.path
    assert hashlib.sha256(second.path.read_bytes()).hexdigest() == digest


def test_stage_verified_pyxis_image_preserves_conflicting_digest_target(
    tmp_path: Path,
) -> None:
    """A conflicting existing target fails closed and stays intact."""
    image, digest = _image(tmp_path)
    authority = _authority(image, digest)
    stage_root = tmp_path / "stage"
    stage_root.mkdir(mode=0o700)
    target = stage_root / f"sha256-{digest}.sqsh"
    target.write_bytes(b"conflict")
    target.chmod(0o400)
    metadata = validate_pyxis_image(
        image,
        expected_sha256=digest,
        provenance=authority,
    )

    with pytest.raises(PyxisImageValidationError, match="staging"):
        pyxis_boundary.stage_verified_pyxis_image(metadata, stage_root)

    assert target.read_bytes() == b"conflict"


def test_stage_verified_pyxis_image_rejects_root_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replaced staging root cannot redirect a content-addressed write."""
    image, digest = _image(tmp_path)
    authority = _authority(image, digest)
    stage_root = tmp_path / "stage"
    moved_root = tmp_path / "moved-stage"
    stage_root.mkdir(mode=0o700)
    metadata = validate_pyxis_image(
        image,
        expected_sha256=digest,
        provenance=authority,
    )
    real_open = os.open
    substituted = False

    def substitute_root(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal substituted
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if not substituted and Path(path) == image:
            stage_root.rename(moved_root)
            stage_root.mkdir(mode=0o700)
            substituted = True
        return descriptor

    monkeypatch.setattr(
        "hephaestus.automation.pyxis_artifact_io.os.open",
        substitute_root,
    )

    with pytest.raises(PyxisImageValidationError, match="staging"):
        pyxis_boundary.stage_verified_pyxis_image(metadata, stage_root)

    target_name = f"sha256-{digest}.sqsh"
    assert not (stage_root / target_name).exists()
    assert not (moved_root / target_name).exists()


def test_stage_verified_pyxis_image_rejects_substitution_after_validation(
    tmp_path: Path,
) -> None:
    """Changed source bytes fail before the content-addressed image dispatch."""
    image, digest = _image(tmp_path)
    authority = _authority(image, digest)
    metadata = validate_pyxis_image(
        image,
        expected_sha256=digest,
        provenance=authority,
    )
    image.chmod(0o600)
    image.write_bytes(b"hsqs" + b"substitute")
    image.chmod(0o400)
    stage_root = tmp_path / "stage"
    stage_root.mkdir(mode=0o700)

    with pytest.raises(PyxisImageValidationError, match="changed during staging"):
        pyxis_boundary.stage_verified_pyxis_image(metadata, stage_root)


def test_validate_pyxis_image_rejects_missing_sidecar(tmp_path: Path) -> None:
    """An image without its host provenance fails closed."""
    image, _ = _image(tmp_path, sidecar=False)

    with pytest.raises(PyxisImageValidationError, match="authority"):
        validate_pyxis_image(image)


def test_validate_pyxis_image_rejects_mutable_symlink(tmp_path: Path) -> None:
    """A symlink cannot select a mutable image after validation."""
    image, _ = _image(tmp_path)
    link = tmp_path / "link.sqsh"
    link.symlink_to(image)

    with pytest.raises(PyxisImageValidationError, match="regular"):
        validate_pyxis_image(
            link,
            expected_sha256="f" * 64,
            provenance=tmp_path / "authority.json",
        )


def test_validate_pyxis_image_rejects_path_substitution_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement after the image opens cannot change the verified path."""
    image, digest = _image(tmp_path)
    authority = _authority(image, digest)
    replacement = tmp_path / "replacement.sqsh"
    replacement.write_bytes(image.read_bytes())
    replacement.chmod(0o400)
    real_open = os.open
    substituted = False

    def substitute_image(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal substituted
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if not substituted and Path(path) == image:
            os.replace(replacement, image)
            substituted = True
        return descriptor

    monkeypatch.setattr(
        "hephaestus.automation.pyxis_artifact_io.os.open",
        substitute_image,
    )

    with pytest.raises(PyxisImageValidationError, match="changed"):
        validate_pyxis_image(
            image,
            expected_sha256=digest,
            provenance=authority,
        )


def test_validate_pyxis_image_rejects_registry_uri(tmp_path: Path) -> None:
    """The worker accepts only a local filesystem image path."""
    with pytest.raises(PyxisImageValidationError, match="local"):
        validate_pyxis_image(
            Path("docker://registry.example/image:latest"),
            expected_sha256="f" * 64,
            provenance=tmp_path / "authority.json",
        )


def test_validate_pyxis_image_rejects_digest_mismatch(tmp_path: Path) -> None:
    """A changed image cannot reuse an old digest sidecar."""
    image, _ = _image(tmp_path)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    authority = _authority(image, digest)
    image.chmod(0o600)
    image.write_bytes(b"hsqs" + b"changed")
    image.chmod(0o400)

    with pytest.raises(PyxisImageValidationError, match="digest"):
        validate_pyxis_image(
            image,
            expected_sha256=digest,
            provenance=authority,
        )


def test_build_pyxis_srun_command_uses_read_only_source_and_no_network(
    tmp_path: Path,
) -> None:
    """Pyxis receives fixed isolation flags and explicit mount modes."""
    image, digest = _image(tmp_path)
    authority = _authority(image, digest)
    source = tmp_path / "source"
    metadata = tmp_path / "metadata.git"
    scratch = tmp_path / "scratch"
    logs = tmp_path / "logs"
    for path in (source, metadata, scratch, logs):
        path.mkdir(parents=True)

    command = build_pyxis_srun_command(
        image=validate_pyxis_image(
            image,
            expected_sha256=digest,
            provenance=authority,
        ),
        source=source,
        git_metadata=metadata,
        scratch=scratch,
        pi_smoke_logs=logs,
        argv=("uv", "run", "pytest", "tests/unit"),
        environment={"HOME": str(scratch / "home"), "UV_OFFLINE": "1"},
        timeout_s=300,
    )

    assert command[0] == "srun"
    assert "--container-readonly" in command
    assert "--no-container-mount-home" in command
    assert "--container-unshare=net,ipc,uts" in command
    assert "--export=NONE" in command
    assert "--nodes=1" in command
    assert "--ntasks=1" in command
    assert "--cpus-per-task=2" in command
    assert "--mem=4096M" in command
    assert "--time=00:05:00" in command
    assert "--propagate=CPU,FSIZE,NPROC,NOFILE" in command
    assert f"--container-image={image.resolve()}" in command
    assert f"--container-workdir={source.resolve()}" in command
    mounts = next(value for value in command if value.startswith("--container-mounts="))
    assert f"{source.resolve()}:{source.resolve()}:ro" in mounts
    assert f"{metadata.resolve()}:{metadata.resolve()}:ro" in mounts
    assert f"{scratch.resolve()}:{scratch.resolve()}:rw" in mounts
    assert f"{logs.resolve()}:{(source / 'pi-smoke-logs').resolve()}:rw" in mounts
    assert command[command.index("/usr/bin/env") + 1] == "-i"
    assert "/usr/local/bin/uv" in command
    assert (
        digest
        in validate_pyxis_image(
            image,
            expected_sha256=digest,
            provenance=authority,
        ).sha256
    )


def test_validate_pyxis_quota_root_requires_finite_private_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A private one-GiB filesystem can hold Linux writable outputs."""
    tmp_path.chmod(0o700)
    monkeypatch.setattr(
        "hephaestus.automation.pyxis_artifact_io.os.statvfs",
        lambda _: SimpleNamespace(f_frsize=4096, f_blocks=262_144),
    )

    assert pyxis_boundary.validate_pyxis_quota_root(tmp_path) == tmp_path.resolve()


def test_validate_pyxis_quota_root_rejects_polling_only_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large host filesystem is not a hard writable-space quota."""
    tmp_path.chmod(0o700)
    monkeypatch.setattr(
        "hephaestus.automation.pyxis_artifact_io.os.statvfs",
        lambda _: SimpleNamespace(f_frsize=4096, f_blocks=262_145),
    )

    with pytest.raises(PyxisImageValidationError, match="hard quota"):
        pyxis_boundary.validate_pyxis_quota_root(tmp_path)


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
        "container_image": str((tmp_path / f"sha256-{'b' * 64}.sqsh").resolve()),
        "container_image_sha256": "b" * 64,
        "container_image_id": "sha256:" + ("c" * 64),
        "container_image_reference": "podman://sha256:" + ("c" * 64),
        "containerfile_sha256": "d" * 64,
        "container_source_revision": "e" * 40,
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
