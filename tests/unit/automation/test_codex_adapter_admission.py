"""Behavior tests for detached Codex adapter deployment admission."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _regular(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _deployment(tmp_path: Path) -> tuple[Path, str, dict[str, Any]]:
    root = tmp_path / "offline"
    root.mkdir(mode=0o700)
    installed = root / "installed"
    package = _regular(
        installed / "example_adapter" / "__init__.py",
        b"def factory():\n    return 'locked'\n",
    )
    metadata = _regular(
        installed / "example_adapter-1.0.dist-info" / "METADATA",
        b"Name: example-adapter\nVersion: 1.0\n",
    )
    entry_points = _regular(
        installed / "example_adapter-1.0.dist-info" / "entry_points.txt",
        b"[hephaestus.codex_isolation_adapters]\nproduction = example_adapter:factory\n",
    )
    record = _regular(
        installed / "example_adapter-1.0.dist-info" / "RECORD",
        (
            b"example_adapter/__init__.py,,\n"
            b"example_adapter-1.0.dist-info/METADATA,,\n"
            b"example_adapter-1.0.dist-info/entry_points.txt,,\n"
            b"example_adapter-1.0.dist-info/RECORD,,\n"
        ),
    )
    manifest = []
    for path in sorted(
        (package, metadata, entry_points, record),
        key=lambda item: item.relative_to(installed).as_posix(),
    ):
        manifest.append(
            {
                "path": path.relative_to(installed).as_posix(),
                "sha256": _sha256(path),
                "size": path.stat().st_size,
                "mode": path.stat().st_mode & 0o777,
            }
        )

    artifacts = {
        "wheel": _regular(root / "example_adapter-1.0-py3-none-any.whl", b"wheel"),
        "guest_image": _regular(root / "guest.raw", b"signed guest"),
        "codex_archive": _regular(root / "codex.zst", b"archive"),
        "sigstore_bundle": _regular(root / "codex.sigstore", b"bundle"),
        "extracted_elf": _regular(
            root / "codex",
            b"\x7fELF\x02\x01" + b"\0" * 12 + b"\xb7\0" + b"\0" * 44,
        ),
        "trusted_root": _regular(root / "trusted-root.json", b"trusted root"),
        "rekor_key": _regular(root / "rekor.pub", b"rekor key"),
        "rekor_checkpoint": _regular(root / "rekor.checkpoint", b"checkpoint"),
        "rekor_inclusion_proof": _regular(root / "rekor.proof", b"proof"),
    }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "adapter_distribution": "example-adapter",
        "adapter_version": "1.0",
        "wheel_path": str(artifacts["wheel"]),
        "wheel_sha256": _sha256(artifacts["wheel"]),
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
        "guest_image_path": str(artifacts["guest_image"]),
        "guest_image_format": "raw",
        "guest_image_platform": "aarch64-linux",
        "guest_image_sha256": _sha256(artifacts["guest_image"]),
        "codex_archive_path": str(artifacts["codex_archive"]),
        "codex_archive_sha256": _sha256(artifacts["codex_archive"]),
        "sigstore_bundle_path": str(artifacts["sigstore_bundle"]),
        "sigstore_bundle_sha256": _sha256(artifacts["sigstore_bundle"]),
        "extracted_elf_path": str(artifacts["extracted_elf"]),
        "extracted_elf_sha256": _sha256(artifacts["extracted_elf"]),
        "codex_release_tag": "rust-v0.153.4",
        "codex_archive_asset": "codex-aarch64-unknown-linux-musl.zst",
        "codex_sigstore_asset": "codex-aarch64-unknown-linux-musl.sigstore",
        "codex_target": "aarch64-unknown-linux-musl",
        "fulcio_certificate_issuer": "O=sigstore.dev, CN=sigstore-intermediate",
        "workflow_certificate_identity": "https://github.com/openai/codex/.github/workflows/rust-release.yml@refs/tags/rust-v0.153.4",
        "oidc_issuer": "https://token.actions.githubusercontent.com",
        "trusted_root_path": str(artifacts["trusted_root"]),
        "trusted_root_sha256": _sha256(artifacts["trusted_root"]),
        "rekor_public_key_path": str(artifacts["rekor_key"]),
        "rekor_public_key_sha256": _sha256(artifacts["rekor_key"]),
        "rekor_checkpoint_path": str(artifacts["rekor_checkpoint"]),
        "rekor_checkpoint_sha256": _sha256(artifacts["rekor_checkpoint"]),
        "rekor_inclusion_proof_path": str(artifacts["rekor_inclusion_proof"]),
        "rekor_inclusion_proof_sha256": _sha256(artifacts["rekor_inclusion_proof"]),
        "rekor_log_id": "a" * 64,
        "rekor_log_index": 1,
        "rekor_integration_time": 1,
    }
    lock = root / "deployment-lock.json"
    lock.write_bytes(_canonical(payload))
    lock.chmod(0o600)
    return lock, _sha256(lock), payload


def _module():
    name = "hephaestus.automation.codex_adapter_admission"
    assert importlib.util.find_spec(name) is not None, "adapter admission is not implemented"
    return importlib.import_module(name)


def _admit(module: Any, lock: Path, digest: str, *, importer: Any = None):
    return module.admit_codex_adapter(
        lock_path=lock,
        expected_sha256=digest,
        selected_entry_point="production",
        offline_verifier=lambda _lock: None,
        importer=importer or (lambda _root, _module, _factory: object()),
        host_platform="darwin",
        host_machine="arm64",
        virtualization_available=True,
    )


def test_tampered_detached_lock_fails_before_adapter_import(tmp_path: Path) -> None:
    """A changed lock fails before external adapter code can load."""
    lock, digest, _ = _deployment(tmp_path)
    lock.write_bytes(lock.read_bytes() + b" ")
    imported = False

    def importer(_root: str, _module: str, _factory: str) -> object:
        nonlocal imported
        imported = True
        return object()

    module = _module()
    with pytest.raises(module.CodexAdapterAdmissionError, match="deployment lock digest"):
        _admit(module, lock, digest, importer=importer)
    assert imported is False


def test_linked_deployment_lock_fails_before_adapter_import(tmp_path: Path) -> None:
    """A linked detached lock fails before external adapter code can load."""
    lock, digest, _ = _deployment(tmp_path)
    linked = tmp_path / "linked-lock.json"
    linked.symlink_to(lock)
    imported = False

    def importer(_root: str, _module: str, _factory: str) -> object:
        nonlocal imported
        imported = True
        return object()

    module = _module()
    with pytest.raises(module.CodexAdapterAdmissionError, match="deployment lock path"):
        _admit(module, linked, digest, importer=importer)
    assert imported is False


def test_unsafe_deployment_lock_parent_fails_before_adapter_import(tmp_path: Path) -> None:
    """A writable non-sticky parent cannot carry the detached trust anchor."""
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    lock, digest, _ = _deployment(unsafe)
    imported = False

    def importer(_root: str, _module: str, _factory: str) -> object:
        nonlocal imported
        imported = True
        return object()

    module = _module()
    with pytest.raises(module.CodexAdapterAdmissionError, match="path component"):
        _admit(module, lock, digest, importer=importer)
    assert imported is False


def test_lock_rejects_unsorted_installed_tree_manifest(tmp_path: Path) -> None:
    """The detached lock has one canonical sorted tree inventory."""
    lock, _, payload = _deployment(tmp_path)
    payload["installed_tree_manifest"].reverse()
    lock.write_bytes(_canonical(payload))
    module = _module()

    with pytest.raises(module.CodexAdapterAdmissionError, match="manifest"):
        _admit(module, lock, _sha256(lock))


def test_lock_rejects_wheel_tags_that_do_not_match_filename(tmp_path: Path) -> None:
    """The retained wheel name must contain the exact locked tag triple."""
    lock, _, payload = _deployment(tmp_path)
    payload["wheel_tags"] = ["cp313", "cp313", "macosx_14_0_arm64"]
    lock.write_bytes(_canonical(payload))
    module = _module()

    with pytest.raises(module.CodexAdapterAdmissionError, match="wheel tags"):
        _admit(module, lock, _sha256(lock))


def test_adapter_self_attestation_is_not_a_trust_anchor(tmp_path: Path) -> None:
    """An adapter identity must equal the host-verified lock identity."""
    lock, digest, _ = _deployment(tmp_path)
    module = _module()
    parsed = module.CodexAdapterDeploymentLockV1.from_bytes(lock.read_bytes())
    admission = module.CodexAdapterAdmission(
        lock=parsed,
        deployment_lock_sha256=digest,
        factory=object(),
    )

    with pytest.raises(module.CodexAdapterAdmissionError, match="adapter identity"):
        admission.validate_adapter_identity(
            distribution="other", version="1.0", installed_tree_sha256="0" * 64
        )


def test_complete_offline_deployment_imports_only_the_locked_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complete locked evidence admits the exact factory after verification."""
    lock, digest, payload = _deployment(tmp_path)
    module = _module()
    monkeypatch.setattr(module, "_ARCHIVE_SHA256", payload["codex_archive_sha256"])
    monkeypatch.setattr(module, "_SIGSTORE_SHA256", payload["sigstore_bundle_sha256"])
    imported: list[tuple[str, str]] = []
    factory = object()

    def importer(_root: str, module_name: str, factory_name: str) -> object:
        imported.append((module_name, factory_name))
        return factory

    admitted = _admit(module, lock, digest, importer=importer)

    assert admitted.factory is factory
    assert admitted.deployment_lock_sha256 == digest
    assert imported == [("example_adapter", "factory")]


