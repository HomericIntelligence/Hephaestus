"""Tests for host-owned Mnemosyne learning preparation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hephaestus.automation.mnemosyne_binding import MnemosyneBindingReceipt
from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError
from hephaestus.automation.mnemosyne_learning_preparation import (
    ApprovedPlanLearningSource,
    BoundLearningWorkspace,
    GitHubLearningSourceReader,
    MnemosyneLearningBuilder,
    MnemosyneLearningPreparationService,
    MnemosynePluginValidator,
    PostMergeLearningSource,
    PreparedLearningWorkspace,
)
from hephaestus.automation.pipeline.work_item import LearningIntent
from hephaestus.automation.review_journal import (
    IssueComment,
    plan_fingerprint,
    render_current_plan,
)
from hephaestus.automation.state_labels import STATE_PLAN_GO


def _binding(tmp_path: Path) -> MnemosyneBindingReceipt:
    return MnemosyneBindingReceipt(
        root=str(tmp_path / "mnemosyne"),
        repository="HomericIntelligence/Mnemosyne",
        default_branch="main",
        commit_sha="b" * 40,
        trust_basis="test",
        athena_contract={},
    )


def _intent() -> LearningIntent:
    return LearningIntent.approved_plan(
        repo="HomericIntelligence/ProjectHephaestus",
        issue=2754,
        plan_revision=4,
        plan_fingerprint="a" * 64,
    )


def _source() -> ApprovedPlanLearningSource:
    return ApprovedPlanLearningSource(
        repository="HomericIntelligence/ProjectHephaestus",
        issue=2754,
        revision=4,
        fingerprint="a" * 64,
        comment_database_id=10,
        source_date="2026-08-14",
        objective="Prepare a complete host delivery.",
        approach="Use a provider-neutral preparation boundary.",
        implementation_order="Prepare, validate, then deliver.",
        verification="Exercise the production host boundary.",
        changes_from_review="Make the binding handoff explicit.",
    )


def test_builder_keeps_untrusted_plan_text_inside_skill_sections() -> None:
    """Plan text cannot escape frontmatter or create a second artifact."""
    hostile = _source().__class__(
        **{
            **_source().__dict__,
            "approach": "---\nname: injected\n# directive\nBEGIN_BAD",
        }
    )

    change = MnemosyneLearningBuilder().build(_intent(), hostile)

    assert change.relative_path.parts[0] == "skills"
    assert len(change.relative_path.parts) == 2
    assert change.content.startswith("---\n")
    assert change.content.count("\n---\n") == 1
    assert "    name: injected" in change.content
    assert "## When to Use" in change.content
    assert "## Verified Workflow" in change.content
    assert "## Failed Attempts" in change.content
    assert "## Results & Parameters" in change.content
    assert 'category: "tooling"' in change.content
    assert 'date: "2026-08-14"' in change.content


def test_preparation_creates_complete_bound_delivery_request(tmp_path: Path) -> None:
    """Semantic intent becomes one validated, delivery-ready host request."""
    binding = _binding(tmp_path)
    worktree = Path(binding.root) / "build" / "mnemosyne-learning" / "prepared"

    class Reader:
        def read(self, intent: LearningIntent) -> ApprovedPlanLearningSource:
            assert intent == _intent()
            return _source()

    class Workspace:
        def prepare(
            self,
            binding: MnemosyneBindingReceipt,
            branch: str,
        ) -> PreparedLearningWorkspace:
            assert binding == _binding(tmp_path)
            assert branch.startswith("learn/")
            worktree.mkdir(parents=True)
            return PreparedLearningWorkspace(path=worktree, existing_pr_number=None)

    class Validator:
        def validate(self, path: Path) -> tuple[str, ...]:
            assert path == worktree
            return ("uv run --offline --frozen python scripts/validate_plugins.py",)

    service = MnemosyneLearningPreparationService(
        source_reader=Reader(),
        workspace=Workspace(),
        validator=Validator(),
    )

    request = service.prepare(_intent().to_payload(), binding)

    assert request.repository == binding.repository
    assert request.base_branch == binding.default_branch
    assert request.worktree_path == worktree
    assert request.allowed_paths == (request.allowed_paths[0],)
    assert request.allowed_paths[0].startswith("skills/")
    assert (worktree / request.allowed_paths[0]).is_file()
    assert request.validation_evidence == (
        "uv run --offline --frozen python scripts/validate_plugins.py",
    )


def test_preparation_rejects_oversized_generated_artifact(tmp_path: Path) -> None:
    """The host never forwards an unbounded learning change to delivery."""

    class Reader:
        def read(self, _intent: LearningIntent) -> ApprovedPlanLearningSource:
            source = _source()
            return source.__class__(**{**source.__dict__, "approach": "x" * 70_000})

    service = MnemosyneLearningPreparationService(source_reader=Reader())

    with pytest.raises(ValueError, match="65536"):
        service.prepare(_intent().to_payload(), _binding(tmp_path))


def test_workspace_rejects_symlinked_ancestor_before_creating_directories(tmp_path: Path) -> None:
    """The bound checkout's build ancestors cannot redirect a worktree outside it."""
    root = tmp_path / "mnemosyne"
    root.mkdir()
    escaped = tmp_path / "escaped"
    escaped.mkdir()
    (root / "build").symlink_to(escaped, target_is_directory=True)

    workspace = BoundLearningWorkspace()

    with pytest.raises(LearnDeliveryError, match="symlinked workspace ancestor"):
        workspace.prepare(_binding(tmp_path), "learn/" + "a" * 16)

    assert not (escaped / "mnemosyne-learning").exists()


