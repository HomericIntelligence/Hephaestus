"""Tests for the fail-closed merge-wait bootstrap stage (#2054)."""

from __future__ import annotations

from typing import Any

from hephaestus.automation.pipeline.jobs import AgentJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.merge_wait import (
    ARM,
    ENTER,
    LEARN_WAIT,
    MW_FINISH,
    POLL,
    MergeWaitStage,
    build_drive_green_learn_prompt,
)
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

MERGED_STATE = {"state": "MERGED", "headRefOid": "abc123"}
CLOSED_STATE = {"state": "CLOSED"}
OPEN_STATE = {"state": "OPEN", "mergeStateStatus": "BEHIND", "headRefOid": "abc123"}


class _MarkFailGitHub(FakeStageGitHub):
    """mark_drive_green_learn_result raises (learn-record write failed)."""

    def mark_drive_green_learn_result(self, issue_number: int, *, succeeded: bool) -> None:
        raise OSError("disk full")


def _drive(stage: Any, item: Any, ctx: Any, pool: FakeWorkerPool, max_steps: int = 20) -> Any:
    """Drive one stage through the canonical fake worker pool."""
    entry = stage.on_enter(item, ctx)
    if entry is not None:
        return entry
    for _ in range(max_steps):
        result = stage.step(item, ctx)
        if isinstance(result, Continue):
            item.state = result.next_state
            continue
        if isinstance(result, JobRequest):
            pool.submit(result.job, result.on_done_state)  # type: ignore[arg-type]
            _handle, job_result = pool.completion_q.get_nowait()
            stage.on_job_done(item, job_result, ctx)
            item.state = result.on_done_state
            continue
        return result
    raise AssertionError("stage driver did not terminate")


def _item(make_work_item: Any, **kwargs: Any) -> Any:
    """Build a merge-wait work item with a valid PR and worktree."""
    kwargs.setdefault("stage", StageName.MERGE_WAIT)
    kwargs.setdefault("pr", 601)
    item = make_work_item(**kwargs)
    item.branch = "1-auto-impl"
    item.worktree = "/tmp/wt/1"
    return item


def _poll_item(make_work_item: Any, **kwargs: Any) -> Any:
    """Build a persisted legacy POLL item."""
    kwargs.setdefault("state", POLL)
    item = _item(make_work_item, **kwargs)
    item.armed = True
    item.payload["merge_wait_started_at"] = 1000.0
    return item


class TestMergeWaitContainment:
    """Every open PR state is terminalized after verified deferral."""

    def test_arm_disables_and_stops_until_the_strict_gate_exists(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state=OPEN_STATE)
        ctx = make_ctx(github=github)
        item = _item(make_work_item, state=ARM)

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.FINISH_FAIL, "strict_gate_unavailable"
        )
        assert github.mutation_log == [("defer_auto_merge", (601,))]
        assert item.armed is False

    def test_persisted_poll_disables_and_stops_without_an_anchor(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A restart cannot bypass containment by restoring POLL directly."""
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state=OPEN_STATE)
        ctx = make_ctx(github=github)
        item = _poll_item(make_work_item)
        item.payload.pop("merge_wait_started_at")

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.FINISH_FAIL, "strict_gate_unavailable"
        )
        assert github.mutation_log == [("defer_auto_merge", (601,))]
        assert item.armed is False

    def test_disable_verification_failure_is_terminal(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        class DeferFailsGitHub(FakeStageGitHub):
            def defer_auto_merge(self, pr_number: int) -> None:
                raise RuntimeError("auto-merge remains enabled")

        stage = MergeWaitStage()
        ctx = make_ctx(github=DeferFailsGitHub(pr_state=OPEN_STATE))
        item = _item(make_work_item, state=ARM)

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.FINISH_FAIL, "auto_merge_disable_failed"
        )

    def test_restored_open_poll_fails_when_auto_merge_deferral_cannot_be_verified(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A persisted POLL state also fails closed on an unsuccessful read-back."""

        class DeferFailsGitHub(FakeStageGitHub):
            def defer_auto_merge(self, pr_number: int) -> None:
                raise RuntimeError(f"PR #{pr_number} remains armed")

        stage = MergeWaitStage()
        ctx = make_ctx(github=DeferFailsGitHub(pr_state=OPEN_STATE))
        item = _poll_item(make_work_item)

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.FINISH_FAIL, "auto_merge_disable_failed"
        )

    def test_dry_run_stops_without_mutating(self, make_ctx: Any, make_work_item: Any) -> None:
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state=OPEN_STATE)
        ctx = make_ctx(github=github, dry_run=True)
        item = _item(make_work_item, state=ARM)

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.FINISH_FAIL, "strict_gate_unavailable"
        )
        assert github.mutation_log == [("defer_auto_merge", (601,))]

    def test_arm_without_pr_finishes_no_pr(self, make_ctx: Any, make_work_item: Any) -> None:
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub())
        item = _item(make_work_item, pr=None, state=ARM)

        assert stage.step(item, ctx) == StageOutcome(Disposition.FINISH_FAIL, "no_pr")

    def test_on_enter_initializes_enter_and_unknown_state_fails(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub())
        item = _item(make_work_item, state="")

        assert stage.on_enter(item, ctx) is None
        assert item.state == ENTER
        assert stage.step(item, ctx) == Continue(next_state=ARM)
        item.state = "BOGUS"
        outcome = stage.step(item, ctx)
        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.FINISH_FAIL


