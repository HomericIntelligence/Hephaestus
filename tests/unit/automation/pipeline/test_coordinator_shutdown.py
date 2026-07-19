"""Interrupt semantics + crash-matrix journal reconstruction for the coordinator (#1817).

Interrupt contract (epic #1809): SIGINT/SIGTERM/SIGHUP share one shutdown
Event; interrupted results park items RESUMABLE at their stage — NEVER
FAILED — and ``on_job_done`` is never called for them. Exit code 130.

Crash matrix: after each representative durable mutation, "crash" (discard all
in-memory state) and re-run the seeding classifier against the resulting
FakeGitHub state — the item must land in the same-or-earlier stage, never lost,
never duplicated. The table below covers every GitHub-journal reconstruction
row from docs/AUTOMATION_LOOP_ARCHITECTURE.md.
"""

from __future__ import annotations

import signal as signal_mod
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.claude_invoke import ReviewVerdict
from hephaestus.automation.pipeline import seeding as seeding_mod
from hephaestus.automation.pipeline.coordinator import Coordinator, PipelineConfig
from hephaestus.automation.pipeline.jobs import AgentJob, JobHandle, JobResult
from hephaestus.automation.pipeline.routing import PIPELINE_ORDER, StageName
from hephaestus.automation.pipeline.seeding import IssueFacts, SeedEntry, classify_issue
from hephaestus.automation.pipeline.stages.base import (
    Continue,
    JobRequest,
    StageContext,
    StageOutcome,
)
from hephaestus.automation.pipeline.stages.implementation import ImplementationStage
from hephaestus.automation.pipeline.stages.plan_review import PlanReviewStage
from hephaestus.automation.pipeline.stages.planning import PlanningStage
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.state_labels import (
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
    STATE_NEEDS_PLAN,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
    STATE_SKIP,
)
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _agent_job(issue: int = 1) -> AgentJob:
    return AgentJob(
        repo="repo-a",
        issue=issue,
        agent="claude",
        model="m",
        prompt_builder=lambda **kwargs: "p",
        cwd=Path("/tmp"),
        timeout_s=10,
        descr="stub",
    )


def _verdict(kind: str) -> ReviewVerdict:
    """Build a ReviewVerdict of the given kind for crash-matrix stage drives."""
    return ReviewVerdict(grade=None, verdict=kind, raw=f"review text ({kind})")


class JobRequestingStage:
    """Stage whose first step always requests an agent job."""

    def __init__(self) -> None:
        self.job_done_calls = 0

    def on_enter(self, item: WorkItem, ctx: Any) -> Any:
        return None

    def step(self, item: WorkItem, ctx: Any) -> Any:
        return JobRequest(_agent_job(item.issue or 0), on_done_state="VERIFY")

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: Any) -> None:
        self.job_done_calls += 1


class InterruptingPool(FakeWorkerPool):
    """FakeWorkerPool that simulates SIGINT landing while the job runs.

    Mirrors the real pool's mandatory post-subprocess check: the shutdown
    event is set mid-job and the result comes back ``interrupted=True``.
    """

    def __init__(self, coordinator_shutdown: Any) -> None:
        super().__init__()
        self._coordinator_shutdown = coordinator_shutdown

    def submit(self, job: Any, on_done_state: Any, **kwargs: Any) -> Any:
        self._coordinator_shutdown.set()
        self.queue_result(JobResult(ok=False, interrupted=True, error="interrupted"))
        return super().submit(job, on_done_state, **kwargs)


def _capture_signal_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    handlers: dict[int, Any] = {}

    def fake_signal(signum: int, handler: Any) -> Any:
        previous = handlers.get(signum, signal_mod.SIG_DFL)
        handlers[signum] = handler
        return previous

    def fake_getsignal(signum: int) -> Any:
        return handlers.get(signum, signal_mod.SIG_DFL)

    monkeypatch.setattr(signal_mod, "signal", fake_signal)
    monkeypatch.setattr(signal_mod, "getsignal", fake_getsignal)


def _raise_sigterm() -> None:
    handler = signal_mod.getsignal(signal_mod.SIGTERM)
    assert callable(handler)
    handler(signal_mod.SIGTERM, None)


class GracefulSignalCompletionPool(FakeWorkerPool):
    """Send first SIGTERM during submit, then deliver a non-interrupted result."""

    def submit(self, job: Any, on_done_state: Any, **kwargs: Any) -> Any:
        _raise_sigterm()
        self.queue_result(JobResult(ok=True, value="completed after shutdown"))
        return super().submit(job, on_done_state, **kwargs)


