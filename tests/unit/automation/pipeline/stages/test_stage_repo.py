"""RepoStage tests: label ensure, clone, and discovery walk (#1817).

Doc section "1. repo" is the binding contract: ENTER -> CLONE_WAIT ->
DISCOVER -> SOURCE; budget clone=2.  The stage initializes only a bounded
metadata cursor; the coordinator owns one-at-a-time classification, semantic
planning review, and terminal completion.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import hephaestus.automation.loop_repo_manager as loop_repo_manager_mod
from hephaestus.automation.issue_waves import WAVE_LEASE_PAYLOAD, IssueWaveStore, WaveLease
from hephaestus.automation.learning_journal import LearningJournalStore
from hephaestus.automation.pipeline import seeding as seeding_mod
from hephaestus.automation.pipeline.jobs import GitJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.seeding import IssueFacts
from hephaestus.automation.pipeline.stages.base import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.repo import (
    SYNCED_MAIN_SHA_KEY,
    RepoIssueSource,
    RepoStage,
)
from hephaestus.automation.pipeline.work_item import ItemKind, LearningIntent, WorkItem

from .conftest import FakeStageGitHub


class _RepoPaths:
    """Provide explicit project and repository paths."""

    def __init__(self, projects_dir: Path, *, repo_root: Path) -> None:
        self.projects_dir = projects_dir
        self.repo_root = repo_root
        self.worktree = self.repo_root


@pytest.fixture
def repo_item() -> WorkItem:
    """Fresh repo-kind work item at ENTER."""
    return WorkItem(repo="repo-a", kind=ItemKind.REPO, stage=StageName.REPO, state="ENTER")


@pytest.fixture
def repo_ctx(tmp_path: Path, make_ctx: Callable[..., Any]) -> Any:
    """Provide the checkout path selected by the coordinator."""
    return make_ctx(paths=_RepoPaths(tmp_path, repo_root=tmp_path / "repo-a"))


def _facts(
    number: int,
    *,
    title: str | None = None,
    body: str = "",
    labels: set[str] | None = None,
    pr: int | None = None,
    pr_open: bool = False,
    pr_merged: bool = False,
) -> IssueFacts:
    return IssueFacts(
        number=number,
        title=title or f"task {number}",
        is_epic=False,
        labels=labels or set(),
        pr_number=pr,
        pr_is_open=pr_open,
        pr_is_merged=pr_merged,
        body=body,
    )


class TestOnEnterAndCloneStates:
    """Checkout proof precedes label setup and discovery."""

    def test_labels_are_deferred_until_checkout_is_verified(
        self, repo_item: WorkItem, repo_ctx: Any
    ) -> None:
        stage = RepoStage()

        assert stage.on_enter(repo_item, repo_ctx) is None
        assert repo_ctx.github.mutation_log == []

        repo_item.state = "LABELS"
        result = stage.step(repo_item, repo_ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "DISCOVER"
        assert ("ensure_state_labels", ()) in repo_ctx.github.mutation_log

    def test_enter_state_advances_to_clone_wait(self, repo_item: WorkItem, repo_ctx: Any) -> None:
        result = RepoStage().step(repo_item, repo_ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "CLONE_WAIT"

    def test_clone_wait_submits_git_clone_job(
        self, repo_item: WorkItem, repo_ctx: Any, tmp_path: Path
    ) -> None:
        """A missing checkout submits GitJob(op="clone") with repo/dest kwargs."""
        repo_item.state = "CLONE_WAIT"

        result = RepoStage().step(repo_item, repo_ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "clone"
        assert result.job.kwargs == {
            "repo": "test-org/repo-a",
            "dest": str(tmp_path / "repo-a"),
        }
        assert result.on_done_state == "CLONE_WAIT"

    def test_existing_checkout_submits_sync_job_before_discovery(
        self, repo_item: WorkItem, repo_ctx: Any, tmp_path: Path
    ) -> None:
        """A pre-existing checkout is synchronized before its issues are read."""
        (tmp_path / "repo-a").mkdir()
        repo_item.state = "CLONE_WAIT"

        result = RepoStage().step(repo_item, repo_ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "sync_checkout"
        assert result.job.kwargs == {
            "repo": "test-org/repo-a",
            "dest": str(tmp_path / "repo-a"),
        }
        assert result.on_done_state == "CLONE_WAIT"

    def test_successful_clone_requires_follow_up_sync_before_labels(
        self, repo_item: WorkItem, repo_ctx: Any
    ) -> None:
        """A new clone cannot reach labels or discovery without a sync proof."""
        repo_item.state = "CLONE_WAIT"
        stage = RepoStage()

        clone = stage.step(repo_item, repo_ctx)
        assert isinstance(clone, JobRequest)
        assert isinstance(clone.job, GitJob)
        assert clone.job.op == "clone"

        stage.on_job_done(repo_item, JobResult(ok=True), repo_ctx)
        sync = stage.step(repo_item, repo_ctx)
        assert isinstance(sync, JobRequest)
        assert isinstance(sync.job, GitJob)
        assert sync.job.op == "sync_checkout"

        stage.on_job_done(repo_item, JobResult(ok=True, value="a" * 40), repo_ctx)
        ready = stage.step(repo_item, repo_ctx)
        assert isinstance(ready, Continue)
        assert ready.next_state == "WAVE_ADMIT"

    def test_explicit_existing_repo_root_submits_sync_job(
        self, repo_item: WorkItem, tmp_path: Path, make_ctx: Callable[..., Any]
    ) -> None:
        """An isolated existing checkout is synchronized at its explicit path."""
        projects_dir = tmp_path / "projects"
        checkout = tmp_path / "isolated" / "repo-a-worktree"
        checkout.mkdir(parents=True)
        ctx = make_ctx(paths=_RepoPaths(projects_dir, repo_root=checkout))
        repo_item.state = "CLONE_WAIT"

        result = RepoStage().step(repo_item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "sync_checkout"
        assert result.job.kwargs == {
            "repo": "test-org/repo-a",
            "dest": str(checkout),
        }

    def test_clone_skipped_in_dry_run(
        self, repo_item: WorkItem, tmp_path: Path, make_ctx: Callable[..., Any]
    ) -> None:
        """[dry-run] logs the would-clone and proceeds — no job submitted."""
        ctx = make_ctx(dry_run=True, paths=_RepoPaths(tmp_path, repo_root=tmp_path / "repo-a"))
        repo_item.state = "CLONE_WAIT"

        result = RepoStage().step(repo_item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "WAVE_ADMIT"

    def test_existing_checkout_is_not_synchronized_in_dry_run(
        self,
        repo_item: WorkItem,
        tmp_path: Path,
        make_ctx: Callable[..., Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Dry runs report a reusable checkout sync without submitting a GitJob."""
        (tmp_path / "repo-a").mkdir()
        ctx = make_ctx(dry_run=True, paths=_RepoPaths(tmp_path, repo_root=tmp_path / "repo-a"))
        repo_item.state = "CLONE_WAIT"
        caplog.set_level(logging.INFO, logger="hephaestus.automation.pipeline.stages.repo")

        result = RepoStage().step(repo_item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "WAVE_ADMIT"
        assert "[dry-run] would synchronize test-org/repo-a" in caplog.text

    def test_clone_failure_retries_within_budget(self, repo_item: WorkItem, repo_ctx: Any) -> None:
        """First failure (attempt 1 < budget 2) re-submits the clone."""
        stage = RepoStage()
        repo_item.state = "CLONE_WAIT"
        stage.on_job_done(repo_item, JobResult(ok=False, error="network"), repo_ctx)
        assert repo_item.attempts["clone"] == 1

        result = stage.step(repo_item, repo_ctx)

        assert isinstance(result, JobRequest)  # retry
        assert isinstance(result.job, GitJob) and result.job.op == "clone"

    def test_clone_exhaustion_finishes_failed(self, repo_item: WorkItem, repo_ctx: Any) -> None:
        """Budget clone=2 exhausted -> finished(fail) per the ROUTES row."""
        stage = RepoStage()
        repo_item.state = "CLONE_WAIT"
        for _ in range(2):
            stage.on_job_done(repo_item, JobResult(ok=False, error="network"), repo_ctx)

        result = stage.step(repo_item, repo_ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.FINISH_FAIL
        assert "clone exhausted" in result.note

    def test_clone_success_records_nothing(self, repo_item: WorkItem, repo_ctx: Any) -> None:
        stage = RepoStage()
        repo_item.state = "CLONE_WAIT"
        repo_item.payload["checkout_op"] = "sync_checkout"

        stage.on_job_done(repo_item, JobResult(ok=True, value="a" * 40), repo_ctx)

        assert repo_item.attempts["clone"] == 0
        assert "clone_failed" not in repo_item.payload
        assert repo_item.payload["checkout_verified"] is True

    @pytest.mark.parametrize("has_issue_limit", [False, True])
    @pytest.mark.parametrize("checkout_exists", [False, True])
    def test_invalid_sync_revision_cannot_use_a_fixture_fallback(
        self,
        repo_item: WorkItem,
        tmp_path: Path,
        make_ctx: Callable[..., Any],
        has_issue_limit: bool,
        checkout_exists: bool,
    ) -> None:
        """A malformed worker revision is a failed checkout in every context."""
        checkout = tmp_path / "checkout"
        if checkout_exists:
            checkout.mkdir()
        config: Any = SimpleNamespace(issue_limit=None) if has_issue_limit else SimpleNamespace()
        ctx = make_ctx(config=config, paths=_RepoPaths(tmp_path, repo_root=checkout))
        repo_item.state = "CLONE_WAIT"
        repo_item.payload["checkout_op"] = "sync_checkout"
        stage = RepoStage()

        stage.on_job_done(repo_item, JobResult(ok=True, value="invalid-revision"), ctx)

        assert repo_item.attempts.get("clone") == 1
        assert repo_item.payload.get("clone_failed") is True
        assert not repo_item.payload.get("checkout_verified")
        assert SYNCED_MAIN_SHA_KEY not in repo_item.payload
        assert ctx.github.mutation_log == []
        retry = stage.step(repo_item, ctx)
        assert isinstance(retry, JobRequest)

    def test_checkout_uses_the_explicit_root_even_when_it_is_the_projects_directory(
        self,
        repo_item: WorkItem,
        tmp_path: Path,
        make_ctx: Callable[..., Any],
    ) -> None:
        """A path equality cannot change the repository selected by the coordinator."""
        ctx = make_ctx(paths=_RepoPaths(tmp_path, repo_root=tmp_path))
        repo_item.state = "CLONE_WAIT"

        request = RepoStage().step(repo_item, ctx)

        assert isinstance(request, JobRequest)
        assert isinstance(request.job, GitJob)
        assert request.job.op == "sync_checkout"
        assert request.job.kwargs["dest"] == str(tmp_path)


_WAVE_BASE = "a" * 40
_WAVE_HEAD = "b" * 40
_WAVE_MERGE = "c" * 40


def _passing_wave_store(
    root: Path, *, final_wave: bool = False
) -> tuple[IssueWaveStore, WaveLease]:
    """Record a passing wave that still needs ancestry verification."""
    store = IssueWaveStore(root, "test-org", "repo-a")
    if final_wave:
        for limit in (1, 2, 4, 8):
            empty = store.seal_selection(store.plan_admission(_WAVE_BASE, limit), [])
            store.verify_prior_wave(empty, current_main_sha=_WAVE_BASE, ancestry_verified=True)
    lease = store.seal_selection(store.plan_admission(_WAVE_BASE, None if final_wave else 1), [7])
    store.record_merge_receipt(
        lease,
        issue_number=7,
        pr_number=17,
        reviewed_head_sha=_WAVE_HEAD,
        merge_sha=_WAVE_MERGE,
    )
    store.record_terminal_outcome(lease, issue_number=7, passed=True, reason="merged", pr_number=17)
    return store, lease


def _wave_item() -> WorkItem:
    """Enter wave admission with the synchronized default-branch revision."""
    return WorkItem(
        repo="repo-a",
        kind=ItemKind.REPO,
        stage=StageName.REPO,
        state="WAVE_ADMIT",
        payload={SYNCED_MAIN_SHA_KEY: _WAVE_MERGE},
    )


class TestIssueWaveRecovery:
    """Fresh ancestry and issue facts control durable wave recovery."""

    @pytest.mark.parametrize("concurrent_update", [False, True], ids=["advance", "concurrent"])
    def test_next_wave_uses_its_verified_checkpoint_generation(
        self,
        tmp_path: Path,
        make_ctx: Callable[..., Any],
        monkeypatch: pytest.MonkeyPatch,
        concurrent_update: bool,
    ) -> None:
        """A wave can use its own verification write but cannot absorb another writer."""
        store, lease = _passing_wave_store(tmp_path)
        ctx = make_ctx(
            paths=_RepoPaths(tmp_path, repo_root=tmp_path), config_overrides={"issue_limit": 2}
        )
        item = _wave_item()
        facts = {
            7: replace(_facts(7, pr=17, pr_merged=True), issue_is_closed=True),
            8: _facts(8, labels={"state:plan-blocked"}),
            9: _facts(9),
            10: _facts(10, labels={"state:skip"}),
            11: _facts(11),
            12: _facts(12),
        }
        discovered: list[int] = []
        reads: list[int] = []

        def read_issue(number: int, github: Any) -> IssueFacts:
            assert github is ctx.github
            reads.append(number)
            return facts[number]

        def discover(org: str, repo: str, **kwargs: object) -> Iterator[dict[str, Any]]:
            assert (org, repo) == ("test-org", "repo-a")
            assert kwargs == {
                "network_timeout": ctx.config.network_timeout,
                "shutdown": ctx.cancellation,
            }
            if concurrent_update:
                other = IssueWaveStore(tmp_path, "test-org", "repo-a")
                other.verify_prior_wave(
                    lease,
                    current_main_sha=_WAVE_MERGE,
                    ancestry_verified=True,
                    facts_by_issue={7: facts[7]},
                )
            for number in (7, 8, 9, 10, 11, 12):
                discovered.append(number)
                yield {"number": number}

        monkeypatch.setattr(seeding_mod, "seed_issue_from_github", read_issue)
        monkeypatch.setattr(loop_repo_manager_mod, "_iter_open_issue_meta", discover)
        stage = RepoStage()
        pending = stage.step(item, ctx)

        assert isinstance(pending, JobRequest)
        assert isinstance(pending.job, GitJob)
        assert pending.job.op == "verify_issue_wave_ancestry"
        assert pending.job.kwargs == {
            "repo_root": str(tmp_path),
            "main_sha": _WAVE_MERGE,
            "ancestor_shas": (_WAVE_BASE, _WAVE_MERGE),
        }
        assert discovered == reads == []
        item.state = pending.on_done_state
        stage.on_job_done(
            item,
            JobResult(
                ok=True, value={"main_sha": _WAVE_MERGE, "ancestors": (_WAVE_BASE, _WAVE_MERGE)}
            ),
            ctx,
        )
        outcome = stage.step(item, ctx)

        assert discovered == [7, 8, 9, 10, 11]
        assert reads == [7, 7, 8, 9, 10, 11]
        checkpoint = store.load()
        assert checkpoint is not None
        assert checkpoint.waves[0].verified_main_sha == _WAVE_MERGE
        assert ctx.github.mutation_log == []
        if concurrent_update:
            assert isinstance(outcome, StageOutcome)
            assert outcome.disposition is Disposition.FINISH_FAIL
            assert "checkpoint changed before selection sealing" in outcome.note
            assert len(checkpoint.waves) == 1
            assert checkpoint.current_wave.issue_numbers == (7,)
            assert "_repo_issue_source" not in item.payload
            return

        assert isinstance(outcome, Continue)
        assert outcome.next_state == "LABELS"
        assert len(checkpoint.waves) == 2
        selected = item.payload[WAVE_LEASE_PAYLOAD]
        assert selected.issue_numbers == (9, 11)
        assert selected.base_main_sha == _WAVE_MERGE
        source = item.payload["_repo_issue_source"]
        assert isinstance(source, RepoIssueSource)
        assert source.wave_lease == selected
        assert source.one_pass is True
        assert [row["number"] for row in source.metadata] == [9, 11]
        resumed = IssueWaveStore(tmp_path, "test-org", "repo-a").plan_admission(_WAVE_MERGE, 2)
        assert resumed.mode == "resume"
        assert resumed.lease == selected

    def test_final_wave_closes_once_and_rechecks_facts_on_restart(
        self,
        tmp_path: Path,
        make_ctx: Callable[..., Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A final audit is durable, and later audits cannot trust changed merge facts."""
        store, _lease = _passing_wave_store(tmp_path, final_wave=True)
        ctx = make_ctx(
            paths=_RepoPaths(tmp_path, repo_root=tmp_path), config_overrides={"issue_limit": None}
        )
        facts = {7: replace(_facts(7, pr=17, pr_merged=True), issue_is_closed=True)}
        reads: list[int] = []

        def read_issue(number: int, github: Any) -> IssueFacts:
            assert github is ctx.github
            reads.append(number)
            return facts[number]

        def no_discovery(*_args: object, **_kwargs: object) -> Iterator[dict[str, Any]]:
            raise AssertionError("a completed rollout must not discover another wave")

        monkeypatch.setattr(seeding_mod, "seed_issue_from_github", read_issue)
        monkeypatch.setattr(loop_repo_manager_mod, "_iter_open_issue_meta", no_discovery)
        stage = RepoStage()
        item = _wave_item()
        pending = stage.step(item, ctx)

        assert isinstance(pending, JobRequest)
        assert isinstance(pending.job, GitJob)
        assert pending.job.op == "verify_issue_wave_ancestry"
        assert reads == []
        item.state = pending.on_done_state
        stage.on_job_done(
            item,
            JobResult(
                ok=True, value={"main_sha": _WAVE_MERGE, "ancestors": (_WAVE_BASE, _WAVE_MERGE)}
            ),
            ctx,
        )
        completed = stage.step(item, ctx)

        assert isinstance(completed, StageOutcome)
        assert completed.disposition is Disposition.FINISH_PASS
        checkpoint = store.load()
        assert checkpoint is not None
        assert checkpoint.status == "completed"
        assert checkpoint.completed_main_sha == _WAVE_MERGE
        saved = store.checkpoint_path.read_bytes()
        restarted = RepoStage().step(_wave_item(), ctx)

        assert isinstance(restarted, StageOutcome)
        assert restarted.disposition is Disposition.FINISH_PASS
        assert "audit-only" in restarted.note
        assert store.checkpoint_path.read_bytes() == saved
        facts[7] = replace(facts[7], pr_is_merged=False)
        changed = RepoStage().step(_wave_item(), ctx)

        assert isinstance(changed, StageOutcome)
        assert changed.disposition is Disposition.FINISH_FAIL
        assert "recorded merged PR" in changed.note
        assert reads == [7, 7, 7]
        assert store.checkpoint_path.read_bytes() == saved
        assert ctx.github.mutation_log == []

    def test_failed_ancestry_preserves_checkpoint_before_any_issue_read(
        self,
        tmp_path: Path,
        make_ctx: Callable[..., Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed ancestry job cannot create a new source or modify its checkpoint."""
        store, _lease = _passing_wave_store(tmp_path)
        saved = store.checkpoint_path.read_bytes()
        ctx = make_ctx(
            paths=_RepoPaths(tmp_path, repo_root=tmp_path), config_overrides={"issue_limit": 2}
        )

        def no_read(*_args: object, **_kwargs: object) -> Any:
            raise AssertionError("issue reads require successful ancestry verification")

        monkeypatch.setattr(seeding_mod, "seed_issue_from_github", no_read)
        monkeypatch.setattr(loop_repo_manager_mod, "_iter_open_issue_meta", no_read)
        stage = RepoStage()
        item = _wave_item()
        pending = stage.step(item, ctx)

        assert isinstance(pending, JobRequest)
        item.state = pending.on_done_state
        stage.on_job_done(item, JobResult(ok=False, error="merge is not on main"), ctx)
        outcome = stage.step(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition is Disposition.FINISH_FAIL
        assert outcome.note == "merge is not on main"
        assert "_repo_issue_source" not in item.payload
        assert WAVE_LEASE_PAYLOAD not in item.payload
        assert store.checkpoint_path.read_bytes() == saved
        assert ctx.github.mutation_log == []


class TestDiscover:
    """Step 3 [M]: list, dedup, epic-tag-before-exclude, classify, orphans."""

    def _patch_discovery(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        meta: list[dict[str, Any]],
        facts: dict[int, IssueFacts],
        classifications: dict[int, tuple[StageName | None, str]],
    ) -> list[int]:
        """Patch the repo-stage read seams; returns the classify-call order."""
        classified: list[int] = []
        monkeypatch.setattr(
            loop_repo_manager_mod, "_iter_open_issue_meta", lambda org, repo, **_kwargs: iter(meta)
        )
        monkeypatch.setattr(seeding_mod, "seed_issue_from_github", lambda num, github: facts[num])

        def fake_classify(f: IssueFacts) -> tuple[StageName | None, str]:
            classified.append(f.number)
            return classifications[f.number]

        monkeypatch.setattr(seeding_mod, "classify_issue", fake_classify)
        return classified

    def test_discover_initializes_source_without_eager_issue_reads(
        self,
        repo_item: WorkItem,
        tmp_path: Path,
        make_ctx: Callable[..., Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Repo discovery must not fall back to current-repo seeding helpers."""

        class RepoScopedGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__(open_pr=44)
                self.issue_reads: list[int] = []

            def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
                self.issue_reads.append(issue_number)
                return {
                    "number": issue_number,
                    "title": "repo-specific task",
                    "state": "OPEN",
                    "body": "",
                    "labels": [{"name": "state:implementation-go"}],
                }

            def find_merged_pr_for_issue(self, issue_number: int) -> int | None:
                return None

        github = RepoScopedGitHub()
        ctx = make_ctx(github=github, paths=_RepoPaths(tmp_path, repo_root=tmp_path))
        monkeypatch.setattr(
            loop_repo_manager_mod,
            "_iter_open_issue_meta",
            lambda org, repo, **_kwargs: iter(
                [{"number": 8, "labels": ["state:implementation-go"], "title": "x"}]
            ),
        )
        repo_item.state = "DISCOVER"

        result = RepoStage().step(repo_item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "SOURCE"
        assert github.issue_reads == []
        assert "products" not in repo_item.payload
        assert "_repo_issue_source" in repo_item.payload

    def test_discover_failure_finishes_fail(
        self,
        repo_item: WorkItem,
        repo_ctx: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Discovery failures are not converted into a successful empty run."""
        monkeypatch.setattr(
            loop_repo_manager_mod,
            "_iter_open_issue_meta",
            lambda org, repo, **_kwargs: (_ for _ in ()).throw(RuntimeError("gh failed")),
        )
        repo_item.state = "DISCOVER"

        result = RepoStage().step(repo_item, repo_ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.FINISH_FAIL
        assert "discovery failed" in result.note

    def test_discover_retains_only_a_metadata_cursor(
        self,
        repo_item: WorkItem,
        repo_ctx: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Discovery does not classify/dedup into an unbounded product list."""
        meta = [
            {"number": 1, "labels": [], "title": "one"},
            {"number": 2, "labels": ["state:plan-go"], "title": "two"},
            {"number": 1, "labels": [], "title": "one (dup)"},  # deduped
        ]
        classified = self._patch_discovery(
            monkeypatch,
            meta=meta,
            facts={1: _facts(1), 2: _facts(2, labels={"state:plan-go"})},
            classifications={
                1: (StageName.PLANNING, "needs plan"),
                2: (StageName.IMPLEMENTATION, "plan approved"),
            },
        )
        repo_item.state = "DISCOVER"

        result = RepoStage().step(repo_item, repo_ctx)

        assert isinstance(result, Continue) and result.next_state == "SOURCE"
        assert classified == []
        assert "products" not in repo_item.payload
        assert "seeded_count" not in repo_item.payload

    def test_discovery_stream_includes_closed_issue_with_pending_learning(
        self,
        tmp_path: Path,
        repo_item: WorkItem,
        make_ctx: Callable[..., Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Closed issues remain discoverable while their journal work is pending."""
        monkeypatch.setattr(
            loop_repo_manager_mod,
            "_iter_open_issue_meta",
            lambda _org, _repo, **_kwargs: iter(()),
        )
        journal = LearningJournalStore(lambda: tmp_path)
        intent = LearningIntent.post_merge(repo="repo-a", issue=2705, pr=99)
        journal.ensure_pending(
            intent.key,
            kind=intent.kind.value,
            identity=intent.journal_identity(),
        )
        github = FakeStageGitHub(issue_state="CLOSED", issue_title="Merged work")
        ctx = make_ctx(
            paths=_RepoPaths(tmp_path, repo_root=tmp_path),
            github=github,
            learning_journal=journal,
        )
        repo_item.state = "DISCOVER"

        result = RepoStage().step(repo_item, ctx)

        assert isinstance(result, Continue)
        source = repo_item.payload["_repo_issue_source"]
        assert next(source.metadata) == {
            "number": 2705,
            "labels": [],
            "title": "Merged work",
        }

    def test_discover_does_not_mutate_before_source_consumption(
        self,
        repo_item: WorkItem,
        repo_ctx: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The source owns the later durable epic-tagging side effect."""
        meta = [
            {"number": 5, "labels": ["epic"], "title": "Epic: umbrella"},
            {"number": 6, "labels": [], "title": "real work"},
        ]
        classified = self._patch_discovery(
            monkeypatch,
            meta=meta,
            facts={6: _facts(6)},
            classifications={6: (StageName.PLANNING, "needs plan")},
        )
        gh: FakeStageGitHub = repo_ctx.github
        repo_item.state = "DISCOVER"

        RepoStage().step(repo_item, repo_ctx)

        assert gh.mutation_log == []
        assert classified == []
        assert "products" not in repo_item.payload

    def test_discover_has_no_epic_tag_write_path(
        self,
        repo_item: WorkItem,
        repo_ctx: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Tracker candidates remain read-only until semantic planning review."""
        meta = [
            {"number": 5, "labels": ["epic"], "title": "Epic: umbrella"},
            {"number": 6, "labels": [], "title": "real work"},
        ]
        classified = self._patch_discovery(
            monkeypatch,
            meta=meta,
            facts={6: _facts(6)},
            classifications={6: (StageName.PLANNING, "needs plan")},
        )
        gh: FakeStageGitHub = repo_ctx.github
        assert not hasattr(gh, "skip_epics")
        repo_item.state = "DISCOVER"

        result = RepoStage().step(repo_item, repo_ctx)

        assert isinstance(result, Continue)
        assert "products" not in repo_item.payload
        assert classified == []
        assert 5 not in gh.labels

    def test_source_state_yields_to_the_coordinator(
        self, repo_item: WorkItem, repo_ctx: Any
    ) -> None:
        """SOURCE is a non-spinning yield point for coordinator source pull."""
        repo_item.state = "SOURCE"

        result = RepoStage().step(repo_item, repo_ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "SOURCE"

    def test_unknown_state_finishes_failed(self, repo_item: WorkItem, repo_ctx: Any) -> None:
        repo_item.state = "BOGUS"

        result = RepoStage().step(repo_item, repo_ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.FINISH_FAIL
