"""Regression coverage for explicit CLI scope checkout synchronization."""

from __future__ import annotations

import subprocess
import threading
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
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from tests.unit.automation.pipeline.conftest import FakeWorkerPool, fake_worker_factories
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


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


def test_direct_scope_prepares_real_intake_before_labels_and_preserves_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct scope admits work only after a real isolated intake succeeds."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=master", str(remote)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    checkout = tmp_path / "repo-a"
    checkout.mkdir()
    for arguments in (
        ("init", "--initial-branch=master"),
        ("config", "user.name", "Test User"),
        ("config", "user.email", "test@example.invalid"),
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
    (checkout / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "tracked.txt"], cwd=checkout, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "master"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "remote", "set-url", "origin", "https://github.com/org/repo-a.git"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    updater = tmp_path / "updater"
    subprocess.run(
        ["git", "clone", str(remote), str(updater)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    for arguments in (
        ("config", "user.name", "Test User"),
        ("config", "user.email", "test@example.invalid"),
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=updater,
            check=True,
            capture_output=True,
            text=True,
        )
    (updater / "remote.txt").write_text("remote\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "remote.txt"], cwd=updater, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "commit", "-m", "remote update"],
        cwd=updater,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "push", "origin", "master"],
        cwd=updater,
        check=True,
        capture_output=True,
        text=True,
    )
    remote_head = subprocess.run(
        ["git", "rev-parse", "refs/heads/master"],
        cwd=remote,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (checkout / "tracked.txt").write_text("staged\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "tracked.txt"], cwd=checkout, check=True, capture_output=True, text=True
    )
    (checkout / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    (checkout / "untracked.txt").write_text("keep\n", encoding="utf-8")

    def caller_state() -> tuple[str, str, str, str, str]:
        def output(*arguments: str) -> str:
            return subprocess.run(
                ["git", *arguments],
                cwd=checkout,
                check=True,
                capture_output=True,
                text=True,
            ).stdout

        return (
            output("rev-parse", "HEAD"),
            output("symbolic-ref", "--quiet", "--short", "HEAD"),
            output("diff", "--cached"),
            output("diff"),
            output("status", "--porcelain", "--untracked-files=all"),
        )

    before = caller_state()
    events: list[str] = []
    github = _RecordingGitHub(events)
    auxiliary = FakeWorkerPool()
    auxiliary._auxiliary = True

    class RecordingWorkerPool(WorkerPool):
        """Record only a successful real intake preparation."""

        def _git_prepare_intake(self, job: GitJob) -> JobResult:
            result = super()._git_prepare_intake(job)
            if result.ok:
                events.append("intake-prepared")
            return result

    def pool_factory(
        size: int,
        shutdown: threading.Event,
        completion_q: Any,
    ) -> RecordingWorkerPool:
        return RecordingWorkerPool(size=size, shutdown=shutdown, completion_q=completion_q)

    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\nprintf 'master\\n'\n", encoding="utf-8")
    gh.chmod(0o755)
    remote_rewrite = (
        "-c",
        f"url.{remote.as_uri()}.insteadOf=https://github.com/org/repo-a.git",
    )
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.worker_pool._trusted_gh_executable",
        lambda _root: str(gh),
    )
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.worker_pool._trusted_remote_git_config",
        lambda _gh: remote_rewrite,
    )

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
            scope=PipelineScope(frozenset({StageName.PLANNING})),
            rate_guard_enabled=False,
        ),
        github=github,
        pool_factory=pool_factory,
        auxiliary_pool_factory=auxiliary.factory,
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _ImmediatePassStage()

    assert coordinator.run() == 0
    assert events == ["intake-prepared", "labels", "classify"]
    issue_item = next(item for item in coordinator.items if item.issue == 101)
    intake_root = coordinator.config.repo_roots["repo-a"]
    receipt = coordinator.config.repo_intake_receipts["repo-a"]
    intake_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=intake_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert receipt["revision"] == intake_head == remote_head
    assert issue_item.payload["_direct_scope_base_sha"] == remote_head
    assert coordinator.config.repo_caller_roots["repo-a"] == checkout
    assert coordinator.config.repo_state_roots["repo-a"] == Path(str(receipt["state_root"]))
    assert intake_root == Path(str(receipt["path"]))
    assert auxiliary.submitted == []
    assert caller_state() == before
    assert (checkout / "tracked.txt").read_text(encoding="utf-8") == "unstaged\n"
    assert (checkout / "untracked.txt").read_text(encoding="utf-8") == "keep\n"


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


def _intake_result(caller_root: Path, intake_root: Path) -> JobResult:
    """Return one materialized intake receipt for coordinator adoption tests."""
    caller_root.mkdir(parents=True, exist_ok=True)
    common_dir = caller_root / ".git"
    common_dir.mkdir(exist_ok=True)
    intake_root.mkdir(parents=True)
    (intake_root / ".git").write_text("gitdir: fake\n", encoding="utf-8")
    return JobResult(
        ok=True,
        value={
            "schema_version": 2,
            "repository": "org/repo-a",
            "repository_identity": "org/repo-a:identity",
            "ownership_key": "org/repo-a:identity:intake",
            "common_dir": str(common_dir),
            "path": str(intake_root),
            "state_root": str(intake_root.parent),
            "default_branch": "main",
            "revision": "a" * 40,
            "generation": 1,
            "detached": True,
            "branch": None,
        },
    )


@pytest.mark.parametrize("state_name", [".automation-state", ".issue_implementer"])
@pytest.mark.parametrize("destination_exists", [False, True])
def test_intake_preparation_failure_preserves_legacy_state_for_recovery(
    tmp_path: Path,
    state_name: str,
    destination_exists: bool,
) -> None:
    """An intake state conflict blocks adoption and preserves both state trees."""
    caller_root = tmp_path / "repo-a"
    intake_root = tmp_path / "intake-owner" / "worktree"
    legacy_state = caller_root / "build" / state_name
    durable_state = intake_root.parent / "build" / state_name
    legacy_state.mkdir(parents=True)
    (legacy_state / "learning.json").write_text('{"state": "pending"}\n', encoding="utf-8")
    if destination_exists:
        durable_state.mkdir(parents=True)
        (durable_state / "current.json").write_text("current\n", encoding="utf-8")
    error = (
        "automation state exists in both legacy and intake roots"
        if destination_exists
        else "legacy automation state requires recovery before intake adoption"
    )
    error = f"{error}: source={legacy_state} destination={durable_state}; preserve both paths"
    pool = FakeWorkerPool()
    pool.script(JobResult(ok=False, error=error), JobResult(ok=False, error=error))
    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo-a"], projects_dir=tmp_path),
        github=FakeStageGitHub(),
        **fake_worker_factories(pool, None),
        install_signals=False,
    )

    assert coordinator.run() == 1
    assert (legacy_state / "learning.json").read_text(encoding="utf-8") == (
        '{"state": "pending"}\n'
    )
    if destination_exists:
        assert (durable_state / "current.json").read_text(encoding="utf-8") == "current\n"
    else:
        assert not durable_state.exists()
    assert coordinator.config.repo_roots == {}
    assert coordinator.config.repo_state_roots == {}
    assert coordinator.config.repo_caller_roots == {}
    assert coordinator.config.repo_intake_receipts == {}
    assert str(legacy_state) in coordinator.ledger[0].reason
    assert str(durable_state) in coordinator.ledger[0].reason
    assert "preserve both paths" in coordinator.ledger[0].reason


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("repository", "ORG/REPO-A"),
        ("repository_identity", "org/repo-a:other"),
        ("ownership_key", "org/repo-a:identity:other"),
        ("common_dir", None),
    ],
)
def test_intake_re_adoption_requires_the_exact_verified_receipt(
    tmp_path: Path,
    field: str,
    changed: object,
) -> None:
    """A later discovery pass cannot change the adopted intake identity."""
    caller_root = tmp_path / "repo-a"
    intake_root = tmp_path / "intake-owner" / "worktree"
    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo-a"], projects_dir=tmp_path),
        github=FakeStageGitHub(),
        **fake_worker_factories(),
        install_signals=False,
    )
    item = WorkItem(repo="repo-a", kind=ItemKind.REPO, stage=StageName.REPO)
    first_result = _intake_result(caller_root, intake_root)

    assert coordinator._adopt_repo_intake(item, first_result) is None
    original_roots = dict(coordinator.config.repo_roots)
    original_state_roots = dict(coordinator.config.repo_state_roots)
    original_caller_roots = dict(coordinator.config.repo_caller_roots)
    original_receipts = dict(coordinator.config.repo_intake_receipts)
    assert isinstance(first_result.value, dict)
    changed_value = dict(first_result.value)
    changed_value[field] = str(tmp_path / f"changed-{field}") if changed is None else changed

    error = coordinator._adopt_repo_intake(item, JobResult(ok=True, value=changed_value))

    assert error == "repository-intake re-adoption changed ownership identity"
    assert coordinator.config.repo_roots == original_roots
    assert coordinator.config.repo_state_roots == original_state_roots
    assert coordinator.config.repo_caller_roots == original_caller_roots
    assert coordinator.config.repo_intake_receipts == original_receipts


