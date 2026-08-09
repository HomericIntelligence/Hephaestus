"""Deterministic fixture coverage for Pi Athena/Mnemosyne host semantics."""
# ruff: noqa: D101, D103

from __future__ import annotations

from pathlib import Path

from hephaestus.automation.athena_contract import AthenaContractReceipt
from hephaestus.automation.mnemosyne_binding import MnemosyneBindingError, MnemosyneBindingReceipt
from hephaestus.automation.mnemosyne_corpus import MnemosyneCorpusResult
from hephaestus.automation.mnemosyne_delivery import LearnDeliveryReceipt
from hephaestus.automation.mnemosyne_skill_host import MnemosyneSkillHost
from hephaestus.automation.pipeline.athena_skill_jobs import AthenaSkillRequest


def _contract() -> AthenaContractReceipt:
    return AthenaContractReceipt(
        athena_repository="github.com/HomericIntelligence/Athena",
        athena_commit="a" * 40,
        advise_sha256="1" * 64,
        learn_sha256="2" * 64,
        dependency_resolution_sha256="3" * 64,
        trust_source="fixture",
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
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def bind(self, *, contract: AthenaContractReceipt) -> MnemosyneBindingReceipt:
        del contract
        if self.fail:
            raise MnemosyneBindingError("trust failure")
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
            context="## Selected Team Skills\n\nUse the existing helper.",
            blocks=(),
            evidence={"selected_paths": ["skills/helper.md"]},
        )


class Delivery:
    def __init__(self, *, drift: bool = False) -> None:
        self.drift = drift

    def deliver_from_request(self, request: AthenaSkillRequest) -> LearnDeliveryReceipt:
        del request
        return LearnDeliveryReceipt(
            repository="acme/Mnemosyne",
            branch="skill/helper",
            base_branch="main",
            commit_sha="c" * 40,
            pr_url="https://github.com/acme/Mnemosyne/pull/2",
            pr_number=2,
            readback_head_sha=("d" * 40 if self.drift else "c" * 40),
            validation_evidence=("pytest",),
            final_disposition="create",
        )


def _request(kind: str, tmp_path: Path) -> AthenaSkillRequest:
    return AthenaSkillRequest(
        kind=kind,
        repo="HomericIntelligence/Hephaestus",
        issue=9,
        agent="pi",
        model="default",
        cwd=tmp_path,
        timeout_s=60,
        payload={"context": "fixture"},
    )


def test_successful_advise_fixture(tmp_path: Path) -> None:
    host = MnemosyneSkillHost(
        contract_loader=_contract,
        binding_service=Binding(),
        corpus_reader=Corpus(),
    )

    result = host.execute(_request("advise", tmp_path))

    assert result.ok
    assert "Use the existing helper" in result.context
    assert result.receipt["binding"]["trust_basis"] == "canonical upstream"


def test_successful_pr_backed_learn_fixture(tmp_path: Path) -> None:
    host = MnemosyneSkillHost(
        contract_loader=_contract,
        binding_service=Binding(),
        delivery_service=Delivery(),
    )

    result = host.execute(_request("learn", tmp_path))

    assert result.ok
    assert result.delivery_receipt is not None
    assert result.delivery_receipt["pr_url"].endswith("/2")


def test_missing_delivery_request_and_trust_failure_fail_closed(tmp_path: Path) -> None:
    missing_delivery = MnemosyneSkillHost(contract_loader=_contract, binding_service=Binding())
    trust_failure = MnemosyneSkillHost(
        contract_loader=_contract, binding_service=Binding(fail=True)
    )

    assert (
        missing_delivery.execute(_request("learn", tmp_path)).error
        == "learn delivery payload is required"
    )
    assert trust_failure.execute(_request("advise", tmp_path)).error == "trust failure"


def test_interrupted_or_drifted_recovery_path_fails_closed(tmp_path: Path) -> None:
    host = MnemosyneSkillHost(
        contract_loader=_contract,
        binding_service=Binding(),
        delivery_service=Delivery(drift=True),
    )

    result = host.execute(_request("learn", tmp_path))

    assert result.error == "learn delivery receipt invalid"
