"""Host-owned execution for Athena-equivalent Mnemosyne skills."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from hephaestus.automation.athena_contract import (
    AthenaContractReceipt,
    load_athena_contract_receipt,
)
from hephaestus.automation.github_api import gh_call
from hephaestus.automation.mnemosyne_binding import (
    MnemosyneBindingError,
    MnemosyneBindingReceipt,
    MnemosyneBindingService,
)
from hephaestus.automation.mnemosyne_corpus import MnemosyneCorpusResult
from hephaestus.automation.mnemosyne_corpus_reader import DefaultCorpusReader
from hephaestus.automation.mnemosyne_delivery import (
    ExistingPullRequest,
    LearnDeliveryError,
    LearnDeliveryReceipt,
    LearnDeliveryRequest,
    LearnDeliveryService,
    valid_delivery_receipt,
)
from hephaestus.automation.mnemosyne_learning_preparation import (
    MnemosyneLearningPreparationService,
)
from hephaestus.automation.pipeline.athena_skill_jobs import AthenaSkillRequest, AthenaSkillResult
from hephaestus.io.utils import write_secure
from hephaestus.utils import subprocess_registry


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

    def deliver_from_request(
        self,
        request: AthenaSkillRequest,
        binding: MnemosyneBindingReceipt,
    ) -> LearnDeliveryReceipt:
        """Deliver a learning change and return a PR-backed receipt."""


class GitHubLearnDeliveryAdapter:
    """Closed ``gh`` adapter for host-owned Mnemosyne learning delivery."""

    def __init__(self, gh: Callable[..., Any] = gh_call) -> None:
        """Initialize the adapter with the shared rate-limited GitHub boundary."""
        self._gh = gh

    def create_pr(self, *, repository: str, head: str, base: str, title: str, body: str) -> int:
        """Create a Mnemosyne PR and return its server-issued number."""
        result = self._gh(
            [
                "pr",
                "create",
                "--repo",
                repository,
                "--head",
                head,
                "--base",
                base,
                "--title",
                title,
                "--body",
                body,
            ],
            check=False,
            track_process_group=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise LearnDeliveryError(f"Mnemosyne PR creation failed: {detail or result.returncode}")
        match = re.search(r"/pull/(\d+)(?:\s|$)", result.stdout or "")
        if match is None:
            raise LearnDeliveryError("Mnemosyne PR creation returned no PR URL")
        return int(match.group(1))

    def read_pr_head(self, *, repository: str, number: int) -> tuple[str, str]:
        """Read back a Mnemosyne PR URL and immutable head SHA."""
        result = self._gh(
            ["pr", "view", str(number), "--repo", repository, "--json", "url,headRefOid"],
            check=False,
            track_process_group=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise LearnDeliveryError(f"Mnemosyne PR readback failed: {detail or result.returncode}")
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise LearnDeliveryError("Mnemosyne PR readback returned malformed JSON") from exc
        if not isinstance(data, dict):
            raise LearnDeliveryError("Mnemosyne PR readback returned non-object JSON")
        url, head_sha = data.get("url"), data.get("headRefOid")
        if not isinstance(url, str) or not url or not isinstance(head_sha, str):
            raise LearnDeliveryError("Mnemosyne PR readback lacks URL or head SHA")
        return url, head_sha

    def read_existing_pr(self, *, repository: str, number: int) -> ExistingPullRequest:
        """Bind an open PR's source repository, ref, and immutable head SHA."""
        if isinstance(number, bool) or number <= 0:
            raise LearnDeliveryError("Mnemosyne existing PR number is invalid")
        result = self._gh(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                repository,
                "--json",
                "url,state,baseRefName,headRefName,headRefOid,headRepository",
            ],
            check=False,
            track_process_group=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise LearnDeliveryError(
                f"Mnemosyne existing PR binding failed: {detail or result.returncode}"
            )
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise LearnDeliveryError("Mnemosyne existing PR returned malformed JSON") from exc
        if not isinstance(data, dict):
            raise LearnDeliveryError("Mnemosyne existing PR returned non-object JSON")
        source = data.get("headRepository")
        source_repository = source.get("nameWithOwner") if isinstance(source, dict) else None
        required = {
            "url": data.get("url"),
            "state": data.get("state"),
            "base_ref": data.get("baseRefName"),
            "source_ref": data.get("headRefName"),
            "head_sha": data.get("headRefOid"),
            "source_repository": source_repository,
        }
        if any(not isinstance(value, str) or not value for value in required.values()):
            raise LearnDeliveryError("Mnemosyne existing PR lacks required source binding fields")
        head_sha = required["head_sha"]
        if not isinstance(head_sha, str) or re.fullmatch(r"[0-9a-f]{40}", head_sha) is None:
            raise LearnDeliveryError("Mnemosyne existing PR has an invalid source head SHA")
        return ExistingPullRequest(
            repository=repository,
            number=number,
            url=required["url"],  # type: ignore[arg-type]
            state=required["state"],  # type: ignore[arg-type]
            base_ref=required["base_ref"],  # type: ignore[arg-type]
            source_repository=required["source_repository"],  # type: ignore[arg-type]
            source_ref=required["source_ref"],  # type: ignore[arg-type]
            head_sha=head_sha,
        )


