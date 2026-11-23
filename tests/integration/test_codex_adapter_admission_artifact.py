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

pytestmark = [
    pytest.mark.integration,
    pytest.mark.artifact,
    pytest.mark.codex_release_artifact,
]

_FIXTURE_ENV = "HEPHAESTUS_CODEX_SIGSTORE_FIXTURE_ROOT"
_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "sigstore" / "codex-sigstore-fixture.json"
)


def _load_manifest() -> dict[str, Any]:
    """Load the reviewed manifest that fixes the release identity."""
    try:
        document = json.loads(_MANIFEST_PATH.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("the Codex Sigstore fixture manifest is unavailable") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 2:
        raise RuntimeError("the Codex Sigstore fixture manifest is invalid")
    return document


_MANIFEST = _load_manifest()
_RELEASE = _MANIFEST["release"]
_TAG = _RELEASE["tag"]
_TARGET = _RELEASE["target"]
_REPOSITORY = _RELEASE["repository"]
_REKOR = _MANIFEST["rekor"]
_REKOR_LOG_ID = _REKOR["log_id"]
_REKOR_LOG_INDEX = _REKOR["log_index"]
_REKOR_INTEGRATION_TIME = _REKOR["integration_time"]


def _asset(suffix: str) -> dict[str, Any]:
    """Return the single release asset record with one name suffix."""
    matches = [record for record in _MANIFEST["assets"] if record["name"].endswith(suffix)]
    if len(matches) != 1:
        raise RuntimeError(f"the fixture manifest must name one {suffix} asset")
    return matches[0]


def _retained(suffix: str) -> dict[str, Any]:
    """Return the single retained evidence record with one name suffix."""
    matches = [record for record in _MANIFEST["retained"] if record["name"].endswith(suffix)]
    if len(matches) != 1:
        raise RuntimeError(f"the fixture manifest must name one {suffix} object")
    return matches[0]


_ARCHIVE = _asset(".zst")
_BUNDLE = _asset(".sigstore")
_ELF = _MANIFEST["extracted_elf"]
_ARCHIVE_NAME = _ARCHIVE["name"]
_ELF_NAME = _ELF["name"]
_BUNDLE_NAME = _BUNDLE["name"]
_ARCHIVE_SHA256 = _ARCHIVE["sha256"]
_ELF_SHA256 = _ELF["sha256"]
_BUNDLE_SHA256 = _BUNDLE["sha256"]

_ARTIFACT_FIELDS = {
    "codex_archive_path": _ARCHIVE_NAME,
    "extracted_elf_path": _ELF_NAME,
    "sigstore_bundle_path": _BUNDLE_NAME,
    "trusted_root_path": _retained("trusted-root.json")["name"],
    "rekor_public_key_path": _retained("rekor.pub")["name"],
    "rekor_checkpoint_path": _retained("rekor.checkpoint")["name"],
    "rekor_inclusion_proof_path": _retained("rekor.proof")["name"],
}

_CONTAINER_FIXTURE_ALIASES = (
    Path(f"/codex-sigstore/{_TAG}"),
    Path(f"/workspace/build/test-fixtures/codex-sigstore/{_TAG}"),
)


def _next_release_tag(tag: str) -> str:
    """Return a different release tag of the same shape."""
    prefix, _, patch = tag.rpartition(".")
    if not prefix or not patch.isdigit():
        raise RuntimeError(f"the fixture release tag is invalid: {tag}")
    return f"{prefix}.{int(patch) + 1}"


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
    if resolved.name != _TAG:
        raise RuntimeError(f"{_FIXTURE_ENV} must select the manifest release tag")
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
        "codex_release_tag": _TAG,
        "codex_archive_asset": _ARCHIVE_NAME,
        "codex_sigstore_asset": _BUNDLE_NAME,
        "codex_target": _TARGET,
        "fulcio_certificate_issuer": "O=sigstore.dev, CN=sigstore-intermediate",
        "workflow_certificate_identity": (
            f"https://github.com/{_REPOSITORY}/.github/workflows/rust-release.yml@refs/tags/{_TAG}"
        ),
        "oidc_issuer": "https://token.actions.githubusercontent.com",
        "rekor_log_id": _REKOR_LOG_ID,
        "rekor_log_index": _REKOR_LOG_INDEX,
        "rekor_integration_time": _REKOR_INTEGRATION_TIME,
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
        payload["codex_release_tag"] = _next_release_tag(_TAG)
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
