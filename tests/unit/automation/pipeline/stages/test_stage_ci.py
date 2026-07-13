"""Tests for the CI drive-green stage (doc section "6. ci", issue #1816)."""

from __future__ import annotations

from typing import Any

from hephaestus.automation.pipeline.jobs import AgentJob, GitJob, JobResult
from hephaestus.automation.pipeline.routing import ROUTES, Disposition, StageName
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.ci import (
    BACKOFF_CAP_S,
    CI_POLL_STARTED_AT,
    DISCOVER,
    ENTER,
    FIX_WAIT,
    POLL,
    POLL_DEADLINE,
    PUSH_WAIT,
    REBASE_PUSH_WAIT,
    REBASE_WAIT,
    CiStage,
    build_ci_fix_prompt,
    build_force_engagement_prompt,
)
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

# Sanity anchors: the reasons this suite exercises are ROUTES rows.
assert ROUTES[StageName.CI].fail_routes["fix_exhausted"] == StageName.IMPLEMENTATION
assert ROUTES[StageName.CI].fail_routes["not_implementation_go"] == StageName.PR_REVIEW
assert ROUTES[StageName.CI].budgets == {"ci_fix": 1, "rebase": 2}

GREEN_CHECKS = [
    {"status": "completed", "conclusion": "success", "required": True},
    {"status": "completed", "conclusion": "skipped", "required": True},
]
FAILING_CHECKS = [
    {"name": "lint", "status": "completed", "conclusion": "failure", "required": True},
    {"status": "completed", "conclusion": "success", "required": True},
]
PENDING_CHECKS = [
    {"status": "in_progress", "conclusion": None, "required": True},
]
# Legacy residual class: concluded, no "failure", but NOT all_green — the
# tightened classifier routes it to the fix leg (never arms it).
CANCELLED_CHECKS = [
    {"status": "completed", "conclusion": "cancelled", "required": True},
]


