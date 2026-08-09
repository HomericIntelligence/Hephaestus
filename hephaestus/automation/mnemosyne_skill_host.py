"""Host-owned execution for Athena-equivalent Mnemosyne skills."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from hephaestus.automation.athena_contract import (
    AthenaContractReceipt,
    load_athena_contract_receipt,
)
from hephaestus.automation.mnemosyne_binding import (
    MnemosyneBindingReceipt,
    MnemosyneBindingService,
)
from hephaestus.automation.mnemosyne_corpus import (
    MnemosyneCorpusResult,
    SkillSelection,
    read_selected_skill_corpus,
)
from hephaestus.automation.mnemosyne_delivery import (
    LearnDeliveryReceipt,
    valid_delivery_receipt,
)
from hephaestus.automation.pipeline.athena_skill_jobs import AthenaSkillRequest, AthenaSkillResult
from hephaestus.io.utils import write_secure


class BindingService(Protocol):
    """Checkout binding surface used by the skill host."""

    def bind(self, *, contract: AthenaContractReceipt) -> MnemosyneBindingReceipt:
        """Bind Mnemosyne and return a receipt."""


class CorpusReader(Protocol):
    """Selected corpus reader surface."""

    def read(
        self,
        request: AthenaSkillRequest,
        binding: MnemosyneBindingReceipt,
        contract: AthenaContractReceipt,
    ) -> MnemosyneCorpusResult:
        """Read selected corpus for a request."""


class LearnDeliveryBackend(Protocol):
    """Learning delivery surface used by the skill host."""

    def deliver_from_request(self, request: AthenaSkillRequest) -> LearnDeliveryReceipt:
        """Deliver a learning change and return a PR-backed receipt."""


def fence_untrusted_context(label: str, content: str, *, nonce: str | None = None) -> str:
    """Fence untrusted context with a nonce-bound boundary."""
    marker = nonce or secrets.token_hex(16)
    return (
        f"--- BEGIN UNTRUSTED {label} {marker} ---\n"
        f"{content}\n"
        f"--- END UNTRUSTED {label} {marker} ---"
    )


class DefaultCorpusReader:
    """Corpus reader that converts request payload selections to skill blobs."""

    def read(
        self,
        request: AthenaSkillRequest,
        binding: MnemosyneBindingReceipt,
        contract: AthenaContractReceipt,
    ) -> MnemosyneCorpusResult:
        """Read selected skill blobs named by ``request.payload``."""
        selections = tuple(
            SkillSelection(
                name=str(item.get("name", "")),
                source=str(item.get("source", "")),
                reason=str(item.get("reason", "")),
            )
            for item in request.payload.get("selected_skills", ())
            if isinstance(item, dict)
        )
        return read_selected_skill_corpus(
            root=Path(binding.root),
            binding=binding,
            contract=contract,
            selections=selections,
        )


class MnemosyneSkillHost:
    """Execute typed Athena skill requests through host-owned receipts."""

    def __init__(
        self,
        *,
        contract_loader: Callable[[], AthenaContractReceipt] | None = None,
        binding_service: BindingService | None = None,
        corpus_reader: CorpusReader | None = None,
        delivery_service: LearnDeliveryBackend | None = None,
    ) -> None:
        """Initialize the host with injectable contract, binding, and delivery services."""
        self.contract_loader = contract_loader or self._load_contract
        self.binding_service = binding_service or MnemosyneBindingService()
        self.corpus_reader = corpus_reader or DefaultCorpusReader()
        self.delivery_service = delivery_service

    def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
        """Execute ``advise`` or ``learn`` and return a typed result envelope."""
        try:
            contract = self.contract_loader()
            binding = self.binding_service.bind(contract=contract)
            if request.kind == "advise":
                corpus = self.corpus_reader.read(request, binding, contract)
                receipt = {
                    "contract": contract.to_dict(),
                    "binding": binding.to_dict(),
                    "corpus": corpus.evidence,
                }
                return AthenaSkillResult(
                    kind="advise",
                    context=corpus.context,
                    receipt=receipt,
                )
            if request.kind == "learn":
                if self.delivery_service is None:
                    return AthenaSkillResult(
                        kind="learn",
                        receipt={"contract": contract.to_dict(), "binding": binding.to_dict()},
                        error="learn delivery backend unavailable",
                    )
                delivery = self.delivery_service.deliver_from_request(request)
                if not valid_delivery_receipt(delivery):
                    return AthenaSkillResult(
                        kind="learn",
                        receipt={"contract": contract.to_dict(), "binding": binding.to_dict()},
                        error="learn delivery receipt invalid",
                    )
                return AthenaSkillResult(
                    kind="learn",
                    receipt={"contract": contract.to_dict(), "binding": binding.to_dict()},
                    delivery_receipt=delivery.to_dict(),
                )
            return AthenaSkillResult(
                kind=str(request.kind), error=f"unsupported Athena skill {request.kind!r}"
            )
        except Exception as exc:
            return AthenaSkillResult(kind=str(request.kind), error=str(exc))

    @staticmethod
    def _load_contract() -> AthenaContractReceipt:
        return load_athena_contract_receipt()


def persist_athena_skill_result(path: Path, result: AthenaSkillResult) -> Path:
    """Persist a typed skill result receipt as JSON."""
    payload = {
        "kind": result.kind,
        "context": result.context,
        "receipt": result.receipt,
        "delivery_receipt": result.delivery_receipt,
        "error": result.error,
    }
    # ``AthenaSkillResult`` is currently dataclass-backed; keep this branch so
    # future subclasses with dataclass receipts serialize predictably.
    if hasattr(result, "__dataclass_fields__"):
        payload = asdict(result)
    write_secure(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path
