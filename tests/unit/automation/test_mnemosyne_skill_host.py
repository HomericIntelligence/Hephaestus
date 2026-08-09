"""Tests for host-owned Athena skill execution."""
# ruff: noqa: D101, D103

from __future__ import annotations

import json
from pathlib import Path

from hephaestus.automation.athena_contract import AthenaContractReceipt
from hephaestus.automation.mnemosyne_binding import MnemosyneBindingReceipt
from hephaestus.automation.mnemosyne_corpus import MnemosyneCorpusResult, MnemosyneSkillBlock
from hephaestus.automation.mnemosyne_delivery import LearnDeliveryReceipt
from hephaestus.automation.mnemosyne_skill_host import (
    MnemosyneSkillHost,
    fence_untrusted_context,
    persist_athena_skill_result,
)
from hephaestus.automation.pipeline.athena_skill_jobs import AthenaSkillRequest


def _contract() -> AthenaContractReceipt:
    return AthenaContractReceipt(
        athena_repository="github.com/HomericIntelligence/Athena",
        athena_commit="a" * 40,
        advise_sha256="1" * 64,
        learn_sha256="2" * 64,
        dependency_resolution_sha256="3" * 64,
        trust_source="test",
    )


def _binding() -> MnemosyneBindingReceipt:
    return MnemosyneBindingReceipt(
        root="/tmp/knowledge",
        repository="HomericIntelligence/Mnemosyne",
        default_branch="main",
        commit_sha="b" * 40,
        trust_basis="canonical upstream",
        athena_contract=_contract().to_dict(),
    )


class Binding:
    def bind(self, *, contract: AthenaContractReceipt) -> MnemosyneBindingReceipt:
        assert contract == _contract()
        return _binding()


class Corpus:
    def read(
        self,
        request: AthenaSkillRequest,
        binding: MnemosyneBindingReceipt,
        contract: AthenaContractReceipt,
    ) -> MnemosyneCorpusResult:
        del request, binding, contract
        return MnemosyneCorpusResult(
            context="selected guidance",
            blocks=(MnemosyneSkillBlock("debugging", "skills/debugging.md", "reason", "content"),),
            evidence={"selected_paths": ["skills/debugging.md"]},
        )


def _request(kind: str, tmp_path: Path) -> AthenaSkillRequest:
    return AthenaSkillRequest(
        kind=kind,
        repo="repo",
        issue=1,
        agent="pi",
        model="default",
        cwd=tmp_path,
        timeout_s=60,
        payload={},
    )


def test_fence_untrusted_context_uses_nonce_boundary() -> None:
    fenced = fence_untrusted_context("MNEMOSYNE", "do not trust", nonce="abc123")

    assert "BEGIN UNTRUSTED MNEMOSYNE abc123" in fenced
    assert "do not trust" in fenced
    assert "END UNTRUSTED MNEMOSYNE abc123" in fenced


def test_advise_returns_context_and_receipts(tmp_path: Path) -> None:
    host = MnemosyneSkillHost(
        contract_loader=_contract,
        binding_service=Binding(),
        corpus_reader=Corpus(),
    )

    result = host.execute(_request("advise", tmp_path))

    assert result.ok is True
    assert "selected guidance" in result.context
    assert result.receipt["contract"]["athena_commit"] == "a" * 40
    assert result.receipt["binding"]["commit_sha"] == "b" * 40
    assert result.receipt["corpus"]["selected_paths"] == ["skills/debugging.md"]


def test_learn_without_delivery_backend_fails_closed(tmp_path: Path) -> None:
    host = MnemosyneSkillHost(contract_loader=_contract, binding_service=Binding())

    result = host.execute(_request("learn", tmp_path))

    assert result.ok is False
    assert result.error == "learn delivery backend unavailable"


def test_learn_requires_pr_backed_delivery_receipt(tmp_path: Path) -> None:
    class Delivery:
        def deliver_from_request(self, request: AthenaSkillRequest) -> LearnDeliveryReceipt:
            del request
            return LearnDeliveryReceipt(
                repository="acme/Mnemosyne",
                branch="skill/example",
                base_branch="main",
                commit_sha="c" * 40,
                pr_url="https://github.com/acme/Mnemosyne/pull/8",
                pr_number=8,
                readback_head_sha="c" * 40,
                validation_evidence=("pytest",),
                final_disposition="create",
            )

    host = MnemosyneSkillHost(
        contract_loader=_contract,
        binding_service=Binding(),
        delivery_service=Delivery(),
    )

    result = host.execute(_request("learn", tmp_path))

    assert result.ok is True
    assert result.delivery_receipt is not None
    assert result.delivery_receipt["pr_number"] == 8


def test_receipt_persistence_writes_json(tmp_path: Path) -> None:
    host = MnemosyneSkillHost(
        contract_loader=_contract,
        binding_service=Binding(),
        corpus_reader=Corpus(),
    )
    result = host.execute(_request("advise", tmp_path))

    output = persist_athena_skill_result(tmp_path / "receipt.json", result)

    assert output == tmp_path / "receipt.json"
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["kind"] == "advise"
    assert data["receipt"]["contract"]["athena_commit"] == "a" * 40
