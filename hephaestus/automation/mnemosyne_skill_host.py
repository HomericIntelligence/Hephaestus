"""Host-owned execution for Athena-equivalent Mnemosyne skills."""

from __future__ import annotations

import json
import re
import secrets
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from hephaestus.automation.athena_contract import (
    AthenaContractReceipt,
    load_athena_contract_receipt,
)
from hephaestus.automation.mnemosyne_binding import (
    MnemosyneBindingReceipt,
    MnemosyneBindingService,
)
from hephaestus.automation.mnemosyne_corpus import (
    MnemosyneCorpusError,
    MnemosyneCorpusResult,
    SkillSelection,
    read_selected_skill_corpus,
)
from hephaestus.automation.mnemosyne_delivery import (
    ExistingPullRequest,
    LearnDeliveryError,
    LearnDeliveryReceipt,
    LearnDeliveryRequest,
    LearnDeliveryService,
    valid_delivery_receipt,
)
from hephaestus.automation.pipeline.athena_skill_jobs import AthenaSkillRequest, AthenaSkillResult
from hephaestus.config.child_environments import build_git_child_env
from hephaestus.github.client import gh_call
from hephaestus.io.utils import write_secure
from hephaestus.utils import subprocess_registry
from hephaestus.utils.helpers import run_subprocess


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

    def __init__(self, service: LearnDeliveryService | None = None) -> None:
        """Use the concrete GitHub adapter unless a test seam supplies a service."""
        self._service = service or LearnDeliveryService(github=GitHubLearnDeliveryAdapter())

    def deliver_from_request(self, request: AthenaSkillRequest) -> LearnDeliveryReceipt:
        """Deliver the request's validated host-owned learning change."""
        raw = request.payload.get("learn_delivery")
        if not isinstance(raw, dict):
            raise LearnDeliveryError("learn delivery payload is required")
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
        return self._service.deliver(delivery)


def fence_untrusted_context(label: str, content: str, *, nonce: str | None = None) -> str:
    """Fence untrusted context with a nonce-bound boundary."""
    marker = nonce or secrets.token_hex(16)
    return (
        f"--- BEGIN UNTRUSTED {label} {marker} ---\n"
        f"{content}\n"
        f"--- END UNTRUSTED {label} {marker} ---"
    )


