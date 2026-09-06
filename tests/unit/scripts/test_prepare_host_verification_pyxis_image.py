"""Tests for the local Pyxis image preparation command."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "prepare_host_verification_pyxis_image.py"
)


def _module() -> ModuleType:
    """Load the standalone preparation script."""
    spec = importlib.util.spec_from_file_location("prepare_host_verification_pyxis_image", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo(tmp_path: Path) -> Path:
    """Create the minimum committed CI build context."""
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci" / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
    subprocess.run(("git", "init", "-q", str(tmp_path)), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "config", "user.name", "Test"), check=True)
    subprocess.run(
        ("git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"), check=True
    )
    subprocess.run(("git", "-C", str(tmp_path), "add", "ci/Containerfile"), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "commit", "-q", "-m", "test"), check=True)
    return tmp_path


def _engine_runner(*, output: Path, image_id: str, calls: list[tuple[str, ...]]) -> Any:
    """Return a fake OCI/Enroot runner with real local Git commands."""

    def runner(argv: tuple[str, ...], **kwargs: Any) -> Any:
        calls.append(argv)
        if argv[0] == "git":
            return subprocess.run(argv, **kwargs)
        if argv[0] == "enroot":
            Path(argv[3]).write_bytes(b"hsqs" + b"image")
        stdout = image_id + "\n" if argv[1:3] == ("image", "inspect") else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    return runner


@pytest.mark.parametrize(("engine", "scheme"), [("podman", "podman"), ("docker", "dockerd")])
def test_prepare_image_uses_content_addressed_import_and_private_authority(
    tmp_path: Path, engine: str, scheme: str
) -> None:
    """The export uses immutable local bytes and a separate private authority."""
    module = _module()
    calls: list[tuple[str, ...]] = []
    output = tmp_path / "out" / "hephaestus-ci.sqsh"
    image_id = "sha256:" + ("c" * 64)

    result = module.prepare_image(
        repo_root=_repo(tmp_path),
        output=output,
        rebuild=True,
        engine=engine,
        runner=_engine_runner(output=output, image_id=image_id, calls=calls),
    )

    build = next(call for call in calls if len(call) > 1 and call[1] == "build")
    imported = next(call for call in calls if call[0] == "enroot")
    assert build[:4] == (engine, "build", "-f", "ci/Containerfile")
    assert imported[-1] == f"{scheme}://{image_id}"
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    authority_path = output.with_suffix(".authority.json")
    authority = json.loads(authority_path.read_text())
    assert result["sha256"] == digest
    assert result["reused"] is False
    assert authority == {
        "schema": "hephaestus-host-verification-pyxis-v2",
        "containerfile": "ci/Containerfile",
        "containerfile_sha256": hashlib.sha256(b"FROM scratch\n").hexdigest(),
        "container_image_id": image_id,
        "container_image_reference": f"{scheme}://{image_id}",
        "source_revision": subprocess.run(
            ("git", "-C", str(tmp_path), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "squashfs_sha256": digest,
    }
    assert stat.S_IMODE(output.stat().st_mode) == 0o400
    assert stat.S_IMODE(authority_path.stat().st_mode) == 0o400
    assert not output.with_name(f"{output.name}.sha256").exists()


def test_prepare_image_reuse_validates_exact_id_and_does_not_rewrite_authority(
    tmp_path: Path,
) -> None:
    """Reuse reads the frozen authority and checks its exact local image ID."""
    module = _module()
    calls: list[tuple[str, ...]] = []
    output = tmp_path / "out" / "hephaestus-ci.sqsh"
    image_id = "sha256:" + ("c" * 64)
    runner = _engine_runner(output=output, image_id=image_id, calls=calls)
    root = _repo(tmp_path)
    module.prepare_image(
        repo_root=root, output=output, rebuild=True, engine="podman", runner=runner
    )
    authority = output.with_suffix(".authority.json")
    before = (authority.read_bytes(), authority.stat().st_mtime_ns)

    result = module.prepare_image(
        repo_root=root, output=output, rebuild=False, engine="podman", runner=runner
    )

    assert result["reused"] is True
    assert (authority.read_bytes(), authority.stat().st_mtime_ns) == before


def test_prepare_image_reuse_rejects_substituted_engine_image(tmp_path: Path) -> None:
    """Reuse cannot report provenance when the content-addressed OCI ID changed."""
    module = _module()
    calls: list[tuple[str, ...]] = []
    output = tmp_path / "out" / "hephaestus-ci.sqsh"
    root = _repo(tmp_path)
    original = "sha256:" + ("c" * 64)
    module.prepare_image(
        repo_root=root,
        output=output,
        rebuild=True,
        engine="podman",
        runner=_engine_runner(output=output, image_id=original, calls=calls),
    )

    with pytest.raises(module.HostVerificationImagePreparationError, match="stale"):
        module.prepare_image(
            repo_root=root,
            output=output,
            engine="podman",
            runner=_engine_runner(output=output, image_id="sha256:" + ("d" * 64), calls=calls),
        )


def test_prepare_image_rejects_unverifiable_local_image_id(tmp_path: Path) -> None:
    """Preparation fails when the local engine returns no immutable image ID."""
    module = _module()
    output = tmp_path / "out" / "hephaestus-ci.sqsh"

    def runner(argv: tuple[str, ...], **kwargs: Any) -> Any:
        if argv[0] == "git":
            return subprocess.run(argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, "not-an-image-id\n", "")

    with pytest.raises(module.HostVerificationImagePreparationError, match="image ID"):
        module.prepare_image(
            repo_root=_repo(tmp_path),
            output=output,
            rebuild=True,
            engine="podman",
            runner=runner,
        )
    assert not output.with_suffix(".authority.json").exists()


def test_prepare_command_has_a_fixed_timeout(tmp_path: Path) -> None:
    """Every preparation subprocess has a fixed wall-clock timeout."""
    module = _module()
    seen: dict[str, object] = {}

    def runner(argv: tuple[str, ...], **kwargs: Any) -> Any:
        del argv
        seen.update(kwargs)
        return subprocess.CompletedProcess((), 0, "", "")

    module._run(("true",), cwd=tmp_path, runner=runner)
    assert seen["timeout"] == 1800


def test_prepare_image_rejects_symlinked_containerfile(tmp_path: Path) -> None:
    """A symlink cannot substitute an unreviewed CI build-context file."""
    module = _module()
    root = _repo(tmp_path)
    original = tmp_path / "outside-containerfile"
    original.write_text("FROM scratch\n", encoding="utf-8")
    (root / "ci" / "Containerfile").unlink()
    (root / "ci" / "Containerfile").symlink_to(original)

    with pytest.raises(module.HostVerificationImagePreparationError, match="regular"):
        module.prepare_image(repo_root=root, engine="podman", rebuild=True)


def test_prepare_image_rejects_symlinked_output_before_writing(tmp_path: Path) -> None:
    """Preparation does not follow a caller-controlled output symlink."""
    module = _module()
    root = _repo(tmp_path)
    outside = tmp_path / "outside.sqsh"
    outside.write_bytes(b"preserve")
    output = root / "build" / "host-verification" / "hephaestus-ci.sqsh"
    output.parent.mkdir(parents=True)
    output.symlink_to(outside)

    with pytest.raises(module.HostVerificationImagePreparationError, match="symlink"):
        module.prepare_image(repo_root=root, output=output, engine="podman", rebuild=True)

    assert outside.read_bytes() == b"preserve"


@pytest.mark.parametrize(
    "uri",
    (
        "docker://registry.example/ci:latest",
        "podman://hephaestus-ci:local",
        "dockerd://hephaestus-ci:local",
    ),
)
def test_prepare_image_rejects_mutable_or_registry_import_uri(tmp_path: Path, uri: str) -> None:
    """Only a content-addressed local import URI is valid."""
    module = _module()
    del tmp_path

    with pytest.raises(module.HostVerificationImagePreparationError, match="content-addressed"):
        module._validate_local_import_uri(uri)


def test_private_authority_write_does_not_follow_existing_symlink(tmp_path: Path) -> None:
    """The authority writer replaces its target and does not follow it."""
    module = _module()
    outside = tmp_path / "outside"
    outside.write_text("preserve", encoding="utf-8")
    authority = tmp_path / "authority.json"
    authority.symlink_to(outside)

    temporary = module._write_private_json(authority, {"key": "value"})
    os.replace(temporary, authority)

    assert outside.read_text(encoding="utf-8") == "preserve"
    assert json.loads(authority.read_text()) == {"key": "value"}