def test_default_importer_loads_only_from_verified_installed_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambient same-name module cannot replace the locked adapter module."""
    lock, digest, payload = _deployment(tmp_path)
    shadow = tmp_path / "shadow"
    _regular(
        shadow / "example_adapter" / "__init__.py",
        b"def factory():\n    return 'shadow'\n",
    )
    monkeypatch.syspath_prepend(str(shadow))
    sys.modules.pop("example_adapter", None)
    module = _module()
    monkeypatch.setattr(module, "_ARCHIVE_SHA256", payload["codex_archive_sha256"])
    monkeypatch.setattr(module, "_SIGSTORE_SHA256", payload["sigstore_bundle_sha256"])

    admitted = _admit(module, lock, digest, importer=module._default_importer)

    assert admitted.factory() == "locked"


def test_installed_wheel_tree_mismatch_fails_before_adapter_import(tmp_path: Path) -> None:
    """Changed installed bytes fail before external adapter code can load."""
    lock, digest, payload = _deployment(tmp_path)
    Path(payload["installed_tree_root"], "example_adapter", "__init__.py").write_text(
        "factory = None\n", encoding="utf-8"
    )
    module = _module()

    with pytest.raises(module.CodexAdapterAdmissionError, match="installed tree"):
        _admit(module, lock, digest)


def test_guest_image_digest_mismatch_fails_before_adapter_import(tmp_path: Path) -> None:
    """A changed guest image fails before external adapter code can load."""
    lock, digest, payload = _deployment(tmp_path)
    Path(payload["guest_image_path"]).write_bytes(b"changed")
    module = _module()

    with pytest.raises(module.CodexAdapterAdmissionError, match="guest image"):
        _admit(module, lock, digest)


@pytest.mark.parametrize("field", ["codex_archive_path", "sigstore_bundle_path"])
def test_codex_archive_or_sigstore_digest_mismatch_fails_before_adapter_import(
    tmp_path: Path, field: str
) -> None:
    """Changed Codex release evidence fails before adapter import."""
    lock, digest, payload = _deployment(tmp_path)
    Path(payload[field]).write_bytes(b"changed")
    module = _module()

    with pytest.raises(module.CodexAdapterAdmissionError, match="Codex release artifact"):
        _admit(module, lock, digest)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("oidc_issuer", "https://issuer.invalid"),
        ("workflow_certificate_identity", "https://identity.invalid"),
        ("fulcio_certificate_issuer", "untrusted"),
    ],
)
def test_oidc_issuer_or_certificate_identity_mismatch_fails_before_adapter_import(
    tmp_path: Path, field: str, value: str
) -> None:
    """A different release identity fails before adapter import."""
    lock, _, payload = _deployment(tmp_path)
    payload[field] = value
    lock.write_bytes(_canonical(payload))
    module = _module()

    with pytest.raises(module.CodexAdapterAdmissionError, match="certificate identity"):
        _admit(module, lock, _sha256(lock))


@pytest.mark.parametrize("field", ["trusted_root_path", "rekor_checkpoint_path"])
def test_trust_root_or_rekor_mismatch_fails_before_adapter_import(
    tmp_path: Path, field: str
) -> None:
    """Changed offline trust evidence fails before adapter import."""
    lock, digest, payload = _deployment(tmp_path)
    Path(payload[field]).write_bytes(b"changed")
    module = _module()

    with pytest.raises(module.CodexAdapterAdmissionError, match="offline trust artifact"):
        _admit(module, lock, digest)


def test_rekor_evidence_is_bound_to_bundle_and_trusted_root(tmp_path: Path) -> None:
    """Retained Rekor bytes must equal the verified bundle and root values."""
    lock_path, _, _ = _deployment(tmp_path)
    module = _module()
    lock = module.CodexAdapterDeploymentLockV1.from_bytes(lock_path.read_bytes())
    proof_value = {
        "checkpoint": {"envelope": "checkpoint"},
        "hashes": [],
        "logIndex": "1",
        "rootHash": "cm9vdA==",
        "treeSize": "2",
    }

    class Proof:
        checkpoint = SimpleNamespace(envelope="checkpoint")

        @staticmethod
        def to_json() -> str:
            return json.dumps(proof_value)

    entry = SimpleNamespace(
        inclusion_proof=Proof(),
        integrated_time=1,
        log_id=SimpleNamespace(key_id=bytes.fromhex(lock.rekor_log_id)),
        log_index=1,
    )
    bundle = SimpleNamespace(log_entry=SimpleNamespace(_inner=entry))
    trusted_root = SimpleNamespace(
        _inner=SimpleNamespace(
            tlogs=[
                SimpleNamespace(
                    log_id=SimpleNamespace(key_id=bytes.fromhex(lock.rekor_log_id)),
                    public_key=SimpleNamespace(raw_bytes=b"rekor key"),
                )
            ]
        )
    )

    module._validate_locked_rekor_evidence(
        lock,
        bundle=bundle,
        trusted_root=trusted_root,
        public_key_bytes=b"rekor key",
        checkpoint_bytes=b"checkpoint",
        inclusion_proof_bytes=_canonical(proof_value),
    )

    with pytest.raises(module.CodexAdapterAdmissionError, match="Rekor evidence"):
        module._validate_locked_rekor_evidence(
            lock,
            bundle=bundle,
            trusted_root=trusted_root,
            public_key_bytes=b"different key",
            checkpoint_bytes=b"checkpoint",
            inclusion_proof_bytes=_canonical(proof_value),
        )
