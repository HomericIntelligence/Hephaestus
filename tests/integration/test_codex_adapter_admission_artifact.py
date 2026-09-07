"""Offline production-admission tests for the real Codex release evidence."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import platform
import secrets
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation import codex_adapter_admission as admission

pytestmark = [pytest.mark.integration, pytest.mark.artifact]

_FIXTURE_ENV = "HEPHAESTUS_CODEX_SIGSTORE_FIXTURE_ROOT"
_ARCHIVE_NAME = "codex-aarch64-unknown-linux-musl.zst"
_ELF_NAME = "codex-aarch64-unknown-linux-musl"
_BUNDLE_NAME = "codex-aarch64-unknown-linux-musl.sigstore"
_ARCHIVE_SHA256 = "7a148fb7e7ed8a4bfa3ac4ffe014336070398eff780893582afab9195c6652f7"
_ELF_SHA256 = "4d76e542c222ea8c75861d8c4ade60a1a332a63255ce1c60bdaebf7c2a2869e6"
_BUNDLE_SHA256 = "847b47e73068f86635c23ab5501647a93fab9ad0c450d6661a88481dfcd6d759"
_REKOR_LOG_ID = "c0d23d6ad406973f9559f3ba2d1ca01f84147d8ffc5b8445c224f98b9591801d"

_ARTIFACT_FIELDS = {
    "codex_archive_path": _ARCHIVE_NAME,
    "extracted_elf_path": _ELF_NAME,
    "sigstore_bundle_path": _BUNDLE_NAME,
    "trusted_root_path": "codex-production-trusted-root.json",
    "rekor_public_key_path": "codex-rekor.pub",
    "rekor_checkpoint_path": "codex-rekor.checkpoint",
    "rekor_inclusion_proof_path": "codex-rekor.proof",
}

_CONTAINER_FIXTURE_ALIASES = (
    Path("/codex-sigstore/rust-v0.153.4"),
    Path("/workspace/build/test-fixtures/codex-sigstore/rust-v0.153.4"),
)


def _sha256(path: Path) -> str:
    """Hash one fixture without loading the full executable into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _fixture_root() -> Path:
    """Return the required absolute, pre-provisioned artifact root."""
    configured = os.environ.get(_FIXTURE_ENV, "")
    path = Path(configured)
    if not configured or not path.is_absolute():
        raise RuntimeError(f"{_FIXTURE_ENV} must select an absolute provisioned path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{_FIXTURE_ENV} is not provisioned") from exc
    if resolved != path:
        raise RuntimeError(f"{_FIXTURE_ENV} must not use a symbolic link")
    for name in _ARTIFACT_FIELDS.values():
        artifact = resolved / name
        try:
            artifact_resolved = artifact.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(f"required Codex fixture is unavailable: {name}") from exc
        if (
            artifact_resolved != artifact
            or artifact_resolved.parent != resolved
            or not artifact.is_file()
        ):
            raise RuntimeError(f"required Codex fixture is unavailable: {name}")
    return resolved


def _regular(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _deployment_lock(tmp_path: Path) -> tuple[Path, str, dict[str, Any]]:
    """Create a valid adapter lock that selects the provisioned Codex evidence."""
    root = tmp_path / "offline"
    root.mkdir(mode=0o700)
    installed = root / "installed"
    files = {
        "example_adapter/__init__.py": b"def factory():\n    return object()\n",
        "example_adapter-1.0.dist-info/METADATA": b"Name: example-adapter\nVersion: 1.0\n",
        "example_adapter-1.0.dist-info/entry_points.txt": (
            b"[hephaestus.codex_isolation_adapters]\nproduction = example_adapter:factory\n"
        ),
        "example_adapter-1.0.dist-info/RECORD": (
            b"example_adapter/__init__.py,,\n"
            b"example_adapter-1.0.dist-info/METADATA,,\n"
            b"example_adapter-1.0.dist-info/entry_points.txt,,\n"
            b"example_adapter-1.0.dist-info/RECORD,,\n"
        ),
    }
    for relative, data in files.items():
        _regular(installed / relative, data)
    manifest = [
        {
            "path": relative,
            "sha256": _sha256(installed / relative),
            "size": (installed / relative).stat().st_size,
            "mode": (installed / relative).stat().st_mode & 0o777,
        }
        for relative in sorted(files)
    ]
    wheel = _regular(root / "example_adapter-1.0-py3-none-any.whl", b"wheel")
    guest = _regular(root / "guest.raw", b"signed guest")
    fixture = _fixture_root()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "adapter_distribution": "example-adapter",
        "adapter_version": "1.0",
        "wheel_path": str(wheel),
        "wheel_sha256": _sha256(wheel),
        "wheel_tags": ["py3", "none", "any"],
        "installed_tree_root": str(installed),
        "installed_tree_manifest": manifest,
        "installed_tree_sha256": hashlib.sha256(_canonical(manifest)).hexdigest(),
        "entry_point_group": "hephaestus.codex_isolation_adapters",
        "entry_point_name": "production",
        "entry_point_module": "example_adapter",
        "entry_point_factory": "factory",
        "adapter_api_version": 1,
        "request_schema_version": 1,
        "prepared_schema_version": 1,
        "result_schema_version": 1,
        "guest_image_path": str(guest),
        "guest_image_format": "raw",
        "guest_image_platform": "aarch64-linux",
        "guest_image_sha256": _sha256(guest),
        "codex_archive_path": str(fixture / _ARCHIVE_NAME),
        "codex_archive_sha256": _ARCHIVE_SHA256,
        "sigstore_bundle_path": str(fixture / _BUNDLE_NAME),
        "sigstore_bundle_sha256": _BUNDLE_SHA256,
        "extracted_elf_path": str(fixture / _ELF_NAME),
        "extracted_elf_sha256": _ELF_SHA256,
        "codex_release_tag": "rust-v0.153.4",
        "codex_archive_asset": _ARCHIVE_NAME,
        "codex_sigstore_asset": _BUNDLE_NAME,
        "codex_target": "aarch64-unknown-linux-musl",
        "fulcio_certificate_issuer": "O=sigstore.dev, CN=sigstore-intermediate",
        "workflow_certificate_identity": (
            "https://github.com/openai/codex/.github/workflows/"
            "rust-release.yml@refs/tags/rust-v0.153.4"
        ),
        "oidc_issuer": "https://token.actions.githubusercontent.com",
        "rekor_log_id": _REKOR_LOG_ID,
        "rekor_log_index": 2_717_156_140,
        "rekor_integration_time": 1_788_562_514,
    }
    for field, name in _ARTIFACT_FIELDS.items():
        payload[field] = str(fixture / name)
        payload[field.replace("_path", "_sha256")] = _sha256(fixture / name)
    lock = root / "deployment-lock.json"
    lock.write_bytes(_canonical(payload))
    lock.chmod(0o600)
    return lock, _sha256(lock), payload


