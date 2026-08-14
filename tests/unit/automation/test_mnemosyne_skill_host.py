"""Tests for host-owned Athena skill execution."""
# ruff: noqa: D101, D103

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hephaestus.automation.athena_contract import AthenaContractReceipt
from hephaestus.automation.mnemosyne_binding import MnemosyneBindingReceipt
from hephaestus.automation.mnemosyne_corpus import (
    MnemosyneCorpusError,
    MnemosyneCorpusResult,
    MnemosyneSkillBlock,
    SkillSelection,
)
from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError, LearnDeliveryReceipt
from hephaestus.automation.mnemosyne_skill_host import (
    DefaultCorpusReader,
    GitHubLearnDeliveryAdapter,
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


def test_default_contract_loader_runs_advise_without_an_agent_harness(tmp_path: Path) -> None:
    class ProviderNeutralBinding:
        def bind(self, *, contract: AthenaContractReceipt) -> MnemosyneBindingReceipt:
            return MnemosyneBindingReceipt(
                root="/tmp/knowledge",
                repository="HomericIntelligence/Mnemosyne",
                default_branch="main",
                commit_sha="b" * 40,
                trust_basis="canonical upstream",
                athena_contract=contract.to_dict(),
            )

    host = MnemosyneSkillHost(
        binding_service=ProviderNeutralBinding(),
        corpus_reader=Corpus(),
    )
    with (
        patch(
            "hephaestus.agents.pi_plugins.preflight_pi_environment",
            side_effect=AssertionError("host advise must not preflight Pi"),
        ) as preflight,
        patch(
            "hephaestus.agents.runtime.run_agent_session",
            side_effect=AssertionError("host advise must not start an agent"),
        ) as start,
    ):
        result = host.execute(_request("advise", tmp_path))

    assert result.ok is True
    assert result.context == "selected guidance"
    assert result.receipt["contract"]["trust_source"] == ("hephaestus-athena-contract:v0.4.0")
    preflight.assert_not_called()
    start.assert_not_called()


def test_default_reader_selects_ranked_bound_skills_for_pipeline_advise_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordinary pipeline payloads retrieve skills without ``selected_skills``."""
    selected: dict[str, tuple[SkillSelection, ...]] = {}

    def git_output(_root: Path, argv: tuple[str, ...]) -> str:
        if argv[:4] == ("ls-tree", "-r", "--name-only", "b" * 40):
            return "skills/debugging.md\nskills/visual-design.md\nskills/debugging.notes.md\n"
        if argv[0] == "show" and argv[1].endswith(":skills/debugging.md"):
            return (
                "---\ndescription: Diagnose worker failures safely\ntags: [workers, failure]\n---\n"
                "# Failure mode\n\nPreserve diagnostics when a worker fails validation.\n"
            )
        if argv[0] == "show" and argv[1].endswith(":skills/visual-design.md"):
            return (
                "---\ndescription: Design visual mockups\n---\n# Outcome\nCreate illustrations.\n"
            )
        raise AssertionError(f"unexpected Git lookup: {argv}")

    def read_selected(**kwargs: object) -> MnemosyneCorpusResult:
        selections = kwargs["selections"]
        assert isinstance(selections, tuple)
        selected["values"] = selections
        return MnemosyneCorpusResult(
            context="## Selected Team Skills\n\nworker guidance",
            blocks=(),
            evidence={"selected_paths": [entry.source for entry in selections]},
        )

    monkeypatch.setattr(
        "hephaestus.automation.mnemosyne_skill_host.read_selected_skill_corpus",
        read_selected,
    )
    request = _request("advise", tmp_path)
    request.payload.update(
        {
            "issue_number": 2517,
            "issue_title": "Preserve worker failure diagnostics",
            "issue_body": "Validate the worker failure path without losing evidence.",
        }
    )
    host = MnemosyneSkillHost(
        contract_loader=_contract,
        binding_service=Binding(),
        corpus_reader=DefaultCorpusReader(git_output=git_output),
    )

    result = host.execute(request)

    assert result.ok is True
    assert [entry.source for entry in selected["values"]] == ["skills/debugging.md"]
    assert "failure" in selected["values"][0].reason
    assert result.receipt["corpus"]["selected_paths"] == ["skills/debugging.md"]


def test_default_reader_reads_committed_git_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Committed corpus reads return stdout from a successful Git command."""

    def run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="skills/debugging.md\n", stderr="")

    monkeypatch.setattr("hephaestus.automation.mnemosyne_skill_host.run_subprocess", run)

    output = DefaultCorpusReader._subprocess_git_output(
        tmp_path, ("ls-tree", "-r", "--name-only", "b" * 40)
    )

    assert output == "skills/debugging.md\n"


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (SimpleNamespace(returncode=1, stdout="", stderr="bad object"), "bad object"),
        (OSError("git unavailable"), "git unavailable"),
    ],
)
def test_default_reader_reports_committed_git_read_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: SimpleNamespace | OSError,
    message: str,
) -> None:
    """Committed corpus reads preserve bounded command and process failures."""

    def run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        if isinstance(result, OSError):
            raise result
        return result

    monkeypatch.setattr("hephaestus.automation.mnemosyne_skill_host.run_subprocess", run)

    with pytest.raises(MnemosyneCorpusError, match=message):
        DefaultCorpusReader._subprocess_git_output(
            tmp_path, ("show", f"{'b' * 40}:skills/debugging.md")
        )


