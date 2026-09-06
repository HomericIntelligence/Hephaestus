"""Tests for the local Pyxis image preparation command."""

from __future__ import annotations

import hashlib
import importlib.util
import json
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
    """Create the minimum valid CI build context."""
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci" / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("engine", "expected_uri"),
    [("podman", "podman://hephaestus-ci:local"), ("docker", "dockerd://hephaestus-ci:local")],
)
def test_prepare_image_uses_local_engine_import_and_writes_attestation(
    tmp_path: Path, engine: str, expected_uri: str
) -> None:
    """Podman and Docker exports use local Enroot schemes and attest provenance."""
    module = _module()
    calls: list[tuple[str, ...]] = []
    output = tmp_path / "out" / "hephaestus-ci.sqsh"
    image_id = "sha256:" + ("c" * 64)

    def runner(argv: tuple[str, ...], **_: Any) -> Any:
        calls.append(argv)
        if argv[0] == "enroot":
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"hsqs" + b"image")
        stdout = image_id + "\n" if argv[1:3] == ("image", "inspect") else ""
        return type("Completed", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    result = module.prepare_image(
        repo_root=_repo(tmp_path),
        output=output,
        rebuild=True,
        engine=engine,
        runner=runner,
    )

    assert calls[0] == (engine, "build", "-f", "ci/Containerfile", "-t", "hephaestus-ci:local", ".")
    assert calls[1] == ("enroot", "import", "--output", str(output), expected_uri)
    assert calls[2] == (engine, "image", "inspect", "--format={{.Id}}", "hephaestus-ci:local")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    assert result["sha256"] == digest
    assert output.with_name(f"{output.name}.sha256").read_text() == f"{digest}  {output.name}\n"
    contract = json.loads(output.with_suffix(".contract.json").read_text())
    assert contract["container_image_sha256"] == digest
    assert contract["container_image_id"] == image_id
    assert contract["local_import_uri"] == expected_uri


def test_prepare_image_rejects_unverifiable_local_image_id(tmp_path: Path) -> None:
    """Preparation fails when the local engine returns no immutable image ID."""
    module = _module()
    output = tmp_path / "out" / "hephaestus-ci.sqsh"

    def runner(argv: tuple[str, ...], **_: Any) -> Any:
        if argv[0] == "enroot":
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"hsqs" + b"image")
        return type(
            "Completed", (), {"returncode": 0, "stdout": "not-an-image-id\n", "stderr": ""}
        )()

    with pytest.raises(module.HostVerificationImagePreparationError, match="image ID"):
        module.prepare_image(
            repo_root=_repo(tmp_path),
            output=output,
            rebuild=True,
            engine="podman",
            runner=runner,
        )


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
    """Preparation must not follow a caller-controlled output symlink."""
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


def test_prepare_image_does_not_accept_registry_import_uri(tmp_path: Path) -> None:
    """The local import URI helper rejects registry references."""
    module = _module()

    with pytest.raises(module.HostVerificationImagePreparationError, match="local"):
        module._validate_local_import_uri("docker://registry.example/ci:latest")
