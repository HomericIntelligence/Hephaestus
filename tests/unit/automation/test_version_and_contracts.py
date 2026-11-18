"""Behavioral tests for the pinned version module and Athena contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hephaestus._version as version_module
from hephaestus.automation.athena_contract import (
    AthenaContractError,
    AthenaContractReceipt,
    _load_manifest,
    assert_athena_contract_matches,
    load_athena_contract_receipt,
)


def test_package_version_is_well_formed() -> None:
    """Generated version metadata has known types."""
    assert isinstance(version_module.__version__, str)
    assert version_module.__version__
    assert isinstance(version_module.version_tuple, tuple)
    assert version_module.__commit_id__ is None or isinstance(version_module.__commit_id__, str)


def test_package_version_matches_tuple_and_commit() -> None:
    """Version and version_tuple agree with each other on the dev suffix scheme."""
    assert version_module.version == version_module.__version__
    assert version_module.version_tuple == version_module.__version_tuple__
    assert version_module.__commit_id__ == version_module.commit_id


_VALID_COMMIT = "a" * 40
_VALID_HEX = "b" * 64


def _manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: dict[str, object]) -> Path:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    import hephaestus.automation.athena_contract as ac

    monkeypatch.setattr(ac, "_CONTRACT_MANIFEST", manifest)
    return manifest


def _valid_raw() -> dict[str, object]:
    return {
        "athena_repository": "HomericIntelligence/Athena",
        "athena_commit": _VALID_COMMIT,
        "advise_sha256": _VALID_HEX,
        "learn_sha256": _VALID_HEX,
        "dependency_resolution_sha256": _VALID_HEX,
        "trust_source": "athena@commit/agent-contract-v1.0.0",
    }


def test_load_manifest_valid_default() -> None:
    """The packaged manifest is readable and well-formed."""
    receipt = _load_manifest()
    assert receipt.athena_repository
    assert isinstance(receipt, AthenaContractReceipt)


def test_load_manifest_rejects_invalid_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A malformed commit hex rejects the manifest."""
    raw = _valid_raw()
    raw["athena_commit"] = "not-a-sha"
    _manifest(monkeypatch, tmp_path, raw)
    with pytest.raises(AthenaContractError):
        _load_manifest()


def test_load_manifest_rejects_missing_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing mandatory field rejects the manifest."""
    raw = _valid_raw()
    del raw["advise_sha256"]
    _manifest(monkeypatch, tmp_path, raw)
    with pytest.raises(AthenaContractError):
        _load_manifest()


def test_load_manifest_rejects_non_string_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-string field rejects the manifest."""
    raw = _valid_raw()
    raw["advise_sha256"] = 3
    _manifest(monkeypatch, tmp_path, raw)
    with pytest.raises(AthenaContractError):
        _load_manifest()


def test_load_manifest_rejects_non_hex_sha(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An invalid SHA-256 hex rejects the manifest."""
    raw = _valid_raw()
    raw["advise_sha256"] = "z" * 64
    _manifest(monkeypatch, tmp_path, raw)
    with pytest.raises(AthenaContractError):
        _load_manifest()


def test_load_manifest_rejects_empty_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty repository identifier rejects the manifest."""
    raw = _valid_raw()
    raw["athena_repository"] = ""
    _manifest(monkeypatch, tmp_path, raw)
    with pytest.raises(AthenaContractError):
        _load_manifest()


def test_load_athena_contract_receipt_valid(tmp_path: Path) -> None:
    """The trust root must verify against the pinned SHA-256s."""
    import hashlib

    skill_dir = tmp_path / "skills"
    (skill_dir / "advise").mkdir(parents=True)
    (skill_dir / "learn").mkdir()
    (tmp_path / "docs").mkdir()
    contents = {
        "skills/advise/SKILL.md": b"advise desk\n",
        "skills/learn/SKILL.md": b"learn bed\n",
        "docs/dependency-resolution.md": b"dep doc body\n",
    }
    import hephaestus.automation.athena_contract as ac

    raw = _valid_raw()
    manifest = tmp_path / "manifest.json"
    for relative, body in contents.items():
        path = tmp_path / relative
        path.write_bytes(body)
        for field, candidate in ac._CONTRACT_FILES.items():
            if candidate.as_posix() == relative:
                raw[field] = hashlib.sha256(body).hexdigest()

    ac_orig = ac._CONTRACT_MANIFEST
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    ac._CONTRACT_MANIFEST = manifest
    try:
        loaded = load_athena_contract_receipt(contract_root=tmp_path, trust_source="op@v0")
        assert loaded.trust_source == "op@v0"
    finally:
        ac._CONTRACT_MANIFEST = ac_orig


def test_load_contract_rejects_mismatching_root(tmp_path: Path) -> None:
    """A root that does not match the pinned hashes rejects the contract."""
    import hephaestus.automation.athena_contract as ac

    for relative in ac._CONTRACT_FILES.values():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"wrong")
    with pytest.raises(AthenaContractError):
        load_athena_contract_receipt(contract_root=tmp_path)


def test_load_contract_rejects_trust_source_without_root() -> None:
    """Trust source without a verified root rejects the contract."""
    with pytest.raises(AthenaContractError):
        load_athena_contract_receipt(trust_source="op@v0")


def test_load_contract_rejects_empty_trust_source(tmp_path: Path) -> None:
    """An empty trust source rejects the contract."""
    import hephaestus.automation.athena_contract as ac

    for relative in ac._CONTRACT_FILES.values():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
    with pytest.raises(AthenaContractError):
        load_athena_contract_receipt(contract_root=tmp_path, trust_source="   ")


def test_assert_athena_contract_matches_failure() -> None:
    """Mismatch between receipt and expected metadata rejects the contract."""
    receipt = AthenaContractReceipt(
        athena_repository="HomericIntelligence/Athena",
        athena_commit=_VALID_COMMIT,
        advise_sha256=_VALID_HEX,
        learn_sha256=_VALID_HEX,
        dependency_resolution_sha256=_VALID_HEX,
        trust_source="t",
    )
    with pytest.raises(AthenaContractError) as excinfo:
        assert_athena_contract_matches(receipt, {"athena_commit": "x" * 40})
    assert "contract" in str(excinfo.value).lower() or "athena_commit" in str(excinfo.value)


def test_failed_contract_root_resolution(tmp_path: Path) -> None:
    """An unresolvable root path rejects the contract."""
    with pytest.raises(AthenaContractError):
        load_athena_contract_receipt(contract_root=tmp_path / "definitely-missing-root")