class _FifoChecksGitHub(FakeStageGitHub):
    """FakeStageGitHub whose pr_checks pops a scripted FIFO (last repeats)."""

    def __init__(self, checks_fifo: list[list[dict[str, Any]]], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._checks_fifo = list(checks_fifo)

    def pr_checks(self, pr_number: int) -> list[dict[str, Any]]:
        del pr_number
        if len(self._checks_fifo) > 1:
            return self._checks_fifo.pop(0)
        return list(self._checks_fifo[0])


def _drive(stage: Any, item: Any, ctx: Any, pool: FakeWorkerPool, max_steps: int = 80) -> Any:
    """Drive a stage through the canonical FakeWorkerPool until an outcome."""
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
            assert not job_result.interrupted  # on_job_done contract precondition
            stage.on_job_done(item, job_result, ctx)
            item.state = result.on_done_state
            continue
        return result
    raise AssertionError("stage driver did not terminate")


def _go_github(**kwargs: Any) -> FakeStageGitHub:
    """FakeStageGitHub for a PR that already carries implementation-go."""
    kwargs.setdefault("pr_impl_state", (True, False))
    return FakeStageGitHub(**kwargs)


def _item(make_work_item: Any, **kwargs: Any) -> Any:
    kwargs.setdefault("stage", StageName.CI)
    kwargs.setdefault("pr", 501)
    item = make_work_item(**kwargs)
    item.branch = item.branch or "1-auto-impl"
    item.worktree = "/tmp/wt/1"
    return item


class TestCiOnEnterAndStates:
    """Entry initialization and the state dispatcher."""

    def test_on_enter_initializes_empty_state(self, make_ctx: Any, make_work_item: Any) -> None:
        """An empty state is initialized to ENTER; nothing durable is written."""
        stage = CiStage()
        github = _go_github()
        ctx = make_ctx(github=github)
        item = _item(make_work_item, state="")

        assert stage.on_enter(item, ctx) is None
        assert item.state == ENTER
        assert github.mutation_log == []

    def test_on_enter_preserves_existing_state(self, make_ctx: Any, make_work_item: Any) -> None:
        """A restart in a later mini-state is never rewound (restart = re-run)."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github())
        item = _item(make_work_item, state=POLL)

        assert stage.on_enter(item, ctx) is None
        assert item.state == POLL

    def test_enter_advances_to_discover(self, make_ctx: Any, make_work_item: Any) -> None:
        """ENTER routes to DISCOVER."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github())
        item = _item(make_work_item, state=ENTER)

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == DISCOVER

    def test_unknown_state_finishes_failed(self, make_ctx: Any, make_work_item: Any) -> None:
        """An unknown mini-state is a hard stop, not a silent spin."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github())
        item = _item(make_work_item, state="BOGUS")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL
        assert "unknown state" in result.note


class TestCiDiscover:
    """DISCOVER: PR resolution + the implementation-go verify."""

    def test_no_pr_and_no_issue_finishes_no_pr(self, make_ctx: Any, make_work_item: Any) -> None:
        """Neither PR nor issue: nothing to drive."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github())
        item = _item(make_work_item, issue=None, pr=None, state=DISCOVER)

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "no_pr")

    def test_no_open_pr_found_finishes_no_pr(self, make_ctx: Any, make_work_item: Any) -> None:
        """Discovery finding no open PR finishes failed (doc fail route)."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github(open_pr=None))
        item = _item(make_work_item, pr=None, state=DISCOVER)

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "no_pr")

    def test_discovery_adopts_pr_and_real_head_branch(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A discovered PR sets item.pr and adopts the PR's REAL head branch."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github(open_pr=777, pr_head_branch="real-head"))
        item = _item(make_work_item, pr=None, state=DISCOVER)
        item.branch = ""

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == REBASE_WAIT
        assert item.pr == 777
        assert item.branch == "real-head"

    def test_discovery_captures_pr_base_branch_from_baseref(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A PR based on a non-main branch seeds payload["base_branch"] from baseRefName."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github(pr_state={"state": "OPEN", "baseRefName": "release/2.0"}))
        item = _item(make_work_item, state=DISCOVER)

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == REBASE_WAIT
        assert item.payload["base_branch"] == "release/2.0"

    def test_discovery_defaults_base_branch_to_main_without_baseref(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """No gh_pr_state / missing baseRefName still defaults to "main" (unchanged)."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github())  # pr_state defaults to None
        item = _item(make_work_item, state=DISCOVER)

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert item.payload["base_branch"] == "main"

    def test_discovery_fails_closed_when_auto_merge_deferral_cannot_be_verified(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A direct CI ingress cannot proceed after a failed containment read-back."""

        class DeferFailsGitHub(FakeStageGitHub):
            def defer_auto_merge(self, pr_number: int) -> None:
                raise RuntimeError(f"PR #{pr_number} remains armed")

        stage = CiStage()
        ctx = make_ctx(github=DeferFailsGitHub(open_pr=777))
        item = _item(make_work_item, pr=None, state=DISCOVER)

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.FINISH_FAIL, "auto_merge_disable_failed"
        )

    def test_already_merged_pr_finishes_before_branch_adoption(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A PR merged after review arming must not adopt a deleted head branch."""

        class MergedGitHub(FakeStageGitHub):
            def get_pr_head_branch(self, pr_number: int) -> str | None:
                raise AssertionError("merged PRs should finish before branch adoption")

            def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
                raise AssertionError("merged PRs should finish before label routing")

        stage = CiStage()
        ctx = make_ctx(github=MergedGitHub(pr_state={"state": "MERGED"}))
        item = _item(make_work_item, state=DISCOVER)
        item.branch = ""

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_PASS, "merged")

    def test_already_closed_pr_finishes_before_branch_adoption(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A PR closed after review arming must not adopt a deleted head branch."""

        class ClosedGitHub(FakeStageGitHub):
            def get_pr_head_branch(self, pr_number: int) -> str | None:
                raise AssertionError("closed PRs should finish before branch adoption")

            def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
                raise AssertionError("closed PRs should finish before label routing")

        stage = CiStage()
        ctx = make_ctx(github=ClosedGitHub(pr_state={"state": "CLOSED"}))
        item = _item(make_work_item, state=DISCOVER)
        item.branch = ""

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "closed")

    def test_missing_implementation_go_fails_back(self, make_ctx: Any, make_work_item: Any) -> None:
        """A PR without state:implementation-go regresses to pr_review."""
        stage = CiStage()
        ctx = make_ctx(github=FakeStageGitHub(pr_impl_state=(False, False)))
        item = _item(make_work_item, state=DISCOVER)

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")

    def test_preset_branch_is_kept(self, make_ctx: Any, make_work_item: Any) -> None:
        """An item that already knows its branch never overwrites it."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github(pr_head_branch="other"))
        item = _item(make_work_item, state=DISCOVER)
        item.branch = "preset-branch"

        stage.step(item, ctx)

        assert item.branch == "preset-branch"


class TestCiRebase:
    """REBASE_WAIT: best-effort mechanical rebase gating."""

    def test_dry_run_skips_rebase(self, make_ctx: Any, make_work_item: Any) -> None:
        """Dry-run never dispatches git work (legacy _drive_issue gate)."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github(), dry_run=True)
        item = _item(make_work_item, state=REBASE_WAIT)

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == POLL

    def test_option_off_skips_rebase(self, make_ctx: Any, make_work_item: Any) -> None:
        """enable_mechanical_rebase=False skips the rebase leg."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github())
        ctx.config.enable_mechanical_rebase = False
        item = _item(make_work_item, state=REBASE_WAIT)

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == POLL

    def test_budget_spent_skips_rebase(self, make_ctx: Any, make_work_item: Any) -> None:
        """A spent rebase budget polls as-is instead of dispatching."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github())
        item = _item(make_work_item, state=REBASE_WAIT)
        item.attempts["rebase"] = 2

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == POLL

    def test_rebase_job_dispatched_and_budget_counted_on_done(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The rebase GitJob targets the base branch; on_job_done counts it."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github())
        item = _item(make_work_item, state=REBASE_WAIT)
        item.worktree = "/tmp/wt/issue-1"
        item.payload["base_branch"] = "develop"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "rebase"
        assert result.job.kwargs["base_branch"] == "develop"
        assert result.on_done_state == REBASE_PUSH_WAIT
        assert item.attempts["rebase"] == 0  # not consumed at submission

        stage.on_job_done(item, JobResult(ok=True, value=True), ctx)
        assert item.attempts["rebase"] == 1  # consumed on completion
        assert item.payload["rebase_clean"] is True

    def test_rebase_without_item_worktree_fails_back_before_git_job(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """CI must not mechanically rebase the loop driver's shared checkout."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github())
        item = _item(make_work_item, state=REBASE_WAIT)
        item.worktree = ""

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FAIL_BACK, "missing_worktree")

    def test_failed_rebase_is_best_effort(self, make_ctx: Any, make_work_item: Any) -> None:
        """A failed rebase counts the budget, records not-clean, no routing flag."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github())
        item = _item(make_work_item, state=REBASE_WAIT)

        stage.on_job_done(item, JobResult(ok=False, error="conflict"), ctx)

        assert item.attempts["rebase"] == 1
        assert item.payload["rebase_clean"] is False
        assert "push_failed" not in item.payload
        assert "ci_fix_failed" not in item.payload


class TestCiRebasePush:
    """REBASE_PUSH_WAIT: push a clean rebase, or poll a conflicted one."""

    def test_clean_rebase_pushes_then_polls(self, make_ctx: Any, make_work_item: Any) -> None:
        """A clean rebase dispatches an op="push" GitJob targeting item.branch."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github())
        item = _item(make_work_item, state=REBASE_PUSH_WAIT)
        item.payload["rebase_clean"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "push"
        assert result.job.kwargs["branch"] == item.branch
        assert result.on_done_state == POLL
        assert "rebase_clean" not in item.payload

    def test_conflicting_rebase_never_pushes(self, make_ctx: Any, make_work_item: Any) -> None:
        """A not-clean rebase has nothing to push and re-polls immediately."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github())
        item = _item(make_work_item, state=REBASE_PUSH_WAIT)
        item.payload["rebase_clean"] = False

        result = stage.step(item, ctx)

        assert result == Continue(next_state=POLL)

    def test_push_without_item_worktree_fails_back_before_git_job(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """CI must not push from the loop driver's shared checkout."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github())
        item = _item(make_work_item, state=REBASE_PUSH_WAIT)
        item.payload["rebase_clean"] = True
        item.worktree = ""

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FAIL_BACK, "missing_worktree")


