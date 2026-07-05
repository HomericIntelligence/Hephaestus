"""Tests for the planning stage (doc section "2. planning")."""

from __future__ import annotations

from typing import Any

from hephaestus.automation.pipeline.jobs import JobResult
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.planning import PlanningStage
from hephaestus.automation.state_labels import (
    STATE_NEEDS_PLAN,
    STATE_PLAN_GO,
    STATE_SKIP,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


class TestPlanningStageEnter:
    """on_enter idempotency guards and fast-forward checks."""

    def test_plan_go_fast_forward_advance(self, make_ctx: Any, make_work_item: Any) -> None:
        """At-or-past state:plan-go advances immediately with zero jobs/writes."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_GO])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1)

        outcome = stage.on_enter(item, ctx)

        assert outcome is not None
        assert outcome.disposition == Disposition.ADVANCE
        assert github.mutation_log == []  # no mutations on fast-forward

    def test_skip_label_skips(self, make_ctx: Any, make_work_item: Any) -> None:
        """state:skip routes the item away without any writes."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_SKIP])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=2)

        outcome = stage.on_enter(item, ctx)

        assert outcome is not None
        assert outcome.disposition == Disposition.SKIP
        assert github.mutation_log == []

    def test_merged_pr_closes_issue(self, make_ctx: Any, make_work_item: Any) -> None:
        """A merged closing PR closes the issue as covered (gate A)."""
        stage = PlanningStage()
        github = FakeStageGitHub(merged_pr=123)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=3)

        outcome = stage.on_enter(item, ctx)

        assert outcome is not None
        assert outcome.disposition == Disposition.SKIP
        assert github.mutation_log == [("close_issue_as_covered", (3, 123))]

    def test_open_pr_skips(self, make_ctx: Any, make_work_item: Any) -> None:
        """An open PR for the issue skips planning with zero writes (gate B)."""
        stage = PlanningStage()
        github = FakeStageGitHub(open_pr=456)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=4)

        outcome = stage.on_enter(item, ctx)

        assert outcome is not None
        assert outcome.disposition == Disposition.SKIP
        assert github.mutation_log == []

    def test_unlabeled_entry_adds_needs_plan(self, make_ctx: Any, make_work_item: Any) -> None:
        """Unlabeled entry durably writes state:needs-plan before proceeding."""
        stage = PlanningStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=5)

        outcome = stage.on_enter(item, ctx)

        assert outcome is None  # proceed to step()
        assert github.mutation_log == [("gh_issue_add_labels", (5, (STATE_NEEDS_PLAN,)))]
        assert STATE_NEEDS_PLAN in github.labels[5]

    def test_reentry_with_needs_plan_is_idempotent(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Re-entry with state:needs-plan already present writes nothing."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=6)

        outcome = stage.on_enter(item, ctx)

        assert outcome is None
        assert github.mutation_log == []

    def test_label_refresh_updates_cache(self, make_ctx: Any, make_work_item: Any) -> None:
        """on_enter refreshes item.labels_cache from GitHub."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=7, labels=["stale:label"])

        stage.on_enter(item, ctx)

        assert STATE_NEEDS_PLAN in item.labels_cache
        assert "stale:label" not in item.labels_cache

    def test_label_refresh_failure_falls_back_to_cache(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A failing label read falls back to the cached labels."""

        class BrokenGitHub(FakeStageGitHub):
            def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
                raise RuntimeError("gh unavailable")

        stage = PlanningStage()
        github = BrokenGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=8, labels=[STATE_PLAN_GO])

        outcome = stage.on_enter(item, ctx)

        assert outcome is not None
        assert outcome.disposition == Disposition.ADVANCE  # cached plan-go honored

    def test_no_issue_number_fails(self, make_ctx: Any, make_work_item: Any) -> None:
        """A work item without an issue number finishes failed."""
        stage = PlanningStage()
        ctx = make_ctx()
        item = make_work_item(issue=None)

        outcome = stage.on_enter(item, ctx)

        assert outcome is not None
        assert outcome.disposition == Disposition.FINISH_FAIL


