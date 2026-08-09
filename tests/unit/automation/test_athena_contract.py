"""Tests for pinned Athena skill-contract receipts."""
# ruff: noqa: D103

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from hephaestus.agents.pi_plugins import (
    InventoryResult,
    PiPreflightResult,
    load_pi_package_catalog,
)
from hephaestus.automation.athena_contract import (
    AthenaContractError,
    AthenaContractReceipt,
    assert_athena_contract_matches,
    load_athena_contract_receipt,
)

FIXTURE_ROOT = Path("tests/fixtures/athena_contract/v0.4.0")


def _expected_contract() -> dict[str, str]:
    return json.loads((FIXTURE_ROOT / "contract.json").read_text(encoding="utf-8"))


def _preflight(root: Path) -> PiPreflightResult:
    """Build a ready preflight receipt bound to the fixture checkout."""
    inventory = InventoryResult(
        ready=True,
        status="ready",
        roots={"athena": root},
        scopes={"athena": "user"},
    )
    return PiPreflightResult.ready_result(inventory)


def _fixture_git_content(_root: Path, _commit: str, relative: Path) -> bytes:
    """Return the immutable fixture content that represents the pinned tree."""
    return (FIXTURE_ROOT / relative).read_bytes()


def test_receipt_binds_fixture_to_catalog_pinned_athena_contract() -> None:
    catalog = load_pi_package_catalog()

    receipt = load_athena_contract_receipt(
        contract_root=FIXTURE_ROOT,
        catalog=catalog,
        preflight=_preflight(FIXTURE_ROOT),
        pinned_content_reader=_fixture_git_content,
        trust_source="fixture:v0.4.0",
    )

    assert receipt == AthenaContractReceipt(**_expected_contract())
    assert receipt.athena_repository == catalog.packages[0].identity
    assert receipt.athena_commit == catalog.packages[0].pin
    assert receipt.requires_flat_skill_corpus is True
    assert receipt.requires_pr_backed_learning is True


def test_receipt_refuses_content_that_differs_from_preflight_pinned_checkout(
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
            preflight=_preflight(copied),
            pinned_content_reader=_fixture_git_content,
            trust_source="fixture:v0.4.0",
        )


def test_missing_contract_file_fails_closed(tmp_path: Path) -> None:
    copied = tmp_path / "contract"
    shutil.copytree(FIXTURE_ROOT, copied)
    (copied / "docs" / "dependency-resolution.md").unlink()

    with pytest.raises(AthenaContractError, match="dependency-resolution"):
        load_athena_contract_receipt(
            contract_root=copied,
            preflight=_preflight(copied),
            pinned_content_reader=_fixture_git_content,
        )


def test_receipt_rejects_contract_root_outside_preflight_proven_package(tmp_path: Path) -> None:
    with pytest.raises(AthenaContractError, match="does not match Pi preflight"):
        load_athena_contract_receipt(
            contract_root=tmp_path,
            preflight=_preflight(FIXTURE_ROOT),
            pinned_content_reader=_fixture_git_content,
        )


def test_contract_predicate_rejects_receipt_checksum_drift() -> None:
    receipt = AthenaContractReceipt(**_expected_contract())

    with pytest.raises(AthenaContractError, match="advise_sha256"):
        assert_athena_contract_matches(
            replace(receipt, advise_sha256="0" * 64), _expected_contract()
        )