class DefaultCorpusReader:
    """Select and read a bounded Athena-compatible skill corpus.

    Pipeline requests carry issue context, rather than model-chosen filenames.
    The host therefore performs deterministic retrieval from the already-bound
    commit.  Explicit selections remain available only to closed callers that
    already have validated selection evidence.
    """

    _MAX_SELECTED_SKILLS = 5
    _TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]{2,}")
    _STOP_WORDS = frozenset(
        {
            "about",
            "after",
            "against",
            "also",
            "and",
            "are",
            "been",
            "before",
            "being",
            "between",
            "but",
            "can",
            "code",
            "for",
            "from",
            "has",
            "have",
            "into",
            "issue",
            "its",
            "not",
            "only",
            "our",
            "should",
            "that",
            "the",
            "their",
            "then",
            "this",
            "through",
            "with",
            "will",
            "would",
            "you",
            "your",
        }
    )

    def __init__(
        self,
        *,
        git_output: Callable[[Path, tuple[str, ...]], str] | None = None,
    ) -> None:
        """Initialize retrieval with an injectable committed-object reader."""
        self._git_output = git_output or self._subprocess_git_output

    @staticmethod
    def _subprocess_git_output(root: Path, argv: tuple[str, ...]) -> str:
        """Read one committed Git object while preserving fail-closed errors."""
        try:
            result = run_subprocess(
                ["git", *argv],
                env=build_git_child_env(),
                cwd=root,
                check=False,
                track_process_group=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MnemosyneCorpusError(f"Mnemosyne corpus search failed: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise MnemosyneCorpusError(
                f"Mnemosyne corpus search failed for {' '.join(argv)}: "
                f"{detail or result.returncode}"
            )
        return result.stdout or ""

    @classmethod
    def _is_flat_skill_path(cls, source: str) -> bool:
        """Return whether ``source`` is a contract-admitted flat skill path."""
        path = PurePosixPath(source)
        return bool(
            not path.is_absolute()
            and ".." not in path.parts
            and len(path.parts) == 2
            and path.parts[0] == "skills"
            and path.suffix == ".md"
            and not path.name.endswith(".notes.md")
        )

    @classmethod
    def _query_terms(cls, request: AthenaSkillRequest) -> tuple[str, ...]:
        """Extract semantic retrieval terms from the ordinary pipeline payload."""
        fields = (
            request.payload.get("issue_title", ""),
            request.payload.get("issue_body", ""),
            request.payload.get("context", ""),
        )
        terms = {
            token
            for value in fields
            if isinstance(value, str)
            for token in cls._TOKEN_RE.findall(value.casefold())
            if token not in cls._STOP_WORDS
        }
        return tuple(sorted(terms))

    @classmethod
    def _score_skill(cls, content: str, source: str, terms: tuple[str, ...]) -> tuple[int, str]:
        """Rank semantic outcome/constraint/failure matches before title wording."""
        lowered = content.casefold()
        source_name = Path(source).stem.casefold()
        matched: list[str] = []
        score = 0
        for term in terms:
            occurrences = lowered.count(term)
            if not occurrences:
                continue
            matched.append(term)
            # The whole committed entry is searched, including frontmatter
            # description/category/tags and trigger/result history.  Stronger
            # signals deliberately favor outcome, constraint, and failure
            # guidance over a coincidental filename match.
            score += min(occurrences, 3)
            if re.search(
                rf"(?im)^#+\s*.*(?:outcome|goal|desired|constraint|failure|failed|result|trigger).*{re.escape(term)}",
                content,
            ):
                score += 8
            if re.search(
                rf"(?im)^(?:description|category|tags|trigger|failure|result):.*{re.escape(term)}",
                content,
            ):
                score += 4
            if term in source_name:
                score += 1
        return score, ", ".join(matched[:4])

    def _explicit_selections(self, raw: object) -> tuple[SkillSelection, ...]:
        """Validate closed caller-provided selections instead of silently dropping them."""
        if not isinstance(raw, (list, tuple)):
            raise MnemosyneCorpusError("selected_skills must be a list of selection objects")
        selections: list[SkillSelection] = []
        for item in raw:
            if not isinstance(item, dict):
                raise MnemosyneCorpusError("selected_skills contains a non-object selection")
            name, source, reason = item.get("name"), item.get("source"), item.get("reason")
            if (
                not isinstance(name, str)
                or not isinstance(source, str)
                or not isinstance(reason, str)
            ):
                raise MnemosyneCorpusError(
                    "selected_skills entries require string name, source, and reason"
                )
            selections.append(SkillSelection(name=name, source=source, reason=reason))
        return tuple(selections)

    def _select_from_bound_corpus(
        self,
        request: AthenaSkillRequest,
        binding: MnemosyneBindingReceipt,
    ) -> tuple[SkillSelection, ...]:
        """Search and rank flat skills from the immutable bound commit."""
        terms = self._query_terms(request)
        if not terms:
            return ()
        paths = self._git_output(
            Path(binding.root),
            ("ls-tree", "-r", "--name-only", binding.commit_sha, "--", "skills"),
        ).splitlines()
        ranked: list[tuple[int, str, str, str]] = []
        for source in paths:
            if not self._is_flat_skill_path(source):
                continue
            content = self._git_output(
                Path(binding.root), ("show", f"{binding.commit_sha}:{source}")
            )
            score, matches = self._score_skill(content, source, terms)
            if score:
                ranked.append((score, source, Path(source).stem, matches))
        ranked.sort(key=lambda entry: (-entry[0], entry[1]))
        return tuple(
            SkillSelection(
                name=name,
                source=source,
                reason=f"Matched intended outcome, constraint, or failure terms: {matches}",
            )
            for _score, source, name, matches in ranked[: self._MAX_SELECTED_SKILLS]
        )

    def read(
        self,
        request: AthenaSkillRequest,
        binding: MnemosyneBindingReceipt,
        contract: AthenaContractReceipt,
    ) -> MnemosyneCorpusResult:
        """Read supplied selections or retrieve them from the bound corpus."""
        raw_selections = request.payload.get("selected_skills")
        selections = (
            self._explicit_selections(raw_selections)
            if raw_selections is not None
            else self._select_from_bound_corpus(request, binding)
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
        self.delivery_service = delivery_service or DefaultLearnDeliveryBackend()

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