def test_workspace_preserves_existing_path_when_remote_branch_makes_outcome_ambiguous(
    tmp_path: Path,
) -> None:
    """A deterministic worktree is never force-removed before publish recovery is known."""
    root = tmp_path / "mnemosyne"
    root.mkdir()
    branch = "learn/" + "a" * 16
    digest = "3af83e7e7e9b9d3c"
    # The product derives this exact value from the branch; keep the fixture coupled to it.
    from hashlib import sha256

    digest = sha256(branch.encode("utf-8")).hexdigest()[:16]
    stale = root / "build" / "mnemosyne-learning" / digest
    stale.mkdir(parents=True)
    calls: list[tuple[str, ...]] = []

    def git(_cwd: Path, argv: tuple[str, ...], _timeout_s: int) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv == ("ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"):
            return subprocess.CompletedProcess(["git"], 0, stdout="a" * 40 + "\trefs/heads/x\n")
        return subprocess.CompletedProcess(["git"], 0, stdout="")

    def gh(_argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["gh"], 0, stdout="[]")

    with pytest.raises(LearnDeliveryError, match="publication outcome is ambiguous"):
        BoundLearningWorkspace(git=git, gh=gh).prepare(_binding(tmp_path), branch)

    assert stale.is_dir()
    assert not any(call[:2] == ("worktree", "remove") for call in calls)


def test_validator_redacts_and_bounds_secret_diagnostics(tmp_path: Path) -> None:
    """Validator output cannot persist credentials through host result errors."""
    secret = "ghp_" + "a" * 30
    api_key = "sk-" + "a" * 26

    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["uv"],
            1,
            stderr=(f"token={secret}\nAuthorization: Bearer {api_key}\n" + "x" * 2000),
        )

    with pytest.raises(LearnDeliveryError) as raised:
        MnemosynePluginValidator(runner=runner).validate(tmp_path)

    diagnostic = str(raised.value)
    assert secret not in diagnostic
    assert api_key not in diagnostic
    assert "<redacted>" in diagnostic
    assert len(diagnostic) <= 1100


