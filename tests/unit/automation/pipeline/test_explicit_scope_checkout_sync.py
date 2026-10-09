"""Regression coverage for explicit CLI scope checkout synchronization."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.issue_waves import IssueWaveStore
from hephaestus.automation.pipeline import seeding as seeding_mod
from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.jobs import AgentJob, GitJob, JobResult
from hephaestus.automation.pipeline.routing import (
    Disposition,
    PipelineScope,
    StageName,
    StageOutcome,
)
from hephaestus.automation.pipeline.seeding import IssueFacts
from hephaestus.automation.pipeline.stages.base import Stage
from hephaestus.automation.pipeline.stages.repo import RepoIssueSource
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.repo_intake import RepoIntakeManager, RepoIntakeReceipt
from tests.unit.automation.pipeline.conftest import FakeWorkerPool, fake_worker_factories
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _run_fixture_git(cwd: Path, *arguments: str) -> str:
    """Run one local Git fixture command and return its output."""
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fixture_git_runner(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    log_errors: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run Git for a local intake fixture."""
    del log_errors
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _initialize_recording_checkout(path: Path) -> None:
    """Create the minimum real Git checkout for sequencing-only tests."""
    path.mkdir()
    _run_fixture_git(path, "init", "--initial-branch=main")
    _run_fixture_git(path, "config", "user.name", "Test User")
    _run_fixture_git(path, "config", "user.email", "test@example.invalid")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    _run_fixture_git(path, "add", "tracked.txt")
    _run_fixture_git(path, "commit", "-m", "base")


@contextmanager
def _accept_recording_intake(
    _manager: RepoIntakeManager,
    _receipt: RepoIntakeReceipt,
) -> Iterator[None]:
    """Accept the synthetic receipt used by sequencing-only tests."""
    yield


