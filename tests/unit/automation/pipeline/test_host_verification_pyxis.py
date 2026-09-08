"""Tests for the Linux Pyxis host-verification boundary."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from hephaestus.automation.pipeline.host_verification_pyxis import (
    PyxisImageValidationError,
    build_pyxis_environment,
    build_pyxis_srun_command,
    stage_verified_pyxis_image,
    validate_pyxis_image,
    validate_pyxis_quota_root,
)
from hephaestus.automation.pipeline.stages.pr_review_receipts import (
    _host_verification_receipt_matches,
)
from hephaestus.automation.pipeline.stages.pr_review_verification import _HostVerificationSpec
from hephaestus.automation.pyxis_artifact_io import (
    CrossNodePathBinding,
    bind_cross_node_root,
)


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

    staged = stage_verified_pyxis_image(metadata, stage_root)
    try:
        assert staged.path == stage_root / f"sha256-{digest}.sqsh"
        assert hashlib.sha256(staged.path.read_bytes()).hexdigest() == digest
        assert staged.path.stat().st_mode & 0o777 == 0o400
    finally:
        assert staged.launch_binding is not None
        staged.launch_binding.close()


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

    first = stage_verified_pyxis_image(metadata, stage_root)
    second = stage_verified_pyxis_image(metadata, stage_root)
    try:
        assert second.path == first.path
        assert hashlib.sha256(second.path.read_bytes()).hexdigest() == digest
    finally:
        assert first.launch_binding is not None
        assert second.launch_binding is not None
        first.launch_binding.close()
        second.launch_binding.close()


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
        stage_verified_pyxis_image(metadata, stage_root)

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
        stage_verified_pyxis_image(metadata, stage_root)

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
        stage_verified_pyxis_image(metadata, stage_root)


@pytest.mark.parametrize("replace_root", (False, True))
def test_staged_image_binding_rejects_post_staging_replacement(
    tmp_path: Path, replace_root: bool
) -> None:
    """The retained binding rejects a leaf or root swap before launch."""
    image, digest = _image(tmp_path)
    authority = _authority(image, digest)
    stage_root = tmp_path / "stage"
    stage_root.mkdir(mode=0o700)
    staged = stage_verified_pyxis_image(
        validate_pyxis_image(image, expected_sha256=digest, provenance=authority),
        stage_root,
    )
    binding = staged.launch_binding
    assert binding is not None
    try:
        if replace_root:
            moved = stage_root.with_name("original-stage")
            stage_root.rename(moved)
            stage_root.mkdir(mode=0o700)
        else:
            replacement = stage_root / "replacement.sqsh"
            replacement.write_bytes(b"hsqs" + b"replacement")
            replacement.chmod(0o400)
            os.replace(replacement, staged.path)

        with pytest.raises(OSError, match="cross-node"):
            binding.revalidate()
    finally:
        binding.close()


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
        "hephaestus.automation.pyxis_artifact_io.os.fstatvfs",
        lambda _: SimpleNamespace(
            f_bsize=4096,
            f_frsize=4096,
            f_blocks=262_144,
            f_fsid=1,
            f_flag=0,
            f_namemax=255,
        ),
    )

    assert validate_pyxis_quota_root(tmp_path) == tmp_path.resolve()


def test_validate_pyxis_quota_root_rejects_polling_only_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large host filesystem is not a hard writable-space quota."""
    tmp_path.chmod(0o700)
    monkeypatch.setattr(
        "hephaestus.automation.pyxis_artifact_io.os.fstatvfs",
        lambda _: SimpleNamespace(
            f_bsize=4096,
            f_frsize=4096,
            f_blocks=262_145,
            f_fsid=1,
            f_flag=0,
            f_namemax=255,
        ),
    )

    with pytest.raises(PyxisImageValidationError, match="hard quota"):
        validate_pyxis_quota_root(tmp_path)


def test_validate_pyxis_quota_root_rejects_untrusted_ancestry(tmp_path: Path) -> None:
    """A group-writable ancestor cannot authorize a cross-node mount."""
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o770)
    shared.chmod(0o770)
    quota = shared / "quota"
    quota.mkdir(mode=0o700)

    with pytest.raises(PyxisImageValidationError, match="quota root"):
        validate_pyxis_quota_root(quota)


def test_cross_node_root_accepts_root_owned_sticky_temporary_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root-owned sticky temporary directory safely separates user entries."""
    original_lstat = Path.lstat
    sticky_ancestor = tmp_path.parent

    def linux_temporary_lstat(path: Path) -> os.stat_result | SimpleNamespace:
        if path == sticky_ancestor:
            return SimpleNamespace(st_mode=0o41777, st_uid=0)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", linux_temporary_lstat)

    with bind_cross_node_root(tmp_path) as binding:
        binding.revalidate()


def test_cross_node_root_rejects_root_owned_writable_nonsticky_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root-owned writable directory needs sticky entry protection."""
    original_lstat = Path.lstat
    writable_ancestor = tmp_path.parent

    def writable_lstat(path: Path) -> os.stat_result | SimpleNamespace:
        if path == writable_ancestor:
            return SimpleNamespace(st_mode=0o40777, st_uid=0)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", writable_lstat)

    with pytest.raises(OSError, match="ancestry is not trusted"):
        bind_cross_node_root(tmp_path)