class TestMergedPrLearn:
    """The only active polling behavior is post-merge learn capture."""

    def test_merged_pr_reaches_existing_learn_path(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state=MERGED_STATE)
        ctx = make_ctx(github=github)
        pool = FakeWorkerPool()

        assert _drive(stage, _item(make_work_item, state=""), ctx, pool) == StageOutcome(
            Disposition.FINISH_PASS, "merged"
        )
        assert github.mutation_log == [("mark_drive_green_learn_result", (1, True))]

    def test_closed_poll_finishes_failed(self, make_ctx: Any, make_work_item: Any) -> None:
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub(pr_state=CLOSED_STATE))

        assert stage.step(_poll_item(make_work_item), ctx) == StageOutcome(
            Disposition.FINISH_FAIL, "closed"
        )

    def test_learn_dedupe_and_disabled_config_skip_agent(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state=MERGED_STATE)
        ctx = make_ctx(github=github)
        pool = FakeWorkerPool()
        first = _poll_item(make_work_item)
        assert _drive(stage, first, ctx, pool) == StageOutcome(Disposition.FINISH_PASS, "merged")
        replay = _poll_item(make_work_item)
        assert _drive(stage, replay, ctx, pool) == StageOutcome(Disposition.FINISH_PASS, "merged")
        assert len(pool.submitted) == 1

        disabled_ctx = make_ctx(github=FakeStageGitHub(pr_state=MERGED_STATE))
        disabled_ctx.config.enable_learn = False
        disabled_pool = FakeWorkerPool()
        assert _drive(
            stage, _poll_item(make_work_item), disabled_ctx, disabled_pool
        ) == StageOutcome(Disposition.FINISH_PASS, "merged")
        assert disabled_pool.submitted == []

    def test_failed_learn_and_failed_mark_never_fail_a_merged_pr(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state=MERGED_STATE)
        ctx = make_ctx(github=github)
        pool = FakeWorkerPool()
        pool.script(JobResult(ok=False, error="learn agent crashed"))
        assert _drive(stage, _poll_item(make_work_item), ctx, pool) == StageOutcome(
            Disposition.FINISH_PASS, "merged"
        )
        assert github.learn_results[1] is False

        failing_ctx = make_ctx(github=_MarkFailGitHub(pr_state=MERGED_STATE))
        assert _drive(
            stage, _poll_item(make_work_item), failing_ctx, FakeWorkerPool()
        ) == StageOutcome(Disposition.FINISH_PASS, "merged")

    def test_learn_job_and_finish_state(self, make_ctx: Any, make_work_item: Any) -> None:
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub())
        item = _poll_item(make_work_item, state=LEARN_WAIT)

        request = stage.step(item, ctx)
        assert isinstance(request, JobRequest)
        assert request.on_done_state == MW_FINISH
        assert isinstance(request.job, AgentJob)
        assert "PR #601" in request.job.prompt_builder(**request.job.prompt_kwargs)
        item.state = MW_FINISH
        assert stage.step(item, ctx) == StageOutcome(Disposition.FINISH_PASS, "merged")

    def test_learn_prompt_renders(self) -> None:
        prompt = build_drive_green_learn_prompt(issue_number=9, pr_number=99)
        assert "/learn" in prompt
        assert "PR #99" in prompt
