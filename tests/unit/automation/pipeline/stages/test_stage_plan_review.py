"""Tests for the plan-review stage (doc section "3. plan_review")."""

from __future__ import annotations

from typing import Any

from hephaestus.automation.claude_invoke import ReviewVerdict, parse_review_verdict
from hephaestus.automation.pipeline.jobs import JobResult
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.plan_review import PlanReviewStage
from hephaestus.automation.state_labels import (
    STATE_NEEDS_PLAN,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _verdict(kind: str) -> ReviewVerdict:
    """Build a ReviewVerdict of the given kind for EVAL tests."""
    return ReviewVerdict(grade=None, verdict=kind, raw=f"review text ({kind})")


class TestPlanReviewStageStep:
    """step state machine: ENTER -> REVIEW_WAIT -> EVAL -> AMEND/LEARN."""

    def test_on_enter_is_noop(self, make_ctx: Any, make_work_item: Any) -> None:
        """on_enter writes nothing and always proceeds (idempotent)."""
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        assert github.mutation_log == []

    def test_enter_routes_to_review(self, make_ctx: Any, make_work_item: Any) -> None:
        """ENTER advances to REVIEW_WAIT."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="ENTER")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "REVIEW_WAIT"

    def test_review_wait_requests_review(self, make_ctx: Any, make_work_item: Any) -> None:
        """REVIEW_WAIT submits the review job with in-worker verdict parsing."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=2, state="REVIEW_WAIT")
        item.payload["plan_text"] = "# Plan"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert result.on_done_state == "EVAL"
        assert result.job.descr == "review"
        assert result.job.parse is parse_review_verdict  # verdict parsed in-worker
        assert item.attempts["plan_review_iter"] == 1
        assert result.job.prompt_kwargs["iteration"] == 0  # 0-based for the prompt
        assert result.job.prompt_kwargs["prior_review"] is None  # first round
        assert result.job.prompt_kwargs["plan_text"] == "# Plan"

    def test_review_wait_threads_prior_review(self, make_ctx: Any, make_work_item: Any) -> None:
        """A later review round passes the prior review text to the prompt."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=3, state="REVIEW_WAIT")
        item.attempts["plan_review_iter"] = 1
        item.payload["prior_review"] = "fix the tests section"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert item.attempts["plan_review_iter"] == 2
        assert result.job.prompt_kwargs["iteration"] == 1
        assert result.job.prompt_kwargs["prior_review"] == "fix the tests section"

    def test_eval_go_applies_label_advances(self, make_ctx: Any, make_work_item: Any) -> None:
        """GO durably applies state:plan-go then advances (learn disabled)."""
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        ctx.config.enable_learn = False
        item = make_work_item(issue=2, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE
        assert github.mutation_log == [
            ("gh_issue_add_labels", (2, (STATE_PLAN_GO,))),
            ("gh_issue_remove_labels", (2, (STATE_PLAN_NO_GO, STATE_NEEDS_PLAN))),
        ]

    def test_eval_go_with_learn_continues_to_learn(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """GO with learn enabled writes the label then continues to LEARN_WAIT."""
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        ctx.config.enable_learn = True
        item = make_work_item(issue=3, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "LEARN_WAIT"
        assert STATE_PLAN_GO in github.labels[3]

    def test_eval_nogo_within_budget_amends(self, make_ctx: Any, make_work_item: Any) -> None:
        """NOGO within the iteration budget continues to AMEND_WAIT, no writes."""
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=4, state="EVAL")
        item.attempts["plan_review_iter"] = 1  # 1 < budget 3
        item.payload["review_verdict"] = _verdict("NOGO")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "AMEND_WAIT"
        assert github.mutation_log == []

    def test_eval_nogo_exhausted_fails_back_nogo(self, make_ctx: Any, make_work_item: Any) -> None:
        """NOGO at the iteration cap applies no-go and fails back ("nogo")."""
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=5, state="EVAL")
        item.attempts["plan_review_iter"] = 3  # 3 >= budget 3
        item.payload["review_verdict"] = _verdict("NOGO")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FAIL_BACK
        assert result.note == "nogo"  # plan_cycles remain (1 < 2)
        assert item.attempts["plan_cycles"] == 1
        assert github.mutation_log == [
            ("gh_issue_add_labels", (5, (STATE_PLAN_NO_GO,))),
            ("gh_issue_remove_labels", (5, (STATE_PLAN_GO, STATE_NEEDS_PLAN))),
        ]

    def test_eval_nogo_plan_cycles_exhausted(self, make_ctx: Any, make_work_item: Any) -> None:
        """NOGO at the cap with plan_cycles consumed fails back terminally."""
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=6, state="EVAL")
        item.attempts["plan_review_iter"] = 3
        item.attempts["plan_cycles"] = 1  # this fail-back becomes 2/2
        item.payload["review_verdict"] = _verdict("NOGO")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FAIL_BACK
        assert result.note == "plan_cycles_exhausted"
        assert item.attempts["plan_cycles"] == 2
        assert STATE_PLAN_NO_GO in github.labels[6]  # label still written first

    def test_eval_ambiguous_at_cap_treated_as_nogo(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """AMBIGUOUS at the iteration cap takes the no-go exhaustion path."""
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=7, state="EVAL")
        item.attempts["plan_review_iter"] = 3
        item.payload["review_verdict"] = _verdict("AMBIGUOUS")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FAIL_BACK
        assert STATE_PLAN_NO_GO in github.labels[7]

    def test_eval_error_leaves_labels_untouched(self, make_ctx: Any, make_work_item: Any) -> None:
        """ERROR (reviewer infrastructure) retries with zero label writes."""
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=8, state="EVAL")
        item.payload["review_verdict"] = _verdict("ERROR")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.RETRY
        assert github.mutation_log == []  # labels untouched on ERROR

    def test_eval_missing_verdict_retries(self, make_ctx: Any, make_work_item: Any) -> None:
        """EVAL without a stored verdict retries instead of guessing."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=9, state="EVAL")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.RETRY

    def test_amend_wait_requests_plan(self, make_ctx: Any, make_work_item: Any) -> None:
        """AMEND_WAIT submits the planner amend job and loops to REVIEW_WAIT."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=10, state="AMEND_WAIT")
        item.payload["prior_review"] = "Feedback: improve clarity"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert result.on_done_state == "REVIEW_WAIT"  # loop back to review
        assert result.job.descr == "amend"
        assert result.job.prompt_kwargs == {"issue_number": 10}

    def test_learn_wait_requests_learn(self, make_ctx: Any, make_work_item: Any) -> None:
        """LEARN_WAIT submits the learn job carrying the approved plan."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=11, state="LEARN_WAIT")
        item.payload["plan_text"] = "# My Plan\n..."

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert result.on_done_state == "FINISH"
        assert result.job.descr == "learn"
        assert result.job.prompt_kwargs == {"context": "# My Plan\n..."}

    def test_finish_advances(self, make_ctx: Any, make_work_item: Any) -> None:
        """FINISH advances to the next stage."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=12, state="FINISH")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE

    def test_unknown_state_fails(self, make_ctx: Any, make_work_item: Any) -> None:
        """An unknown state finishes failed instead of looping silently."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=13, state="BOGUS")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL

    def test_no_issue_number_fails(self, make_ctx: Any, make_work_item: Any) -> None:
        """Step without an issue number finishes failed."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=None, state="ENTER")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL


class TestPlanReviewStageOnJobDone:
    """on_job_done payload handling (state still at the WAIT state)."""

    def test_review_verdict_stored_in_payload(self, make_ctx: Any, make_work_item: Any) -> None:
        """The parsed verdict and its raw text are stored on the payload."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="REVIEW_WAIT")
        verdict = _verdict("GO")
        result = JobResult(ok=True, value=verdict)

        stage.on_job_done(item, result, ctx)

        assert item.payload["review_verdict"] == verdict
        assert item.payload["prior_review"] == verdict.raw

    def test_amend_result_stored_in_payload(self, make_ctx: Any, make_work_item: Any) -> None:
        """The amended plan text is stored on the payload."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=2, state="AMEND_WAIT")
        result = JobResult(ok=True, value="# Amended plan here")

        stage.on_job_done(item, result, ctx)

        assert item.payload["plan_text"] == "# Amended plan here"

    def test_failed_result_is_not_stored(self, make_ctx: Any, make_work_item: Any) -> None:
        """A failed job result is logged and never stored."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=3, state="REVIEW_WAIT")
        result = JobResult(ok=False, error="reviewer crashed")

        stage.on_job_done(item, result, ctx)

        assert "review_verdict" not in item.payload


class TestDurableWriteOrdering:
    """The load-bearing invariant: durable writes precede advancing outcomes."""

    def test_go_verdict_mutation_before_advance(self, make_ctx: Any, make_work_item: Any) -> None:
        """The state:plan-go write is recorded before ADVANCE is returned."""
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        ctx.config.enable_learn = False
        item = make_work_item(issue=11, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")

        result = stage.step(item, ctx)

        # Mutations are recorded at the moment the advancing outcome exists.
        assert github.mutation_log[0][0] == "gh_issue_add_labels"
        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE

    def test_nogo_exhausted_mutation_before_fail_back(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The state:plan-no-go write is recorded before FAIL_BACK is returned."""
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=12, state="EVAL")
        item.attempts["plan_review_iter"] = 3
        item.payload["review_verdict"] = _verdict("NOGO")

        result = stage.step(item, ctx)

        assert github.mutation_log[0][0] == "gh_issue_add_labels"
        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FAIL_BACK


class TestReviewFlowWithFakePool:
    """Drive the review round through the canonical FakeWorkerPool."""

    def test_review_round_to_go(self, make_ctx: Any, make_work_item: Any) -> None:
        """REVIEW_WAIT job -> pool -> on_job_done -> EVAL -> ADVANCE."""
        from tests.unit.automation.pipeline.conftest import FakeWorkerPool

        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        ctx.config.enable_learn = False
        item = make_work_item(issue=20, state="REVIEW_WAIT")
        item.payload["plan_text"] = "# Plan"

        request = stage.step(item, ctx)
        assert isinstance(request, JobRequest)

        pool = FakeWorkerPool()
        pool.script(JobResult(ok=True, value=_verdict("GO")))
        handle = pool.submit(request.job, request.on_done_state)  # type: ignore[arg-type]
        done_handle, done_result = pool.completion_q.get_nowait()
        assert done_handle is handle
        assert not done_result.interrupted  # on_job_done contract precondition

        stage.on_job_done(item, done_result, ctx)  # state still REVIEW_WAIT
        item.state = request.on_done_state  # coordinator advances to EVAL

        outcome = stage.step(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.ADVANCE
        assert STATE_PLAN_GO in github.labels[20]