def _required_string(data: Mapping[str, object], name: str) -> str:
    """Return a non-empty request string or reject a malformed delivery payload."""
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise LearnDeliveryError(f"learn delivery payload lacks non-empty {name}")
    return value


def _string_tuple(data: Mapping[str, object], name: str) -> tuple[str, ...]:
    """Return a non-empty string tuple from a JSON delivery payload field."""
    value = data.get(name)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise LearnDeliveryError(f"learn delivery payload lacks non-empty {name}")
    return tuple(value)


class DefaultLearnDeliveryBackend:
    """Convert the closed Athena request payload into host-owned PR delivery."""

    def __init__(
        self,
        service: LearnDeliveryService | None = None,
        preparation: MnemosyneLearningPreparationService | None = None,
        gh_extra_path_root: Path | None = None,
    ) -> None:
        """Use the concrete GitHub adapter unless a test seam supplies a service."""
        self._service = service or LearnDeliveryService(
            github=GitHubLearnDeliveryAdapter(), gh_extra_path_root=gh_extra_path_root
        )
        self._preparation = preparation or MnemosyneLearningPreparationService(
            gh_extra_path_root=gh_extra_path_root
        )

    def deliver_from_request(
        self,
        request: AthenaSkillRequest,
        binding: MnemosyneBindingReceipt,
    ) -> LearnDeliveryReceipt:
        """Deliver the request's validated host-owned learning change."""
        has_intent = "learning_intent" in request.payload
        has_delivery = "learn_delivery" in request.payload
        if has_intent and has_delivery:
            raise LearnDeliveryError(
                "learn request must contain exactly one of learning_intent or learn_delivery"
            )
        if not has_intent and not has_delivery:
            raise LearnDeliveryError("learn delivery payload is required")
        if has_intent:
            raw_intent = request.payload.get("learning_intent")
            if not isinstance(raw_intent, dict):
                raise LearnDeliveryError("learning intent payload must be an object")
            delivery = self._preparation.prepare(raw_intent, binding)
            self._validate_binding(delivery, binding)
            return self._service.deliver(delivery)
        raw = request.payload.get("learn_delivery")
        if not isinstance(raw, dict):
            raise LearnDeliveryError("learn delivery payload must be an object")
        existing_pr = raw.get("existing_pr_number")
        if existing_pr is not None and (
            isinstance(existing_pr, bool) or not isinstance(existing_pr, int)
        ):
            raise LearnDeliveryError("learn delivery payload has invalid existing_pr_number")
        delivery = LearnDeliveryRequest(
            repository=_required_string(raw, "repository"),
            worktree_path=Path(_required_string(raw, "worktree_path")),
            branch=_required_string(raw, "branch"),
            base_branch=_required_string(raw, "base_branch"),
            allowed_paths=_string_tuple(raw, "allowed_paths"),
            commit_message=_required_string(raw, "commit_message"),
            pr_title=_required_string(raw, "pr_title"),
            pr_body=_required_string(raw, "pr_body"),
            disposition=_required_string(raw, "disposition"),
            validation_evidence=_string_tuple(raw, "validation_evidence"),
            existing_pr_number=existing_pr,
        )
        self._validate_binding(delivery, binding)
        raise LearnDeliveryError("learning_deferred:source_evidence_required")

    @staticmethod
    def _validate_binding(
        delivery: LearnDeliveryRequest,
        binding: MnemosyneBindingReceipt,
    ) -> None:
        """Require request repository/base to equal the trusted binding."""
        if delivery.repository != binding.repository:
            raise LearnDeliveryError("learn delivery repository does not match Mnemosyne binding")
        if delivery.base_branch != binding.default_branch:
            raise LearnDeliveryError("learn delivery base branch does not match Mnemosyne binding")


def fence_untrusted_context(label: str, content: str, *, nonce: str | None = None) -> str:
    """Fence untrusted context with a nonce-bound boundary."""
    marker = nonce or secrets.token_hex(16)
    return (
        f"--- BEGIN UNTRUSTED {label} {marker} ---\n"
        f"{content}\n"
        f"--- END UNTRUSTED {label} {marker} ---"
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
        gh_extra_path_root: Path | None = None,
    ) -> None:
        """Initialize the host with injectable contract, binding, and delivery services."""
        self.contract_loader = contract_loader or self._load_contract
        self.binding_service = binding_service or MnemosyneBindingService(
            gh_extra_path_root=gh_extra_path_root
        )
        self.corpus_reader = corpus_reader or DefaultCorpusReader()
        self.delivery_service = delivery_service or DefaultLearnDeliveryBackend(
            gh_extra_path_root=gh_extra_path_root
        )

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
                delivery = self.delivery_service.deliver_from_request(request, binding)
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
        except MnemosyneBindingError as exc:
            return AthenaSkillResult(kind=str(request.kind), error=f"{exc.failure_kind}: {exc}")
        except Exception as exc:
            return AthenaSkillResult(kind=str(request.kind), error=str(exc))

    @staticmethod
    def cancel() -> None:
        """Stop active host-owned subprocess groups during forced shutdown."""
        subprocess_registry.terminate_all()

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
