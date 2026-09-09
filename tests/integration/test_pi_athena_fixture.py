"""Deterministic fixture coverage for Pi Athena/Mnemosyne host semantics."""
# ruff: noqa: D101, D103

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from hephaestus.automation.athena_contract import AthenaContractReceipt
from hephaestus.automation.mnemosyne_binding import MnemosyneBindingError, MnemosyneBindingReceipt
from hephaestus.automation.mnemosyne_corpus import MnemosyneCorpusResult
from hephaestus.automation.mnemosyne_delivery import (
    ExistingPullRequest,
    LearnDeliveryReceipt,
    LearnDeliveryService,
)
from hephaestus.automation.mnemosyne_learning_preparation import (
    MnemosyneLearningBuilder,
    MnemosyneLearningPreparationService,
    PostMergeLearningSource,
    PreparedLearningChange,
    PreparedLearningWorkspace,
)
from hephaestus.automation.mnemosyne_skill_host import (
    DefaultLearnDeliveryBackend,
    MnemosyneSkillHost,
)
from hephaestus.automation.pipeline.athena_skill_jobs import AthenaSkillRequest
from hephaestus.automation.pipeline.work_item import LearningIntent


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
        version="3.0.0",
        commit_sha="b" * 40,
        sync_status="updated",
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

    def deliver_from_request(
        self,
        request: AthenaSkillRequest,
        binding: MnemosyneBindingReceipt,
    ) -> LearnDeliveryReceipt:
        del request, binding
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


class FixtureLearningBuilder(MnemosyneLearningBuilder):
    def build(self, requested: LearningIntent, source: object) -> PreparedLearningChange:
        return PreparedLearningChange(
            PurePosixPath("skills/host-learning.md"),
            "---\nname: host-learning\n---\n# Host learning\n",
            "Host learning",
        )


