"""Tests for pinned Athena skill-contract receipts."""
# ruff: noqa: D103

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.automation.athena_contract import (
    AthenaContractError,
    AthenaContractReceipt,
    assert_athena_contract_matches,
    load_athena_contract_receipt,
)

FIXTURE_ROOT = Path("tests/fixtures/athena_contract/v0.5.0")
CONTRACT_MODULE = Path("hephaestus/automation/athena_contract.py")


def _expected_contract() -> dict[str, str]:
    return json.loads((FIXTURE_ROOT / "contract.json").read_text(encoding="utf-8"))


def test_receipt_binds_fixture_to_provider_neutral_athena_contract() -> None:
    receipt = load_athena_contract_receipt(
        contract_root=FIXTURE_ROOT,
        trust_source="fixture:v0.5.0",
    )

    assert receipt == AthenaContractReceipt(**_expected_contract())
    assert receipt.requires_flat_skill_corpus is True
    assert receipt.requires_pr_backed_learning is True


def test_default_receipt_needs_no_harness_or_contract_checkout() -> None:
    with patch(
        "hephaestus.agents.pi_plugins.preflight_pi_environment",
        side_effect=AssertionError("Athena contract loading must not preflight Pi"),
    ) as preflight:
        receipt = load_athena_contract_receipt()

    expected = _expected_contract()
    assert receipt.athena_repository == expected["athena_repository"]
    assert receipt.athena_commit == expected["athena_commit"]
    assert receipt.advise_sha256 == expected["advise_sha256"]
    assert receipt.learn_sha256 == expected["learn_sha256"]
    assert receipt.dependency_resolution_sha256 == expected["dependency_resolution_sha256"]
    assert receipt.trust_source == "hephaestus-athena-contract:v0.5.0"
    preflight.assert_not_called()


def test_contract_module_does_not_depend_on_pi_or_agent_runtime() -> None:
    source = CONTRACT_MODULE.read_text(encoding="utf-8")

    assert "hephaestus.agents.pi" not in source
    assert "hephaestus.agents.runtime" not in source
    assert "preflight_pi_environment" not in source


def test_trust_source_override_requires_verified_contract_root() -> None:
    with pytest.raises(AthenaContractError, match="requires a verified root"):
        load_athena_contract_receipt(trust_source="unverified")


def test_manifest_rejects_non_string_fields(tmp_path: Path) -> None:
    manifest = tmp_path / "athena_contract_manifest.json"
    invalid = _expected_contract()
    invalid["athena_repository"] = ["HomericIntelligence/Athena"]  # type: ignore[assignment]
    manifest.write_text(json.dumps(invalid), encoding="utf-8")

    with (
        patch("hephaestus.automation.athena_contract._CONTRACT_MANIFEST", manifest),
        pytest.raises(AthenaContractError, match=r"fields must be strings.*athena_repository"),
    ):
        load_athena_contract_receipt()


def test_receipt_refuses_content_that_differs_from_packaged_contract(
    tmp_path: Path,
) -> None:
    copied = tmp_path / "contract"
    shutil.copytree(FIXTURE_ROOT, copied)
    (copied / "skills" / "advise" / "SKILL.md").write_text(
        "drifted\n",
        encoding="utf-8",
    )

    with pytest.raises(AthenaContractError, match=r"pinned Athena content mismatch.*advise"):
        load_athena_contract_receipt(
            contract_root=copied,
            trust_source="fixture:v0.5.0",
        )


def test_missing_contract_file_fails_closed(tmp_path: Path) -> None:
    copied = tmp_path / "contract"
    shutil.copytree(FIXTURE_ROOT, copied)
    (copied / "docs" / "dependency-resolution.md").unlink()

    with pytest.raises(AthenaContractError, match="dependency-resolution"):
        load_athena_contract_receipt(
            contract_root=copied,
        )


def test_receipt_rejects_contract_root_with_wrong_files(tmp_path: Path) -> None:
    with pytest.raises(AthenaContractError, match="missing Athena contract file"):
        load_athena_contract_receipt(
            contract_root=tmp_path,
        )


def test_contract_predicate_rejects_receipt_checksum_drift() -> None:
    receipt = AthenaContractReceipt(**_expected_contract())

    with pytest.raises(AthenaContractError, match="advise_sha256"):
        assert_athena_contract_matches(
            replace(receipt, advise_sha256="0" * 64), _expected_contract()
        )