def test_adopt_repo_intake_rejects_an_unregistered_replacement(
    tmp_path: Path,
) -> None:
    """Adoption rejects a valid receipt path that Git does not own."""
    caller = tmp_path / "repo-a"
    caller.mkdir()
    _run_fixture_git(caller, "init", "--initial-branch=main")
    _run_fixture_git(caller, "config", "user.name", "Test User")
    _run_fixture_git(caller, "config", "user.email", "test@example.invalid")
    (caller / "tracked.txt").write_text("base\n", encoding="utf-8")
    _run_fixture_git(caller, "add", "tracked.txt")
    _run_fixture_git(caller, "commit", "-m", "base")
    _run_fixture_git(
        caller,
        "remote",
        "add",
        "origin",
        "https://github.com/org/repo-a.git",
    )
    revision = _run_fixture_git(caller, "rev-parse", "HEAD")
    manager = RepoIntakeManager(
        caller,
        repository="org/repo-a",
        gh_command="gh",
        timeout_s=30,
        git_runner=_fixture_git_runner,
        git_env={},
        remote_config=(),
    )
    manager.state_dir.mkdir(mode=0o700, parents=True)
    manager.worktree_path.mkdir()
    _run_fixture_git(manager.worktree_path, "init", "--initial-branch=main")
    receipt = RepoIntakeReceipt(
        repository="org/repo-a",
        repository_identity=manager.repository_identity,
        ownership_key=manager.ownership_key,
        common_dir=manager.common_dir,
        path=manager.worktree_path.resolve(),
        default_branch="main",
        revision=revision,
        generation=1,
    )
    manager.receipt_path.write_text(
        json.dumps(receipt.to_dict(), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manager.receipt_path.chmod(0o600)
    config = PipelineConfig(
        org="org",
        repos=["repo-a"],
        projects_dir=tmp_path,
        repo_roots={"repo-a": caller},
    )
    coordinator = Coordinator(
        config,
        github=FakeStageGitHub(),
        **fake_worker_factories(),
        install_signals=False,
    )
    cached = object()
    coordinator._ctx_cache["repo-a"] = cached  # type: ignore[assignment]

    error = coordinator._adopt_repo_intake(
        WorkItem(repo="repo-a", kind=ItemKind.REPO),
        JobResult(ok=True, value=receipt.to_dict()),
    )

    assert error is not None
    assert config.repo_roots["repo-a"] == caller
    assert "repo-a" not in config.repo_state_roots
    assert coordinator._ctx_cache["repo-a"] is cached


class _ImmediatePassStage(Stage):
    """Finish a seeded issue without an agent job."""

    def on_enter(self, item: WorkItem, ctx: Any) -> None:
        del item, ctx

    def step(self, item: WorkItem, ctx: Any) -> StageOutcome:
        del item, ctx
        return StageOutcome(Disposition.FINISH_PASS, "complete")

    def on_job_done(self, item: WorkItem, result: Any, ctx: Any) -> None:
        del item, result, ctx
        raise AssertionError("the deterministic stage does not submit jobs")


class _RecordingPool(FakeWorkerPool):
    """Record the synchronization submission in the shared event trace."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def submit(self, job: Any, on_done_state: Any, **kwargs: Any) -> Any:
        if isinstance(job, GitJob) and job.op == "clone":
            self._events.append("clone")
        if isinstance(job, GitJob) and job.op == "prepare_intake":
            self._events.append("intake")
        if isinstance(job, GitJob) and job.op == "sync_checkout":
            self._events.append("sync")
        return super().submit(job, on_done_state, **kwargs)


class _RecordingGitHub(FakeStageGitHub):
    """Record the first label mutation relative to direct classification."""

    def __init__(self, events: list[str], **kwargs: Any) -> None:
        super().__init__(labels=["state:needs-plan"], **kwargs)
        self._events = events

    def ensure_state_labels(self) -> None:
        self._events.append("labels")
        super().ensure_state_labels()

    def find_issue_for_pr(self, pr_number: int) -> int | None:
        self._events.append("classify-pr")
        return super().find_issue_for_pr(pr_number)


def _facts(issue: int) -> IssueFacts:
    return IssueFacts(
        number=issue,
        title=f"Issue {issue}",
        body="",
        is_epic=False,
        labels={"state:needs-plan"},
        pr_number=None,
        pr_is_open=False,
        pr_is_merged=False,
    )


def test_explicit_scope_syncs_before_labels_and_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scoped run gates direct source admission on a clean-main sync job."""
    events: list[str] = []
    checkout = tmp_path / "repo-a"
    _initialize_recording_checkout(checkout)
    pool = _RecordingPool(events)
    github = _RecordingGitHub(events)

    def classify(issue: int, github_arg: Any) -> IssueFacts:
        del github_arg
        events.append("classify")
        return _facts(issue)

    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", classify)
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.admission._filter_open_issues",
        lambda _repo, issues, **_kwargs: list(issues),
    )
    monkeypatch.setattr(RepoIntakeManager, "adoption_guard", _accept_recording_intake)
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            issues=[101],
            projects_dir=tmp_path,
            scope=PipelineScope(frozenset({StageName.PLANNING, StageName.PLAN_REVIEW})),
            rate_guard_enabled=False,
        ),
        github=github,
        **fake_worker_factories(pool, None),
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _ImmediatePassStage()

    assert coordinator.run() == 0
    assert events[:3] == ["intake", "labels", "classify"]
    issue_item = next(item for item in coordinator.items if item.issue == 101)
    assert issue_item.payload["_direct_scope_base_sha"] == "a" * 40
    assert coordinator.config.repo_roots["repo-a"] == tmp_path / ".repo-a-intake"