def test_successful_pr_backed_learn_fixture(tmp_path: Path) -> None:
    """Production preparation and delivery succeed without any agent harness."""
    intent = LearningIntent.post_merge(repo="HomericIntelligence/Hephaestus", issue=9, pr=10)
    binding = MnemosyneBindingReceipt(
        root=str(tmp_path / "knowledge"),
        repository="HomericIntelligence/Mnemosyne",
        default_branch="main",
        version="3.0.0",
        commit_sha="b" * 40,
        sync_status="updated",
        trust_basis="canonical upstream",
        athena_contract=_contract().to_dict(),
    )
    worktree = Path(binding.root) / "build" / "mnemosyne-learning" / "fixture"

    class LearningBinding:
        def bind(self, *, contract: AthenaContractReceipt) -> MnemosyneBindingReceipt:
            assert contract == _contract()
            return binding

    class SourceReader:
        def read(self, requested: LearningIntent) -> PostMergeLearningSource:
            return PostMergeLearningSource(
                repository=requested.repo,
                issue=requested.issue,
                pr=10,
                title="Host learning boundary",
                body="Closes #9",
                merged_at="2026-08-14T00:00:00Z",
                merge_commit_sha="c" * 40,
                url="https://github.com/org/repo/pull/10",
                verified_head="d" * 40,
                verification_evidence=("tests: fixture",),
            )

    class Workspace:
        def prepare(
            self,
            binding: MnemosyneBindingReceipt,
            branch: str,
        ) -> PreparedLearningWorkspace:
            assert binding == binding_receipt
            assert branch.startswith("learn/")
            worktree.mkdir(parents=True)
            return PreparedLearningWorkspace(worktree, None)

    class Validator:
        def validate(self, path: Path) -> tuple[str, ...]:
            assert path == worktree
            return ("fixture validator",)

    class Git:
        def __call__(
            self,
            cwd: Path,
            argv: tuple[str, ...],
            timeout_s: int,
        ) -> subprocess.CompletedProcess[str]:
            del timeout_s
            stdout = ""
            if argv == ("remote", "get-url", "origin"):
                stdout = "git@github.com:HomericIntelligence/Mnemosyne.git\n"
            elif argv == ("ls-files", "--others", "--exclude-standard"):
                stdout = "\n".join(
                    path.relative_to(cwd).as_posix() for path in (cwd / "skills").glob("*.md")
                )
            elif argv == ("rev-parse", "HEAD"):
                stdout = "c" * 40 + "\n"
            return subprocess.CompletedProcess(["git", *argv], 0, stdout=stdout, stderr="")

    class GitHub:
        def create_pr(self, **_kwargs: object) -> int:
            return 2

        def read_pr_head(self, **_kwargs: object) -> tuple[str, str]:
            return "https://github.com/HomericIntelligence/Mnemosyne/pull/2", "c" * 40

        def read_existing_pr(
            self,
            *,
            repository: str,
            number: int,
        ) -> ExistingPullRequest:
            del repository, number
            raise AssertionError("new delivery must not bind an existing PR")

    binding_receipt = binding
    preparation = MnemosyneLearningPreparationService(
        source_reader=SourceReader(),
        builder=FixtureLearningBuilder(),
        workspace=Workspace(),
        validator=Validator(),
    )
    delivery = LearnDeliveryService(git=Git(), github=GitHub())
    host = MnemosyneSkillHost(
        contract_loader=_contract,
        binding_service=LearningBinding(),
        delivery_service=DefaultLearnDeliveryBackend(
            service=delivery,
            preparation=preparation,
        ),
    )
    request = _request("learn", tmp_path)
    request.payload["learning_intent"] = intent.to_payload()

    with (
        patch(
            "hephaestus.automation.mnemosyne_skill_host.DefaultCorpusReader.read",
            return_value=MnemosyneCorpusResult(context="", blocks=(), evidence={}),
        ),
        patch(
            "hephaestus.agents.pi_plugins.preflight_pi_environment",
            side_effect=AssertionError("learning must not preflight Pi"),
        ) as pi_preflight,
        patch(
            "hephaestus.agents.runtime.run_agent_text",
            side_effect=AssertionError("learning must not run agent text"),
        ) as agent_text,
        patch(
            "hephaestus.agents.runtime.run_agent_session",
            side_effect=AssertionError("learning must not run an agent session"),
        ) as agent_session,
        patch(
            "hephaestus.agents.runtime.run_claude_text",
            side_effect=AssertionError("learning must not run Claude text"),
        ) as claude_text,
        patch(
            "hephaestus.agents.runtime.run_codex_text",
            side_effect=AssertionError("learning must not run Codex text"),
        ) as codex_text,
        patch(
            "hephaestus.agents.runtime.run_codex_session",
            side_effect=AssertionError("learning must not run a Codex session"),
        ) as codex_session,
        patch(
            "hephaestus.agents.runtime.run_pi_text",
            side_effect=AssertionError("learning must not run Pi text"),
        ) as pi_text,
        patch(
            "hephaestus.agents.runtime.run_pi_session",
            side_effect=AssertionError("learning must not run a Pi session"),
        ) as pi_session,
        patch(
            "hephaestus.agents.runtime.run_pi_smoke_session",
            side_effect=AssertionError("learning must not run Pi smoke"),
        ) as pi_smoke,
        patch(
            "hephaestus.automation.claude_invoke.invoke_claude_with_session",
            side_effect=AssertionError("learning must not invoke Claude"),
        ) as claude,
        patch(
            "hephaestus.automation.pipeline.worker_pool.WorkerPool._run_agent",
            side_effect=AssertionError("learning must not dispatch AgentJob"),
        ) as agent_job,
    ):
        result = host.execute(request)

    assert result.ok
    assert result.delivery_receipt is not None
    assert result.delivery_receipt["pr_url"].endswith("/2")
    pi_preflight.assert_not_called()
    agent_text.assert_not_called()
    agent_session.assert_not_called()
    claude_text.assert_not_called()
    codex_text.assert_not_called()
    codex_session.assert_not_called()
    pi_text.assert_not_called()
    pi_session.assert_not_called()
    pi_smoke.assert_not_called()
    claude.assert_not_called()
    agent_job.assert_not_called()


def test_missing_delivery_request_and_trust_failure_fail_closed(tmp_path: Path) -> None:
    missing_delivery = MnemosyneSkillHost(contract_loader=_contract, binding_service=Binding())
    trust_failure = MnemosyneSkillHost(
        contract_loader=_contract, binding_service=Binding(fail=True)
    )

    assert (
        missing_delivery.execute(_request("learn", tmp_path)).error
        == "learn delivery payload is required"
    )
    assert (
        trust_failure.execute(_request("advise", tmp_path)).error
        == "mnemosyne_binding: trust failure"
    )


def test_interrupted_or_drifted_recovery_path_fails_closed(tmp_path: Path) -> None:
    host = MnemosyneSkillHost(
        contract_loader=_contract,
        binding_service=Binding(),
        delivery_service=Delivery(drift=True),
    )

    result = host.execute(_request("learn", tmp_path))

    assert result.error == "learn delivery receipt invalid"