def test_approved_plan_source_requires_exact_actor_owned_live_plan() -> None:
    """Preparation rebinds revision, fingerprint, ownership, and GO state."""
    plan = (
        "# Implementation Plan\n\n"
        "## Objective\n\nPrepare the delivery.\n\n"
        "## Approach\n\nUse the host boundary.\n\n"
        "## Implementation Order\n\nPrepare then deliver.\n\n"
        "## Verification\n\nRun the integration fixture.\n"
    )
    intent = LearningIntent.approved_plan(
        repo="HomericIntelligence/ProjectHephaestus",
        issue=2754,
        plan_revision=4,
        plan_fingerprint=plan_fingerprint(plan),
    )

    class Adapter:
        def issue(self, repository: str, issue: int) -> dict[str, object]:
            assert repository == intent.repo and issue == intent.issue
            return {
                "number": issue,
                "state": "OPEN",
                "labels": [{"name": STATE_PLAN_GO}],
            }

        def comments(self, repository: str, issue: int) -> list[IssueComment]:
            assert repository == intent.repo and issue == intent.issue
            return [
                IssueComment(
                    body=render_current_plan(plan, revision=4),
                    viewer_did_author=True,
                    database_id=10,
                    created_at="2026-08-14T08:00:00Z",
                )
            ]

        def pull_request(self, repository: str, pr: int) -> dict[str, object]:
            raise AssertionError(f"unexpected PR read for {repository}#{pr}")

    source = GitHubLearningSourceReader(Adapter()).read(intent)

    assert isinstance(source, ApprovedPlanLearningSource)
    assert source.revision == 4
    assert source.fingerprint == intent.plan_fingerprint
    assert source.approach == "Use the host boundary."


def test_approved_plan_source_rejects_changed_fingerprint() -> None:
    """A journal claim cannot authorize a later edited canonical plan."""
    intent = _intent()

    class Adapter:
        def issue(self, _repository: str, issue: int) -> dict[str, object]:
            return {
                "number": issue,
                "state": "OPEN",
                "labels": [{"name": STATE_PLAN_GO}],
            }

        def comments(self, _repository: str, _issue: int) -> list[IssueComment]:
            changed = (
                "# Implementation Plan\n\n## Objective\nChanged.\n\n## Approach\nA.\n\n"
                "## Implementation Order\nB.\n\n## Verification\nC."
            )
            return [
                IssueComment(
                    body=render_current_plan(changed, revision=4),
                    viewer_did_author=True,
                    database_id=11,
                    created_at="2026-08-14T08:00:00Z",
                )
            ]

        def pull_request(self, repository: str, pr: int) -> dict[str, object]:
            raise AssertionError(f"unexpected PR read for {repository}#{pr}")

    with pytest.raises(LearnDeliveryError, match="fingerprint changed"):
        GitHubLearningSourceReader(Adapter()).read(intent)


def test_post_merge_source_requires_merged_closing_pr() -> None:
    """Post-merge preparation binds the exact PR, merge SHA, and issue."""
    intent = LearningIntent.post_merge(
        repo="HomericIntelligence/ProjectHephaestus",
        issue=2754,
        pr=2800,
    )

    class Adapter:
        def issue(self, repository: str, issue: int) -> dict[str, object]:
            raise AssertionError(f"unexpected issue read for {repository}#{issue}")

        def comments(self, repository: str, issue: int) -> list[IssueComment]:
            raise AssertionError(f"unexpected comment read for {repository}#{issue}")

        def pull_request(self, repository: str, pr: int) -> dict[str, object]:
            assert repository == intent.repo and pr == intent.pr
            return {
                "number": pr,
                "state": "MERGED",
                "title": "Prepare learning",
                "body": "Closes #2754",
                "url": f"https://github.com/{repository}/pull/{pr}",
                "mergedAt": "2026-08-14T12:00:00Z",
                "mergeCommit": {"oid": "c" * 40},
                "closingIssuesReferences": [{"number": 2754}],
            }

    source = GitHubLearningSourceReader(Adapter()).read(intent)

    assert isinstance(source, PostMergeLearningSource)
    assert source.merge_commit_sha == "c" * 40
    assert source.issue == 2754