def test_direct_scope_uses_isolated_intake_when_primary_has_tracked_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct scope prepares intake without synchronizing the caller checkout."""
    checkout = tmp_path / "repo-a"
    checkout.mkdir()
    (checkout / "tracked.txt").write_text("local work\n", encoding="utf-8")
    events: list[str] = []
    pool = _RecordingPool(events)
    github = _RecordingGitHub(events)

    monkeypatch.setattr(
        "hephaestus.automation.pipeline.admission._filter_open_issues",
        lambda _repo, issues: list(issues),
    )
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            issues=[101],
            projects_dir=tmp_path,
            scope=PipelineScope(frozenset({StageName.PLANNING})),
        ),
        github=github,
        **fake_worker_factories(pool, None),
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _ImmediatePassStage()

    coordinator.run()

    first_git_job = next(handle.job for handle in pool.submitted if isinstance(handle.job, GitJob))
    assert first_git_job.op == "prepare_intake"


def test_missing_direct_scope_checkout_clones_then_syncs_before_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing direct-scope checkout is synchronized before it is admitted."""
    events: list[str] = []
    pool = _RecordingPool(events)
    github = _RecordingGitHub(events)

    def classify(issue: int, github_arg: Any) -> IssueFacts:
        del github_arg
        events.append("classify")
        return _facts(issue)

    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", classify)
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.admission._filter_open_issues",
        lambda _repo, issues, **_kwargs: list(issues),
    )
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            issues=[101],
            projects_dir=tmp_path,
            scope=PipelineScope(frozenset({StageName.PLANNING, StageName.PLAN_REVIEW})),
            rate_guard_enabled=False,
        ),
        github=github,
        **fake_worker_factories(pool, None),
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _ImmediatePassStage()

    assert coordinator.run() == 0
    assert events[:4] == ["clone", "sync", "labels", "classify"]
    issue_item = next(item for item in coordinator.items if item.issue == 101)
    assert issue_item.payload["_direct_scope_base_sha"] == "a" * 40


def test_direct_issue_rejects_scalar_intake_success_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare SHA cannot replace the typed intake receipt in a light fixture."""
    checkout = tmp_path / "repo-a"
    checkout.mkdir()
    pin = "a" * 40
    pool = FakeWorkerPool()
    pool.script(JobResult(ok=True, value=pin))
    github = FakeStageGitHub(labels=["state:needs-plan"])

    monkeypatch.setattr(
        "hephaestus.automation.pipeline.admission._filter_open_issues",
        lambda _repo, issues, **_kwargs: list(issues),
    )
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            issues=[101],
            projects_dir=tmp_path,
            scope=PipelineScope(frozenset({StageName.PLANNING})),
            rate_guard_enabled=False,
        ),
        github=github,
        **fake_worker_factories(pool, None),
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _ImmediatePassStage()

    assert coordinator.run() == 1
    assert github.mutation_log == []
    assert len(coordinator.ledger) == 1
    assert "repository-intake receipt invalid" in coordinator.ledger[0].reason


def test_issue_wave_classification_reads_receipt_owned_state_after_intake_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue-wave recovery reads durable state outside the replaceable intake."""
    durable_root = tmp_path / "intake-owner"
    intake_root = durable_root / "checkout"
    durable_root.mkdir()
    intake_root.mkdir()
    store = IssueWaveStore(durable_root, "acme", "hephaestus")
    lease = store.seal_selection(store.plan_admission("a" * 40, 1), [19])
    store.record_merge_receipt(
        lease,
        issue_number=19,
        pr_number=23,
        reviewed_head_sha="b" * 40,
        merge_sha="c" * 40,
    )
    store.record_terminal_outcome(
        lease,
        issue_number=19,
        passed=True,
        reason="merged",
        pr_number=23,
    )
    facts = IssueFacts(
        number=19,
        title="Merged issue",
        body="",
        is_epic=False,
        labels=set(),
        pr_number=23,
        pr_is_open=False,
        pr_is_merged=True,
        issue_is_closed=True,
    )
    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", lambda *_args: facts)
    coordinator = Coordinator(
        PipelineConfig(
            org="acme",
            repos=["hephaestus"],
            projects_dir=tmp_path,
            repo_roots={"hephaestus": intake_root},
            repo_state_roots={"hephaestus": durable_root},
        ),
        github=FakeStageGitHub(),
        **fake_worker_factories(),
        install_signals=False,
    )

    entry = coordinator._classify_repo_issue_entry(
        "hephaestus",
        RepoIssueSource(metadata=iter(()), wave_lease=lease),
        19,
        coordinator.github,
    )

    assert entry is not None
    assert entry.stage is StageName.FINISHED
    assert entry.passed is True
    assert not (intake_root / "build" / ".automation-state").exists()