def _deny_network(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("offline production admission used the network")


def _configure_production_host(
    monkeypatch: pytest.MonkeyPatch,
    imported: list[tuple[str, str]],
) -> None:
    """Select the production admission path and retain the import boundary."""

    def importer(_root: object, module_name: str, factory_name: str) -> object:
        imported.append((module_name, factory_name))
        return object()

    monkeypatch.setattr(socket, "create_connection", _deny_network)
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr(admission, "_virtualization_framework_available", lambda: True)
    monkeypatch.setattr(admission, "_default_importer", importer)


def _tampered_copy(source: Path, destination: Path) -> Path:
    """Copy one object with bounded memory and change exactly one byte."""
    with source.open("rb") as source_stream, destination.open("xb") as target_stream:
        shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
    destination.chmod(0o600)
    with destination.open("r+b") as stream:
        original = stream.read(1)
        if not original:
            raise AssertionError("a retained artifact must not be empty")
        stream.seek(0)
        stream.write(bytes((original[0] ^ 1,)))
        stream.flush()
        os.fsync(stream.fileno())
    return destination


def test_every_container_fixture_alias_is_read_only() -> None:
    """Each container path to the host fixture must reject a create."""
    fixture = _fixture_root()
    if fixture not in _CONTAINER_FIXTURE_ALIASES:
        raise AssertionError("the fixture root is not a reviewed container alias")
    for root in _CONTAINER_FIXTURE_ALIASES:
        if not root.is_dir():
            raise AssertionError(f"the container fixture alias is absent: {root}")
        probe = root / f".hephaestus-write-probe-{secrets.token_hex(16)}"
        if probe.exists():
            raise AssertionError(f"the read-only probe name already exists: {probe}")
        descriptor = -1
        try:
            descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            if exc.errno == errno.EROFS:
                continue
            raise AssertionError(
                f"the fixture alias failed with errno {exc.errno}, not EROFS: {root}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
                probe.unlink(missing_ok=True)
        raise AssertionError(f"the container fixture alias is writable: {root}")


def test_real_codex_evidence_passes_public_production_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All retained real release objects pass the public offline admission path."""
    lock, digest, _payload = _deployment_lock(tmp_path)
    imported: list[tuple[str, str]] = []
    _configure_production_host(monkeypatch, imported)

    result = admission.admit_codex_adapter(
        lock_path=lock,
        expected_sha256=digest,
        selected_entry_point="production",
    )

    assert result.lock.codex_archive_sha256 == _ARCHIVE_SHA256
    assert result.lock.extracted_elf_sha256 == _ELF_SHA256
    assert imported == [("example_adapter", "factory")]


@pytest.mark.parametrize("field", tuple(_ARTIFACT_FIELDS))
def test_each_changed_real_codex_object_fails_before_adapter_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    """A one-byte change in each retained object fails before adapter import."""
    lock, _digest, payload = _deployment_lock(tmp_path)
    original = Path(payload[field])
    payload[field] = str(_tampered_copy(original, tmp_path / f"changed-{original.name}"))
    lock.write_bytes(_canonical(payload))
    digest = _sha256(lock)
    imported: list[tuple[str, str]] = []
    _configure_production_host(monkeypatch, imported)

    with pytest.raises(admission.CodexAdapterAdmissionError):
        admission.admit_codex_adapter(
            lock_path=lock,
            expected_sha256=digest,
            selected_entry_point="production",
        )

    assert imported == []


@pytest.mark.parametrize("mismatch", ("digest", "selection", "identity"))
def test_real_codex_lock_mismatch_fails_before_adapter_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    """Each detached-lock mismatch fails before adapter import."""
    lock, digest, payload = _deployment_lock(tmp_path)
    selection = "production"
    if mismatch == "digest":
        digest = "0" * 64
    elif mismatch == "selection":
        selection = "other"
    else:
        payload["codex_release_tag"] = "rust-v0.153.5"
        lock.write_bytes(_canonical(payload))
        digest = _sha256(lock)
    imported: list[tuple[str, str]] = []
    _configure_production_host(monkeypatch, imported)

    with pytest.raises(admission.CodexAdapterAdmissionError):
        admission.admit_codex_adapter(
            lock_path=lock,
            expected_sha256=digest,
            selected_entry_point=selection,
        )

    assert imported == []