class SecondSignalPool(FakeWorkerPool):
    """Send two SIGTERMs during submit and leave the job in flight."""

    def submit(self, job: Any, on_done_state: Any, **kwargs: Any) -> Any:
        del kwargs
        handle = JobHandle(job=job, on_done_state=on_done_state)
        self.submitted.append(handle)
        _raise_sigterm()
        _raise_sigterm()
        return handle


def _coordinator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed: list[Any] | None = None,
    grace_s: float = 30.0,
    install_signals: bool = False,
) -> Coordinator:
    config = PipelineConfig(
        org="org", repos=["repo-a"], loops=1, projects_dir=tmp_path, grace_s=grace_s
    )
    monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda r, i, p: list(seed or []))
    coordinator = Coordinator(
        config, github=FakeStageGitHub(), pool=FakeWorkerPool(), install_signals=install_signals
    )
    coordinator._rate_budget_ok = lambda: (True, 0.0)  # type: ignore[method-assign]
    return coordinator


class TestInterruptSemantics:
    """Shutdown mid-job: RESUMABLE, never FAILED; exit 130."""

    def test_shutdown_mid_job_parks_resumable_not_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An interrupted job parks its item RESUMABLE; on_job_done never runs."""
        seed = [SeedEntry(kind="issue", identifier=1, stage=StageName.PLANNING, reason="r")]
        coordinator = _coordinator(tmp_path, monkeypatch, seed=seed)
        pool = InterruptingPool(coordinator.shutdown)
        coordinator.pool = pool
        coordinator.completion_q = pool.completion_q
        stage = JobRequestingStage()
        coordinator.stages[StageName.PLANNING] = stage

        exit_code = coordinator.run()

        assert exit_code == 130
        assert stage.job_done_calls == 0  # never called for interrupted results
        assert coordinator.ledger == []  # nothing recorded as FAILED
        item = coordinator.items[0]
        assert item.result is not None
        assert item.result.reason == "resumable at planning"
        assert not item.result.passed
        assert item.stage is StageName.PLANNING  # never advanced

    def test_graceful_shutdown_stops_admission_and_marks_queued_resumable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Queued (never-started) items report RESUMABLE at their stage."""
        coordinator = _coordinator(tmp_path, monkeypatch)
        item = WorkItem(
            repo="repo-a", kind=ItemKind.ISSUE, issue=2, stage=StageName.PR_REVIEW, state="ENTER"
        )
        coordinator._push_item(item, StageName.PR_REVIEW, enter=True)
        coordinator.shutdown.set()

        exit_code = coordinator.run()

        assert exit_code == 130
        assert item.result is not None
        assert item.result.reason == "resumable at pr_review"
        assert coordinator.ledger == []

    def test_second_signal_immediate_teardown_synthesizes_interrupted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Immediate shutdown cancels the pool and parks in-flight items."""
        seed = [SeedEntry(kind="issue", identifier=3, stage=StageName.IMPLEMENTATION, reason="r")]
        _capture_signal_handlers(monkeypatch)
        coordinator = _coordinator(tmp_path, monkeypatch, seed=seed, install_signals=True)
        pool = SecondSignalPool()
        coordinator.pool = pool
        coordinator.completion_q = pool.completion_q
        stage = JobRequestingStage()
        coordinator.stages[StageName.IMPLEMENTATION] = stage

        exit_code = coordinator.run()

        assert exit_code == 130
        assert len(pool.submitted) == 1
        assert pool.shutdown_event.is_set()
        assert stage.job_done_calls == 0
        assert coordinator.ledger == []
        item = coordinator.items[0]
        assert item.result is not None
        assert item.result.reason == "resumable at implementation"
        assert not item.result.passed
        assert item.stage is StageName.IMPLEMENTATION

    def test_completion_after_graceful_shutdown_does_not_advance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-interrupted completion during shutdown still parks RESUMABLE."""
        seed = [SeedEntry(kind="issue", identifier=4, stage=StageName.PLANNING, reason="r")]
        _capture_signal_handlers(monkeypatch)
        coordinator = _coordinator(tmp_path, monkeypatch, seed=seed, install_signals=True)
        pool = GracefulSignalCompletionPool()
        coordinator.pool = pool
        coordinator.completion_q = pool.completion_q
        stage = JobRequestingStage()
        coordinator.stages[StageName.PLANNING] = stage

        exit_code = coordinator.run()

        assert exit_code == 130
        assert len(pool.submitted) == 1
        assert stage.job_done_calls == 1
        assert coordinator.ledger == []
        item = coordinator.items[0]
        assert item.result is not None
        assert item.result.reason == "resumable at planning"
        assert item.stage is StageName.PLANNING

    def test_signal_handler_escalation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First signal = graceful (grace deadline); second = immediate."""
        coordinator = _coordinator(tmp_path, monkeypatch, grace_s=17.0)
        coordinator._install_signal_handlers()
        import signal as signal_mod

        handler = signal_mod.getsignal(signal_mod.SIGTERM)
        assert callable(handler)

        handler(signal_mod.SIGTERM, None)
        assert coordinator.shutdown.is_set()
        assert coordinator._grace_deadline is not None
        assert coordinator._immediate is False
        assert not coordinator.completion_q.empty()
        coordinator._drain_completions()
        assert coordinator.completion_q.empty()

        handler(signal_mod.SIGTERM, None)
        assert coordinator._immediate is True
        assert not coordinator.completion_q.empty()


def _classify_from_fake(
    gh: FakeStageGitHub,
    issue: int,
    *,
    open_pr: int | None = None,
    merged_pr: int | None = None,
    is_epic: bool = False,
) -> Any:
    """Re-run the seeding classifier against the FakeGitHub label journal."""
    facts = IssueFacts(
        number=issue,
        title="Epic: a task" if is_epic else "a task",
        is_epic=is_epic,
        labels=set(gh.labels.get(issue, set())),
        pr_number=open_pr if open_pr is not None else merged_pr,
        pr_is_open=open_pr is not None,
        pr_is_merged=merged_pr is not None,
    )
    stage, _reason = classify_issue(facts)
    return stage


def _order(stage: StageName) -> int:
    return PIPELINE_ORDER.index(stage)


class TestCrashMatrixJournal:
    """Truncate after each durable mutation -> re-seed -> same-or-earlier stage."""

    @pytest.mark.parametrize(
        ("case", "labels", "open_pr", "merged_pr", "is_epic", "expected"),
        [
            ("no label", [], None, None, False, StageName.PLANNING),
            ("needs-plan label", [STATE_NEEDS_PLAN], None, None, False, StageName.PLANNING),
            ("plan-no-go label", [STATE_PLAN_NO_GO], None, None, False, StageName.PLANNING),
            ("plan-go label", [STATE_PLAN_GO], None, None, False, StageName.IMPLEMENTATION),
            (
                "open PR without implementation-go",
                [STATE_IMPLEMENTATION_NO_GO],
                77,
                None,
                False,
                StageName.PR_REVIEW,
            ),
            (
                # Issue-level implementation-go on an open PR is a legacy
                # compatibility label (#2140): post-#2280 the durable
                # authorization is the PR-level label, so this routes back to
                # review rather than arming a merge.
                "open PR with legacy issue-level implementation-go",
                [STATE_IMPLEMENTATION_GO],
                78,
                None,
                False,
                StageName.PR_REVIEW,
            ),
            ("merged PR", [], None, 79, False, StageName.FINISHED),
            ("state:skip", [STATE_SKIP], None, None, False, None),
            ("untagged epic", [], None, None, True, None),
        ],
    )
    def test_reconstruction_table_covers_every_github_journal_row(
        self,
        case: str,
        labels: list[str],
        open_pr: int | None,
        merged_pr: int | None,
        is_epic: bool,
        expected: StageName | None,
    ) -> None:
        """Every architecture-doc reconstruction row maps to exactly one entry queue."""
        gh = FakeStageGitHub()
        issue = 90
        if labels:
            gh.add_labels(issue, labels)

        entry = _classify_from_fake(
            gh,
            issue,
            open_pr=open_pr,
            merged_pr=merged_pr,
            is_epic=is_epic,
        )

        assert entry is expected, case

    def _ctx(self, gh: FakeStageGitHub) -> StageContext:
        from tests.unit.automation.pipeline.stages.conftest import _budget_fn, _Config, _Paths

        return StageContext(
            config=_Config(),
            org="org",
            dry_run=False,
            github=gh,
            paths=_Paths(),
            budget_fn=_budget_fn,
        )

    def _drive_plan_review_go_label(self, gh: FakeStageGitHub, issue: int) -> None:
        """Drive plan_review's real GO path until it writes state:plan-go."""
        stage = PlanReviewStage()
        ctx = self._ctx(gh)
        ctx.config.enable_learn = False
        item = WorkItem(
            repo="repo-a",
            kind=ItemKind.ISSUE,
            issue=issue,
            stage=StageName.PLAN_REVIEW,
            state="ENTER",
        )
        item.payload["issue_title"] = "A task"
        item.payload["issue_body"] = "Body"
        item.payload["plan_text"] = "# Implementation Plan\n\nDo the work."

        assert stage.on_enter(item, ctx) is None
        enter = stage.step(item, ctx)
        assert isinstance(enter, Continue)
        item.state = enter.next_state

        request = stage.step(item, ctx)
        assert isinstance(request, JobRequest)
        stage.on_job_done(item, JobResult(ok=True, value=_verdict("GO")), ctx)
        item.state = request.on_done_state

        outcome = stage.step(item, ctx)
        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition.value == "advance"
        assert ("gh_issue_add_labels", (issue, (STATE_PLAN_GO,))) in gh.mutation_log

    def test_crash_after_needs_plan_label_reenters_planning(self) -> None:
        """S1: crash right after planning's entry-label write -> planning again."""
        gh = FakeStageGitHub()
        stage = PlanningStage()
        item = WorkItem(
            repo="repo-a", kind=ItemKind.ISSUE, issue=1, stage=StageName.PLANNING, state="ENTER"
        )

        assert stage.on_enter(item, self._ctx(gh)) is None  # writes state:needs-plan
        # CRASH: discard the item entirely; only the GitHub journal survives.
        entry = _classify_from_fake(gh, 1)

        assert entry is StageName.PLANNING  # same stage — never lost
        assert ("gh_issue_add_labels", (1, ("state:needs-plan",))) in gh.mutation_log

    def test_crash_after_plan_comment_upsert_stays_at_or_before_plan_review(self) -> None:
        """S2: crash after the durable plan-comment upsert -> same-or-earlier."""
        gh = FakeStageGitHub()
        stage = PlanningStage()
        ctx = self._ctx(gh)
        item = WorkItem(
            repo="repo-a", kind=ItemKind.ISSUE, issue=2, stage=StageName.PLANNING, state="ENTER"
        )
        assert stage.on_enter(item, ctx) is None
        item.state = "VERIFY"
        item.payload["plan_text"] = "# Implementation Plan\n\ndo things"

        outcome = stage.step(item, ctx)  # upserts the plan comment, then ADVANCE
        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition.value == "advance"
        # CRASH before the queue push to plan_review.
        entry = _classify_from_fake(gh, 2)

        # Labels still say needs-plan: re-entry lands at planning (earlier
        # than the lost plan_review push — never lost, never duplicated),
        # and planning's on_enter fast-forwards past the existing comment.
        assert _order(entry) <= _order(StageName.PLAN_REVIEW)
        assert any(name == "gh_issue_upsert_comment" for name, _ in gh.mutation_log)

    def test_crash_after_plan_go_label_reenters_implementation(self) -> None:
        """S3: crash after plan_review's GO label write -> implementation."""
        gh = FakeStageGitHub()
        self._drive_plan_review_go_label(gh, 3)

        # CRASH before the push to the implementation queue.
        entry = _classify_from_fake(gh, 3)

        assert entry is StageName.IMPLEMENTATION  # exactly the lost push target
        # Exactly one classification — never duplicated across queues.
        assert isinstance(entry, StageName)

    def test_crash_after_pr_creation_reenters_pr_review(self) -> None:
        """S4: with an open PR journaled, re-seeding enters pr_review (not earlier)."""
        gh = FakeStageGitHub()
        self._drive_plan_review_go_label(gh, 4)

        stage = ImplementationStage()
        item = WorkItem(
            repo="repo-a",
            kind=ItemKind.ISSUE,
            issue=4,
            stage=StageName.IMPLEMENTATION,
            state="PR_CREATE",
        )
        item.branch = "4-auto-impl"
        item.payload["issue_title"] = "A task"
        item.payload["implement_summary"] = "Implemented issue #4."

        outcome = stage.step(item, self._ctx(gh))

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition.value == "advance"
        assert item.pr is not None
        assert item.pr in gh.prs
        assert [name for name, _ in gh.mutation_log[-2:]] == [
            "gh_pr_create",
            "defer_auto_merge",
        ]

        # CRASH before the push to the pr_review queue.
        entry = _classify_from_fake(gh, 4, open_pr=item.pr)

        assert entry is StageName.PR_REVIEW