def test_explicit_pr_scope_syncs_before_labels_and_pr_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit PR follows the same checkout proof before its first read."""
    events: list[str] = []
    _initialize_recording_checkout(tmp_path / "repo-a")
    pool = _RecordingPool(events)
    github = _RecordingGitHub(events, pr_issue=101, pr_impl_state=(True, False))
    monkeypatch.setattr(RepoIntakeManager, "adoption_guard", _accept_recording_intake)

    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            prs=[77],
            projects_dir=tmp_path,
            scope=PipelineScope(frozenset({StageName.MERGE_WAIT})),
            rate_guard_enabled=False,
        ),
        github=github,
        **fake_worker_factories(pool, None),
        install_signals=False,
    )
    coordinator.stages[StageName.MERGE_WAIT] = _ImmediatePassStage()

    assert coordinator.run() == 0
    assert events[:3] == ["intake", "labels", "classify-pr"]


@pytest.mark.parametrize(
    "sync_error",
    ["fetch unavailable", "checkout is dirty", "cannot fast-forward checkout"],
)
def test_explicit_scope_sync_failure_blocks_labels_sources_and_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sync_error: str
) -> None:
    """Fetch, dirty, or stale checkout failures cannot enter a scoped run."""
    checkout = tmp_path / "repo-a"
    checkout.mkdir()
    classifications: list[int] = []
    pool = FakeWorkerPool()
    pool.script(
        JobResult(ok=False, error=sync_error),
        JobResult(ok=False, error=sync_error),
    )
    github = FakeStageGitHub(labels=["state:needs-plan"])

    def classify(issue: int, github_arg: Any) -> IssueFacts:
        del github_arg
        classifications.append(issue)
        return _facts(issue)

    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", classify)
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.admission._filter_open_issues",
        lambda _repo, issues, **_kwargs: list(issues),
    )
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            issues=[101],
            projects_dir=tmp_path,
            rate_guard_enabled=False,
        ),
        github=github,
        **fake_worker_factories(pool, None),
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _ImmediatePassStage()

    assert coordinator.run() == 1
    assert classifications == []
    assert github.mutation_log == []
    assert [handle.job.op for handle in pool.submitted if isinstance(handle.job, GitJob)] == [
        "prepare_intake",
        "prepare_intake",
    ]
    assert not any(isinstance(handle.job, AgentJob) for handle in pool.submitted)
    assert len(coordinator.ledger) == 1
    assert "clone exhausted" in coordinator.ledger[0].reason


def test_malformed_intake_receipt_blocks_labels_sources_and_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed successful receipt cannot enter the scoped pipeline."""
    checkout = tmp_path / "repo-a"
    checkout.mkdir()
    classifications: list[int] = []
    pool = FakeWorkerPool()
    pool.script(JobResult(ok=True, value={"revision": "a" * 40}))
    github = FakeStageGitHub(labels=["state:needs-plan"])

    def classify(issue: int, github_arg: Any) -> IssueFacts:
        del github_arg
        classifications.append(issue)
        return _facts(issue)

    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", classify)
    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo-a"], issues=[101], projects_dir=tmp_path),
        github=github,
        **fake_worker_factories(pool, None),
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _ImmediatePassStage()

    assert coordinator.run() == 1
    assert classifications == []
    assert github.mutation_log == []
    assert not any(isinstance(handle.job, AgentJob) for handle in pool.submitted)
    assert len(coordinator.ledger) == 1
    assert "repository-intake receipt invalid" in coordinator.ledger[0].reason