@pytest.mark.parametrize(
    ("revision", "default_branch"),
    [
        ("b" * 40, "main"),
        ("a" * 40, "stable"),
        ("b" * 40, "stable"),
    ],
)
def test_intake_re_adoption_accepts_the_next_verified_generation(
    tmp_path: Path,
    revision: str,
    default_branch: str,
) -> None:
    """A verified intake generation can advance without changing authority."""
    caller_root = tmp_path / "repo-a"
    intake_root = tmp_path / "intake-owner" / "worktree"
    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo-a"], projects_dir=tmp_path),
        github=FakeStageGitHub(),
        **fake_worker_factories(),
        install_signals=False,
    )
    item = WorkItem(repo="repo-a", kind=ItemKind.REPO, stage=StageName.REPO)
    first_result = _intake_result(caller_root, intake_root)
    assert coordinator._adopt_repo_intake(item, first_result) is None
    assert isinstance(first_result.value, dict)
    changed_value = dict(first_result.value)
    changed_value.update(
        revision=revision,
        default_branch=default_branch,
        generation=2,
    )

    error = coordinator._adopt_repo_intake(item, JobResult(ok=True, value=changed_value))

    assert error is None
    assert coordinator.config.repo_intake_receipts["repo-a"] == changed_value


@pytest.mark.parametrize(
    ("first_generation", "revision", "default_branch", "next_generation"),
    [
        (1, "b" * 40, "main", 1),
        (1, "a" * 40, "main", 2),
        (1, "b" * 40, "main", 3),
        (2, "b" * 40, "main", 1),
    ],
)
def test_intake_re_adoption_rejects_invalid_generation_continuity(
    tmp_path: Path,
    first_generation: int,
    revision: str,
    default_branch: str,
    next_generation: int,
) -> None:
    """A receipt update must match one manager generation transition."""
    caller_root = tmp_path / "repo-a"
    intake_root = tmp_path / "intake-owner" / "worktree"
    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo-a"], projects_dir=tmp_path),
        github=FakeStageGitHub(),
        **fake_worker_factories(),
        install_signals=False,
    )
    item = WorkItem(repo="repo-a", kind=ItemKind.REPO, stage=StageName.REPO)
    first_result = _intake_result(caller_root, intake_root)
    assert isinstance(first_result.value, dict)
    first_value = dict(first_result.value)
    first_value["generation"] = first_generation
    assert coordinator._adopt_repo_intake(item, JobResult(ok=True, value=first_value)) is None
    changed_value = dict(first_value)
    changed_value.update(
        revision=revision,
        default_branch=default_branch,
        generation=next_generation,
    )

    error = coordinator._adopt_repo_intake(item, JobResult(ok=True, value=changed_value))

    assert error == "repository-intake re-adoption has invalid generation continuity"
    assert coordinator.config.repo_intake_receipts["repo-a"] == first_value