class TestPlanningStageStep:
    """step state machine: ENTER -> ADVISE_WAIT -> PLAN_WAIT -> VERIFY."""

    def test_enter_routes_to_advise_when_enabled(self, make_ctx: Any, make_work_item: Any) -> None:
        """ENTER advances to ADVISE_WAIT when advise is enabled."""
        stage = PlanningStage()
        ctx = make_ctx()
        ctx.config.enable_advise = True
        item = make_work_item(issue=1, state="ENTER")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "ADVISE_WAIT"

    def test_enter_skips_advise_when_disabled(self, make_ctx: Any, make_work_item: Any) -> None:
        """ENTER advances straight to PLAN_WAIT when advise is disabled."""
        stage = PlanningStage()
        ctx = make_ctx()
        ctx.config.enable_advise = False
        item = make_work_item(issue=2, state="ENTER")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "PLAN_WAIT"

    def test_advise_wait_requests_advise_job(self, make_ctx: Any, make_work_item: Any) -> None:
        """ADVISE_WAIT submits the advise job and lands in PLAN_WAIT."""
        stage = PlanningStage()
        ctx = make_ctx()
        item = make_work_item(issue=3, state="ADVISE_WAIT")

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert result.on_done_state == "PLAN_WAIT"
        assert result.job.descr == "advise"
        assert result.job.prompt_kwargs["issue_number"] == 3

    def test_plan_wait_requests_plan_job(self, make_ctx: Any, make_work_item: Any) -> None:
        """PLAN_WAIT submits the plan job (planner session) and lands in VERIFY."""
        stage = PlanningStage()
        ctx = make_ctx()
        item = make_work_item(issue=4, state="PLAN_WAIT")

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert result.on_done_state == "VERIFY"
        assert result.job.descr == "plan"
        assert result.job.prompt_kwargs == {"issue_number": 4}

    def test_verify_with_plan_advances(self, make_ctx: Any, make_work_item: Any) -> None:
        """VERIFY with an existing plan comment advances."""
        stage = PlanningStage()
        github = FakeStageGitHub(has_plan=True)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=5, state="VERIFY")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE

    def test_verify_without_plan_retries(self, make_ctx: Any, make_work_item: Any) -> None:
        """VERIFY without a plan retries while within the plan budget."""
        stage = PlanningStage()
        github = FakeStageGitHub(has_plan=False)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=6, state="VERIFY")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.RETRY
        assert item.attempts["plan"] == 1

    def test_verify_exhausts_budget(self, make_ctx: Any, make_work_item: Any) -> None:
        """VERIFY fails after exhausting the plan budget (2)."""
        stage = PlanningStage()
        github = FakeStageGitHub(has_plan=False)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=7, state="VERIFY")
        item.attempts["plan"] = 1  # this attempt becomes 2/2

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL

    def test_unknown_state_fails(self, make_ctx: Any, make_work_item: Any) -> None:
        """An unknown state finishes failed instead of looping silently."""
        stage = PlanningStage()
        ctx = make_ctx()
        item = make_work_item(issue=8, state="BOGUS")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL

    def test_no_issue_number_fails(self, make_ctx: Any, make_work_item: Any) -> None:
        """Step without an issue number finishes failed."""
        stage = PlanningStage()
        ctx = make_ctx()
        item = make_work_item(issue=None, state="ENTER")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL


class TestPlanningStageOnJobDone:
    """on_job_done payload handling (state still at the WAIT state)."""

    def test_advise_result_stored_in_payload(self, make_ctx: Any, make_work_item: Any) -> None:
        """The advise job's findings are stored on the payload."""
        stage = PlanningStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="ADVISE_WAIT")
        result = JobResult(ok=True, value="advise findings here")

        stage.on_job_done(item, result, ctx)

        assert item.payload["advise_findings"] == "advise findings here"

    def test_plan_result_stored_in_payload(self, make_ctx: Any, make_work_item: Any) -> None:
        """The plan job's text is stored on the payload."""
        stage = PlanningStage()
        ctx = make_ctx()
        item = make_work_item(issue=2, state="PLAN_WAIT")
        result = JobResult(ok=True, value="# Issue plan here")

        stage.on_job_done(item, result, ctx)

        assert item.payload["plan_text"] == "# Issue plan here"

    def test_failed_result_is_not_stored(self, make_ctx: Any, make_work_item: Any) -> None:
        """A failed job result is logged and never stored."""
        stage = PlanningStage()
        ctx = make_ctx()
        item = make_work_item(issue=3, state="PLAN_WAIT")
        result = JobResult(ok=False, error="agent timeout")

        stage.on_job_done(item, result, ctx)

        assert "plan_text" not in item.payload