def test_default_reader_accepts_valid_explicit_selection() -> None:
    """Closed callers may provide a fully typed explicit skill selection."""
    selections = DefaultCorpusReader()._explicit_selections(
        [{"name": "debugging", "source": "skills/debugging.md", "reason": "failure match"}]
    )

    assert selections == (SkillSelection("debugging", "skills/debugging.md", "failure match"),)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({}, "must be a list"),
        (["debugging"], "non-object selection"),
        ([{"name": "debugging", "source": "skills/debugging.md"}], "require string"),
    ],
)
def test_default_reader_rejects_malformed_explicit_selections(raw: object, message: str) -> None:
    """Explicit selections fail closed when their shape or fields are malformed."""
    with pytest.raises(MnemosyneCorpusError, match=message):
        DefaultCorpusReader()._explicit_selections(raw)


def test_learn_without_closed_delivery_payload_fails_closed(tmp_path: Path) -> None:
    host = MnemosyneSkillHost(contract_loader=_contract, binding_service=Binding())

    result = host.execute(_request("learn", tmp_path))

    assert result.ok is False
    assert result.error == "learn delivery payload is required"


def test_learn_rejects_ambiguous_intent_and_delivery_payload(tmp_path: Path) -> None:
    """The host accepts exactly one preparation input shape."""
    host = MnemosyneSkillHost(contract_loader=_contract, binding_service=Binding())
    request = _request("learn", tmp_path)
    request.payload.update({"learning_intent": {}, "learn_delivery": {}})

    result = host.execute(request)

    assert result.ok is False
    assert result.error == (
        "learn request must contain exactly one of learning_intent or learn_delivery"
    )


def test_learn_rejects_incomplete_explicit_delivery_with_exact_diagnostic(
    tmp_path: Path,
) -> None:
    """Legacy closed callers retain field-specific malformed-input errors."""
    host = MnemosyneSkillHost(contract_loader=_contract, binding_service=Binding())
    request = _request("learn", tmp_path)
    request.payload["learn_delivery"] = {
        "repository": "HomericIntelligence/Mnemosyne",
    }

    result = host.execute(request)

    assert result.error == "learn delivery payload lacks non-empty worktree_path"