@pytest.mark.parametrize(
    ("root_map", "expected_error"),
    [
        ("repo_roots", "repository-intake receipt changed the verified intake path"),
        ("repo_state_roots", "repository-intake receipt changed the durable state root"),
    ],
)
def test_intake_re_adoption_requires_stable_adopted_roots(
    tmp_path: Path,
    root_map: str,
    expected_error: str,
) -> None:
    """An exact receipt cannot replace a changed operational or state root."""
    caller_root = tmp_path / "repo-a"
    intake_root = tmp_path / "intake-owner" / "worktree"
    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo-a"], projects_dir=tmp_path),
        github=FakeStageGitHub(),
        **fake_worker_factories(),
        install_signals=False,
    )
    item = WorkItem(repo="repo-a", kind=ItemKind.REPO, stage=StageName.REPO)
    result = _intake_result(caller_root, intake_root)
    assert coordinator._adopt_repo_intake(item, result) is None
    roots = getattr(coordinator.config, root_map)
    roots["repo-a"] = tmp_path / "changed-root"
    original_maps = (
        dict(coordinator.config.repo_roots),
        dict(coordinator.config.repo_state_roots),
        dict(coordinator.config.repo_caller_roots),
        dict(coordinator.config.repo_intake_receipts),
    )

    error = coordinator._adopt_repo_intake(item, result)

    assert error == expected_error
    assert original_maps == (
        coordinator.config.repo_roots,
        coordinator.config.repo_state_roots,
        coordinator.config.repo_caller_roots,
        coordinator.config.repo_intake_receipts,
    )