def test_retained_quota_binding_revalidates_the_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed filesystem identity stops launch-time quota reuse."""
    tmp_path.chmod(0o700)
    original = os.statvfs(tmp_path)
    filesystem_id = [original.f_fsid]
    monkeypatch.setattr(
        "hephaestus.automation.pyxis_artifact_io.os.fstatvfs",
        lambda _: SimpleNamespace(
            f_bsize=original.f_bsize,
            f_frsize=4096,
            f_blocks=262_144,
            f_fsid=filesystem_id[0],
            f_flag=original.f_flag,
            f_namemax=original.f_namemax,
        ),
    )
    binding = validate_pyxis_quota_root(tmp_path, retain_binding=True)
    assert isinstance(binding, CrossNodePathBinding)
    filesystem_id[0] += 1
    try:
        with pytest.raises(OSError, match="filesystem changed"):
            binding.revalidate()
    finally:
        binding.close()


def test_build_pyxis_environment_is_scrubbed_and_source_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container receives only fixed offline variables and source path."""
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)
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
        "RUFF_CACHE_DIR": str((scratch / "cache" / "ruff").resolve()),
        "COVERAGE_FILE": str((scratch / "cache" / ".coverage").resolve()),
        "PYTHONPYCACHEPREFIX": str((scratch / "cache" / "pycache").resolve()),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
        "PYTHONPATH": str(source.resolve()),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    assert "GH_TOKEN" not in environment
    assert "GITHUB_TOKEN" not in environment


def test_build_pyxis_environment_prepares_writable_runtime_paths(tmp_path: Path) -> None:
    """Tools can write temporary files and caches within the declared scratch."""
    scratch = tmp_path / "scratch"
    environment = build_pyxis_environment(source=tmp_path / "source", scratch=scratch)

    for name in ("HOME", "TMPDIR", "XDG_CACHE_HOME"):
        directory = Path(environment[name])
        with tempfile.TemporaryFile(dir=directory) as stream:
            stream.write(b"runtime output")
        assert directory.is_relative_to(scratch)
    assert Path(environment["COVERAGE_FILE"]).is_relative_to(scratch)
    assert Path(environment["RUFF_CACHE_DIR"]).is_relative_to(scratch)
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["PYTEST_ADDOPTS"] == "-p no:cacheprovider"


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


@pytest.mark.parametrize(
    ("help_text", "expected"),
    [
        ("      --container-unshare=NS,...\n        Unshare namespaces.\n", True),
        ("      --container-unshare NS,...\n", True),
        ("      --container-unshare\n", True),
        ("      --container-unshare-extra=NS\n", False),
        ("Description mentions --container-unshare=NS\n", False),
        ("      --container-image=PATH\n", False),
        ("", False),
    ],
)
def test_pyxis_help_requires_exact_option(help_text: str, expected: bool) -> None:
    """Only a declared namespace option satisfies the runtime prerequisite."""
    from hephaestus.automation.pipeline.host_verification_pyxis import (
        pyxis_help_supports_namespace_isolation,
    )

    assert pyxis_help_supports_namespace_isolation(help_text) is expected


@pytest.mark.parametrize(
    "allocation_id", ["", "0", "-1", "+1", "01", "1_2", "1,2", "１２", 1, None]
)
def test_pyxis_placement_rejects_invalid_allocation(allocation_id: object) -> None:
    """Placement requires one positive ASCII decimal allocation ID."""
    from hephaestus.automation.pipeline.host_verification_pyxis import PyxisExecutionPlacement

    with pytest.raises(ValueError, match="allocation"):
        PyxisExecutionPlacement(allocation_id=allocation_id, node="node-1")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "node",
    [
        "",
        "node[1-2]",
        "node1,node2",
        "node1 node2",
        "node/1",
        "-node",
        "node-",
        "node..example",
        "node\n",
        "é",
        "a" * 64,
        ".node",
        "node.",
        None,
        1,
    ],
)
def test_pyxis_placement_rejects_invalid_node(node: object) -> None:
    """Placement accepts one hostname without scheduler list syntax."""
    from hephaestus.automation.pipeline.host_verification_pyxis import PyxisExecutionPlacement

    with pytest.raises(ValueError, match="node"):
        PyxisExecutionPlacement(allocation_id="123", node=node)  # type: ignore[arg-type]


@pytest.mark.parametrize("node", ["node-1", "node1.cluster.example"])
def test_pyxis_placement_adds_only_explicit_scheduler_pair(tmp_path: Path, node: str) -> None:
    """Explicit placement adds two flags and preserves the default command."""
    from dataclasses import FrozenInstanceError
    from functools import partial

    from hephaestus.automation.pipeline.host_verification_pyxis import PyxisExecutionPlacement

    placement = PyxisExecutionPlacement(allocation_id="123", node=node)
    with pytest.raises(FrozenInstanceError):
        placement.node = "other"  # type: ignore[misc]
    image, digest = _image(tmp_path)
    metadata = validate_pyxis_image(
        image, expected_sha256=digest, provenance=_authority(image, digest)
    )
    build_command = partial(
        build_pyxis_srun_command,
        image=metadata,
        source=tmp_path,
        git_metadata=tmp_path,
        scratch=tmp_path,
        pi_smoke_logs=tmp_path,
        argv=("true",),
        environment={},
        timeout_s=30,
    )
    default = build_command()
    explicit = build_command(placement=placement)
    pair = ("--jobid=123", f"--nodelist={node}")
    index = explicit.index(pair[0])
    assert explicit[index : index + 2] == pair
    assert index < explicit.index(f"--container-image={metadata.path}")
    assert explicit[:index] + explicit[index + 2 :] == default
