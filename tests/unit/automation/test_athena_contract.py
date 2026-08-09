"""Tests for pinned Athena skill-contract receipts."""
# ruff: noqa: D103

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hephaestus.agents.pi_plugins import load_pi_package_catalog
from hephaestus.automation.athena_contract import (
    AthenaContractError,
    AthenaContractReceipt,
    assert_athena_contract_matches,
    load_athena_contract_receipt,
)

FIXTURE_ROOT = Path("tests/fixtures/athena_contract/v0.4.0")


def _expected_contract() -> dict[str, str]:
    return json.loads((FIXTURE_ROOT / "contract.json").read_text(encoding="utf-8"))


def test_receipt_binds_fixture_to_catalog_pinned_athena_contract() -> None:
    catalog = load_pi_package_catalog()

    receipt = load_athena_contract_receipt(
        contract_root=FIXTURE_ROOT,
        catalog=catalog,
        trust_source="fixture:v0.4.0",
    )

    assert receipt == AthenaContractReceipt(**_expected_contract())
    assert receipt.athena_repository == catalog.packages[0].identity
    assert receipt.athena_commit == catalog.packages[0].pin
    assert receipt.requires_flat_skill_corpus is True
    assert receipt.requires_pr_backed_learning is True


def test_contract_predicate_rejects_checksum_drift(tmp_path: Path) -> None:
    copied = tmp_path / "contract"
    shutil.copytree(FIXTURE_ROOT, copied)
    (copied / "skills" / "advise" / "SKILL.md").write_text(
        "drifted\n",
        encoding="utf-8",
    )

    receipt = load_athena_contract_receipt(
        contract_root=copied,
        trust_source="fixture:v0.4.0",
    )

    with pytest.raises(AthenaContractError, match="advise_sha256"):
        assert_athena_contract_matches(receipt, _expected_contract())


def test_missing_contract_file_fails_closed(tmp_path: Path) -> None:
    copied = tmp_path / "contract"
    shutil.copytree(FIXTURE_ROOT, copied)
    (copied / "docs" / "dependency-resolution.md").unlink()

    with pytest.raises(AthenaContractError, match="dependency-resolution"):
        load_athena_contract_receipt(contract_root=copied)