def test_full_discovery_reseed_reuses_original_caller_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second discovery pass prepares the same intake from the first caller."""
    caller_root = tmp_path / "repo-a"
    caller_root.mkdir()
    events: list[tuple[str, int]] = []
    pool = FakeWorkerPool()
    first_result = _intake_result(
        caller_root,
        tmp_path / ".repo-a-intake" / "worktree",
    )
    assert isinstance(first_result.value, dict)
    second_value = dict(first_result.value)
    second_value.update(revision="b" * 40, generation=2)
    pool.script(first_result, JobResult(ok=True, value=second_value))

    class _FailThenPassStage(Stage):
        def on_enter(self, item: WorkItem, ctx: Any) -> None:
            del item, ctx

        def step(self, item: WorkItem, ctx: Any) -> StageOutcome:
            del ctx
            assert item.issue == 101
            outcome = "pass" if any(event[0] == "fail" for event in events) else "fail"
            events.append((outcome, item.issue))
            disposition = Disposition.FINISH_PASS if outcome == "pass" else Disposition.FINISH_FAIL
            return StageOutcome(disposition, outcome)

        def on_job_done(self, item: WorkItem, result: Any, ctx: Any) -> None:
            del item, result, ctx
            raise AssertionError("the deterministic stage does not submit jobs")

    monkeypatch.setattr(
        "hephaestus.automation.loop_repo_manager._iter_open_issue_meta",
        lambda _org, _repo, **_kwargs: iter(
            [{"number": 101, "labels": ["state:needs-plan"], "title": "retry"}]
        ),
    )
    monkeypatch.setattr(seeding_mod, "seed_issue_from_github", lambda issue, _github: _facts(issue))
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=2,
            parallel_repos=1,
            max_workers=1,
            projects_dir=tmp_path,
            rate_guard_enabled=False,
        ),
        github=FakeStageGitHub(labels=["state:needs-plan"]),
        **fake_worker_factories(pool, None),
        install_signals=False,
    )
    coordinator.stages[StageName.PLANNING] = _FailThenPassStage()

    assert coordinator.run() == 0

    intake_jobs = [
        handle.job
        for handle in pool.submitted
        if isinstance(handle.job, GitJob) and handle.job.op == "prepare_intake"
    ]
    assert [job.kwargs["caller_root"] for job in intake_jobs] == [
        str(caller_root),
        str(caller_root),
    ]
    assert coordinator.config.repo_roots["repo-a"] == tmp_path / ".repo-a-intake" / "worktree"
    assert coordinator.config.repo_caller_roots["repo-a"] == caller_root
    assert coordinator.config.repo_state_roots["repo-a"] == tmp_path / ".repo-a-intake"
    assert coordinator.config.repo_intake_receipts["repo-a"]["revision"] == "b" * 40
    assert coordinator.config.repo_intake_receipts["repo-a"]["generation"] == 2
    assert events == [("fail", 101), ("pass", 101)]


def test_explicit_pr_scope_syncs_before_labels_and_pr_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit PR follows the same checkout proof before its first read."""
    events: list[str] = []
    (tmp_path / "repo-a").mkdir()
    pool = _RecordingPool(events)
    github = _RecordingGitHub(events, pr_issue=101, pr_impl_state=(True, False))

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