class TestCiPoll:
    """POLL: non-blocking classify -> RETRY / ADVANCE / fix routing."""

    def test_pending_parks_with_payload_delay(self, make_ctx: Any, make_work_item: Any) -> None:
        """PENDING returns RETRY and records the backoff delay in the payload.

        StageOutcome has no delay field — the coordinator (#1817) consumes
        payload["retry_delay_s"] (the base.py timer-park contract).
        """
        stage = CiStage()
        ctx = make_ctx(github=_go_github(checks=PENDING_CHECKS))
        item = _item(make_work_item, state=POLL)

        for expected_delay in (1, 2, 4, 8):
            result = stage.step(item, ctx)
            assert result == StageOutcome(Disposition.RETRY, "ci_pending")
            assert item.payload["retry_delay_s"] == expected_delay
        assert item.payload["ci_poll_count"] == 4
        assert item.payload[CI_POLL_STARTED_AT] > 0

    def test_wall_clock_deadline_times_out(self, make_ctx: Any, make_work_item: Any) -> None:
        """Elapsed wall-clock over poll_max_wait finishes timeout."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github(checks=PENDING_CHECKS))
        ctx.config.poll_max_wait = 10
        item = _item(make_work_item, state=POLL)
        item.payload[CI_POLL_STARTED_AT] = 0.0

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "timeout")

    def test_pending_uses_wall_clock_not_durable_poll_count(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A restart with many prior polls keeps waiting until wall-clock expires."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github(checks=PENDING_CHECKS))
        item = _item(make_work_item, state=POLL)
        item.payload[CI_POLL_STARTED_AT] = 1000.0
        item.payload["ci_poll_count"] = POLL_DEADLINE

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.RETRY, "ci_pending")
        assert item.payload["retry_delay_s"] == BACKOFF_CAP_S
        assert item.payload["ci_poll_count"] == POLL_DEADLINE + 1

    def test_green_advances(self, make_ctx: Any, make_work_item: Any) -> None:
        """GREEN checks ADVANCE to merge_wait."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github(checks=GREEN_CHECKS))
        item = _item(make_work_item, state=POLL)

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE

    def test_no_checks_advances(self, make_ctx: Any, make_work_item: Any) -> None:
        """No CI configured is the legacy success case."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github(checks=[]))
        item = _item(make_work_item, state=POLL)

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE

    def test_failing_is_never_treated_as_green(self, make_ctx: Any, make_work_item: Any) -> None:
        """FAILING enters the fix leg — mutation probe (a): FAILING != GREEN."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github(checks=FAILING_CHECKS))
        item = _item(make_work_item, state=POLL)

        result = stage.step(item, ctx)

        assert not (isinstance(result, StageOutcome) and result.disposition == Disposition.ADVANCE)
        assert result == Continue(next_state=FIX_WAIT)

    def test_cancelled_residual_is_failing_not_green(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The legacy non-all_green residual (cancelled) must never arm."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github(checks=CANCELLED_CHECKS))
        item = _item(make_work_item, state=POLL)

        result = stage.step(item, ctx)

        assert result == Continue(next_state=FIX_WAIT)

    def test_failing_with_spent_budget_fails_back(self, make_ctx: Any, make_work_item: Any) -> None:
        """FAILING past the ci_fix budget fails back fix_exhausted."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github(checks=FAILING_CHECKS))
        item = _item(make_work_item, state=POLL)
        item.attempts["ci_fix"] = 1

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FAIL_BACK, "fix_exhausted")

    def test_push_failed_flag_fails_back(self, make_ctx: Any, make_work_item: Any) -> None:
        """A hard push failure means the head never advanced: fail back."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github(checks=GREEN_CHECKS))
        item = _item(make_work_item, state=POLL)
        item.payload["push_failed"] = True

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FAIL_BACK, "fix_exhausted")
        assert "push_failed" not in item.payload  # consumed

    def test_no_commit_triggers_one_force_engagement(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The FIRST no-commit turn escalates once; the second exhausts (#846)."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github(checks=FAILING_CHECKS))
        item = _item(make_work_item, state=POLL)

        item.payload["push_no_commit"] = True
        first = stage.step(item, ctx)
        assert first == Continue(next_state=FIX_WAIT)
        assert item.payload["force_engagement"] is True
        assert item.payload["force_engagement_done"] is True

        item.payload["push_no_commit"] = True
        second = stage.step(item, ctx)
        assert second == StageOutcome(Disposition.FAIL_BACK, "fix_exhausted")

    def test_poll_without_pr_finishes_no_pr(self, make_ctx: Any, make_work_item: Any) -> None:
        """Restart safety: POLL with no PR finishes failed."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github())
        item = _item(make_work_item, pr=None, state=POLL)

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "no_pr")


class TestCiWalks:
    """Pool-driven walks through the full state machine."""

    def test_green_path_advances_after_rebase(self, make_ctx: Any, make_work_item: Any) -> None:
        """ENTER -> DISCOVER -> rebase job -> push job -> POLL green -> ADVANCE."""
        stage = CiStage()
        github = _go_github(checks=GREEN_CHECKS)
        ctx = make_ctx(github=github)
        pool = FakeWorkerPool()
        item = _item(make_work_item, state="")

        outcome = _drive(stage, item, ctx, pool)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.ADVANCE
        git_jobs = [h.job for h in pool.submitted if isinstance(h.job, GitJob)]
        assert [j.op for j in git_jobs] == ["rebase", "push"]
        assert item.attempts["rebase"] == 1
        assert github.mutation_log == [("defer_auto_merge", (501,))]

    def test_fix_path_reaches_green(self, make_ctx: Any, make_work_item: Any) -> None:
        """FAILING -> fix agent -> push (real commit) -> re-poll green -> ADVANCE."""
        stage = CiStage()
        github = _FifoChecksGitHub([FAILING_CHECKS, GREEN_CHECKS], pr_impl_state=(True, False))
        ctx = make_ctx(github=github)
        ctx.config.enable_mechanical_rebase = False
        pool = FakeWorkerPool()
        pool.script(
            JobResult(ok=True, value="fixed"),  # ci_fix agent session
            JobResult(ok=True, value=True),  # push: real commit
        )
        item = _item(make_work_item, state="")
        item.payload["ci_poll_count"] = 3  # pre-existing backoff window

        outcome = _drive(stage, item, ctx, pool)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.ADVANCE
        fix_job, push_job = (h.job for h in pool.submitted)
        assert isinstance(fix_job, AgentJob)
        assert fix_job.descr == "ci_fix"
        assert fix_job.prompt_builder is build_ci_fix_prompt
        assert isinstance(push_job, GitJob)
        assert push_job.op == "commit_push"
        assert item.attempts["ci_fix"] == 1
        # A real pushed commit restarts the poll backoff window.
        assert "ci_poll_count" not in item.payload

    def test_fix_prompt_kwargs_render(self, make_ctx: Any, make_work_item: Any) -> None:
        """The submitted fix job's kwargs compose a real prompt (kwargs-verified)."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github())
        item = _item(make_work_item, state=FIX_WAIT)
        item.payload.update(
            {
                "ci_logs": "boom-log",
                "failing_check_names": ["lint"],
                "advise_findings": "prior wisdom",
                "review_threads_block": "THREADS\n",
            }
        )

        request = stage.step(item, ctx)

        assert isinstance(request, JobRequest)
        assert isinstance(request.job, AgentJob)
        prompt = request.job.prompt_builder(**request.job.prompt_kwargs)
        assert "boom-log" in prompt
        assert "lint" in prompt
        assert "prior wisdom" in prompt
        assert item.branch in prompt

    def test_fix_budget_exhaustion_fails_back(self, make_ctx: Any, make_work_item: Any) -> None:
        """A fix round that leaves CI red exhausts ci_fix=1 -> implementation."""
        stage = CiStage()
        github = _FifoChecksGitHub([FAILING_CHECKS], pr_impl_state=(True, False))
        ctx = make_ctx(github=github)
        ctx.config.enable_mechanical_rebase = False
        pool = FakeWorkerPool()
        pool.script(
            JobResult(ok=True, value="fixed"),  # ci_fix
            JobResult(ok=True, value=True),  # push: real commit
        )
        item = _item(make_work_item, state="")

        outcome = _drive(stage, item, ctx, pool)

        assert outcome == StageOutcome(Disposition.FAIL_BACK, "fix_exhausted")
        assert item.attempts["ci_fix"] == 1

    def test_failed_fix_job_never_pushes(self, make_ctx: Any, make_work_item: Any) -> None:
        """A hard-failed fix session reroutes to POLL without a push job."""
        stage = CiStage()
        github = _FifoChecksGitHub([FAILING_CHECKS], pr_impl_state=(True, False))
        ctx = make_ctx(github=github)
        ctx.config.enable_mechanical_rebase = False
        pool = FakeWorkerPool()
        pool.script(JobResult(ok=False, error="agent crashed"))

        item = _item(make_work_item, state="")
        outcome = _drive(stage, item, ctx, pool)

        assert outcome == StageOutcome(Disposition.FAIL_BACK, "fix_exhausted")
        assert len(pool.submitted) == 1  # the agent job only — no push
        assert isinstance(pool.submitted[0].job, AgentJob)

    def test_hard_push_failure_fails_back(self, make_ctx: Any, make_work_item: Any) -> None:
        """A lost-lease/broken-remote push is exhaustion (head never advanced)."""
        stage = CiStage()
        github = _FifoChecksGitHub([FAILING_CHECKS], pr_impl_state=(True, False))
        ctx = make_ctx(github=github)
        ctx.config.enable_mechanical_rebase = False
        pool = FakeWorkerPool()
        pool.script(
            JobResult(ok=True, value="fixed"),
            JobResult(ok=False, error="lease lost"),
        )

        item = _item(make_work_item, state="")
        outcome = _drive(stage, item, ctx, pool)

        assert outcome == StageOutcome(Disposition.FAIL_BACK, "fix_exhausted")

    def test_no_commit_escalation_walk(self, make_ctx: Any, make_work_item: Any) -> None:
        """No-commit push -> ONE force-engagement retry -> green -> ADVANCE."""
        stage = CiStage()
        github = _FifoChecksGitHub([FAILING_CHECKS, GREEN_CHECKS], pr_impl_state=(True, False))
        ctx = make_ctx(github=github)
        ctx.config.enable_mechanical_rebase = False
        pool = FakeWorkerPool()
        pool.script(
            JobResult(ok=True, value="claims fixed"),  # ci_fix
            JobResult(ok=True, value=False),  # push: NO commit
            JobResult(ok=True, value="really fixed"),  # force-engagement retry
            JobResult(ok=True, value=True),  # push: real commit
        )

        item = _item(make_work_item, state="")
        item.payload["failing_check_names"] = ["lint"]
        outcome = _drive(stage, item, ctx, pool)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.ADVANCE
        agent_jobs = [h.job for h in pool.submitted if isinstance(h.job, AgentJob)]
        assert [j.descr for j in agent_jobs] == ["ci_fix", "force_engagement"]
        assert agent_jobs[1].prompt_builder is build_force_engagement_prompt
        prompt = agent_jobs[1].prompt_builder(**agent_jobs[1].prompt_kwargs)
        assert "Force-Engagement Retry" in prompt
        assert "lint" in prompt

    def test_double_no_commit_exhausts(self, make_ctx: Any, make_work_item: Any) -> None:
        """A second consecutive no-commit turn fails back (escalation is ONE)."""
        stage = CiStage()
        github = _FifoChecksGitHub([FAILING_CHECKS], pr_impl_state=(True, False))
        ctx = make_ctx(github=github)
        ctx.config.enable_mechanical_rebase = False
        pool = FakeWorkerPool()
        pool.script(
            JobResult(ok=True, value="claims fixed"),
            JobResult(ok=True, value=False),  # no commit
            JobResult(ok=True, value="claims again"),
            JobResult(ok=True, value=False),  # STILL no commit
        )

        item = _item(make_work_item, state="")
        outcome = _drive(stage, item, ctx, pool)

        assert outcome == StageOutcome(Disposition.FAIL_BACK, "fix_exhausted")
        agent_jobs = [h.job for h in pool.submitted if isinstance(h.job, AgentJob)]
        assert len(agent_jobs) == 2  # never a third session


class TestCiPromptBuilders:
    """The composed builders reuse the orchestrator templates verbatim."""

    def test_build_ci_fix_prompt_contents(self) -> None:
        """The fix prompt carries logs, branch, findings, and check names."""
        prompt = build_ci_fix_prompt(
            issue_number=7,
            pr_number=42,
            worktree_path="/tmp/wt",
            ci_logs="the-log-tail",
            pr_head_branch="7-auto-impl",
            advise_findings="learned things",
            review_threads_block="## Threads\n",
            failing_check_names=("lint", "pytest"),
        )
        assert "#42" in prompt  # pr_ref renders "<repo>#42"
        assert "the-log-tail" in prompt
        assert "7-auto-impl" in prompt
        assert "learned things" in prompt
        assert "- lint" in prompt
        assert "git commit -S -s" in prompt

    def test_build_force_engagement_prompt_contents(self) -> None:
        """The escalation prompt names the checks and the branch invariant."""
        prompt = build_force_engagement_prompt(
            issue_number=7,
            pr_number=42,
            worktree_path="/tmp/wt",
            pr_head_branch="7-auto-impl",
            failing_check_names=("mypy",),
        )
        assert "Force-Engagement Retry" in prompt
        assert "- mypy" in prompt
        assert "7-auto-impl" in prompt
        assert "BLOCKED:" in prompt


class TestCiOnJobDone:
    """on_job_done result routing (state still the submitting WAIT state)."""

    def test_fix_failure_counts_budget_and_flags(self, make_ctx: Any, make_work_item: Any) -> None:
        """A hard-failed fix job consumes ci_fix and flags ci_fix_failed."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github())
        item = _item(make_work_item, state=FIX_WAIT)

        stage.on_job_done(item, JobResult(ok=False, error="boom"), ctx)

        assert item.attempts["ci_fix"] == 1
        assert item.payload["ci_fix_failed"] is True

        # PUSH_WAIT consumes the flag and reroutes to POLL without a job.
        item.state = PUSH_WAIT
        result = stage.step(item, ctx)
        assert result == Continue(next_state=POLL)

    def test_push_result_variants(self, make_ctx: Any, make_work_item: Any) -> None:
        """ok+commit resets backoff; ok+no-commit flags; failure flags hard."""
        stage = CiStage()
        ctx = make_ctx(github=_go_github())
        item = _item(make_work_item, state=PUSH_WAIT)

        item.payload[CI_POLL_STARTED_AT] = 111.0
        item.payload["ci_poll_count"] = 9
        item.payload["retry_delay_s"] = 60
        stage.on_job_done(item, JobResult(ok=True, value=True), ctx)
        assert CI_POLL_STARTED_AT not in item.payload
        assert "ci_poll_count" not in item.payload
        assert "retry_delay_s" not in item.payload

        stage.on_job_done(item, JobResult(ok=True, value=False), ctx)
        push_no_commit = item.payload.pop("push_no_commit")
        assert push_no_commit is True

        stage.on_job_done(item, JobResult(ok=False, error="remote gone"), ctx)
        assert item.payload["push_failed"] is True