def test_default_host_constructs_and_uses_concrete_delivery_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default host wiring delivers through the host-owned concrete backend."""

    class Service:
        def __init__(self, *, github: object) -> None:
            assert github is not None
            self.requests: list[object] = []

        def deliver(self, request: object) -> LearnDeliveryReceipt:
            self.requests.append(request)
            return LearnDeliveryReceipt(
                repository="HomericIntelligence/Mnemosyne",
                branch="skill/example",
                base_branch="main",
                commit_sha="c" * 40,
                pr_url="https://github.com/HomericIntelligence/Mnemosyne/pull/8",
                pr_number=8,
                readback_head_sha="c" * 40,
                validation_evidence=("pytest",),
                final_disposition="create",
            )

    monkeypatch.setattr("hephaestus.automation.mnemosyne_skill_host.LearnDeliveryService", Service)
    host = MnemosyneSkillHost(contract_loader=_contract, binding_service=Binding())
    request = _request("learn", tmp_path)
    request.payload.update(
        {
            "learn_delivery": {
                "repository": "HomericIntelligence/Mnemosyne",
                "worktree_path": str(tmp_path),
                "branch": "skill/example",
                "base_branch": "main",
                "allowed_paths": ["skills/example.md"],
                "commit_message": "docs(skills): capture reusable workflow",
                "pr_title": "docs(skills): capture reusable workflow",
                "pr_body": "Summary.\n",
                "disposition": "create",
                "validation_evidence": ["pytest"],
            }
        }
    )

    result = host.execute(request)

    assert result.ok is True
    assert result.delivery_receipt is not None
    assert result.delivery_receipt["pr_number"] == 8


def test_github_delivery_adapter_creates_pr_and_returns_server_number() -> None:
    """PR creation returns the number parsed from the server-issued URL."""
    calls: list[list[str]] = []

    def gh(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout="https://github.com/acme/Mnemosyne/pull/8\n",
        )

    number = GitHubLearnDeliveryAdapter(gh=gh).create_pr(
        repository="acme/Mnemosyne",
        head="skill/example",
        base="main",
        title="docs(skills): capture reusable workflow",
        body="Summary.\n",
    )

    assert number == 8
    assert calls == [
        [
            "pr",
            "create",
            "--repo",
            "acme/Mnemosyne",
            "--head",
            "skill/example",
            "--base",
            "main",
            "--title",
            "docs(skills): capture reusable workflow",
            "--body",
            "Summary.\n",
        ]
    ]


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "message"),
    [
        (1, "", "permission denied", "PR creation failed: permission denied"),
        (0, "created without a URL", "", "PR creation returned no PR URL"),
    ],
)
def test_github_delivery_adapter_rejects_failed_or_unbound_pr_creation(
    returncode: int,
    stdout: str,
    stderr: str,
    message: str,
) -> None:
    """PR creation fails closed on command failure or absent server identity."""

    def gh(_argv: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    with pytest.raises(LearnDeliveryError, match=message):
        GitHubLearnDeliveryAdapter(gh=gh).create_pr(
            repository="acme/Mnemosyne",
            head="skill/example",
            base="main",
            title="title",
            body="body",
        )


def test_github_delivery_adapter_reads_back_pr_head() -> None:
    """PR readback returns the server URL and immutable head SHA."""

    def gh(_argv: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "url": "https://github.com/acme/Mnemosyne/pull/8",
                    "headRefOid": "a" * 40,
                }
            ),
        )

    url, head_sha = GitHubLearnDeliveryAdapter(gh=gh).read_pr_head(
        repository="acme/Mnemosyne", number=8
    )

    assert url == "https://github.com/acme/Mnemosyne/pull/8"
    assert head_sha == "a" * 40


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "message"),
    [
        (1, "", "not found", "PR readback failed: not found"),
        (0, "not-json", "", "PR readback returned malformed JSON"),
        (0, "[]", "", "PR readback returned non-object JSON"),
        (0, '{"url": "https://example.test/8"}', "", "PR readback lacks URL or head SHA"),
    ],
)
def test_github_delivery_adapter_rejects_invalid_pr_readback(
    returncode: int,
    stdout: str,
    stderr: str,
    message: str,
) -> None:
    """PR readback rejects command, JSON, shape, and identity failures."""

    def gh(_argv: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    with pytest.raises(LearnDeliveryError, match=message):
        GitHubLearnDeliveryAdapter(gh=gh).read_pr_head(repository="acme/Mnemosyne", number=8)


def test_github_delivery_adapter_binds_existing_pr_source_fields() -> None:
    """Existing-PR delivery uses server-read source identity before mutation."""
    calls: list[list[str]] = []

    def gh(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "url": "https://github.com/acme/Mnemosyne/pull/8",
                    "state": "OPEN",
                    "baseRefName": "main",
                    "headRefName": "skill/example",
                    "headRefOid": "a" * 40,
                    "headRepository": {"nameWithOwner": "acme/Mnemosyne"},
                }
            ),
        )

    binding = GitHubLearnDeliveryAdapter(gh=gh).read_existing_pr(
        repository="acme/Mnemosyne", number=8
    )

    assert binding.source_repository == "acme/Mnemosyne"
    assert binding.source_ref == "skill/example"
    assert binding.head_sha == "a" * 40
    assert calls == [
        [
            "pr",
            "view",
            "8",
            "--repo",
            "acme/Mnemosyne",
            "--json",
            "url,state,baseRefName,headRefName,headRefOid,headRepository",
        ]
    ]


def test_github_delivery_adapter_rejects_malformed_existing_pr_source() -> None:
    """A missing source repository cannot become an existing-PR delivery target."""

    def gh(_argv: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "url": "https://github.com/acme/Mnemosyne/pull/8",
                    "state": "OPEN",
                    "baseRefName": "main",
                    "headRefName": "skill/example",
                    "headRefOid": "a" * 40,
                    "headRepository": None,
                }
            ),
        )

    with pytest.raises(LearnDeliveryError, match="required source binding fields"):
        GitHubLearnDeliveryAdapter(gh=gh).read_existing_pr(repository="acme/Mnemosyne", number=8)


def test_learn_requires_pr_backed_delivery_receipt(tmp_path: Path) -> None:
    class Delivery:
        def deliver_from_request(
            self,
            request: AthenaSkillRequest,
            binding: MnemosyneBindingReceipt,
        ) -> LearnDeliveryReceipt:
            del request
            assert binding == _binding()
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
