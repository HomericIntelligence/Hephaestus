"""Tests for the planning stage (doc section "2. planning")."""

from __future__ import annotations

from typing import Any

from hephaestus.automation.pipeline.jobs import AgentJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.planning import (
    PlanningStage,
    build_plan_prompt,
)
from hephaestus.automation.prompts.planning import get_plan_prompt
from hephaestus.automation.protocol import PLAN_COMMENT_MARKER
from hephaestus.automation.state_labels import (
    STATE_NEEDS_PLAN,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
    STATE_SKIP,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


class TestBuildPlanPrompt:
    """build_plan_prompt composes the plan prompt with the advise block."""

    def test_without_findings_is_plan_prompt_verbatim(self) -> None:
        """No advise findings means the untouched template output."""
        assert build_plan_prompt(7) == get_plan_prompt(7)
        assert build_plan_prompt(7, "") == get_plan_prompt(7)

    def test_with_findings_appends_learnings_block(self) -> None:
        """Advise findings ride in the legacy learnings block."""
        prompt = build_plan_prompt(7, "Use the retry helper from utils.")

        assert prompt.startswith(get_plan_prompt(7))  # template reused verbatim
        assert "## Prior Learnings from Team Knowledge Base" in prompt
        assert prompt.endswith("Use the retry helper from utils.")


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

    def test_existing_plan_fast_forwards_to_verify(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A restart with a posted plan comment jumps straight to VERIFY.

        Real has-plan semantics: advise + plan are never redone mid-stage.
        """
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN], has_plan=True)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=9, state="ENTER")

        outcome = stage.on_enter(item, ctx)

        assert outcome is None  # proceed, but...
        assert item.state == "VERIFY"  # ...straight to verification
        assert github.mutation_log == []  # no rewrites on re-entry

    def test_double_on_enter_is_idempotent(self, make_ctx: Any, make_work_item: Any) -> None:
        """A literal double on_enter produces no extra mutations or moves."""
        stage = PlanningStage()
        github = FakeStageGitHub(has_plan=True)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=10, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        assert item.state == "VERIFY"
        log_after_first = list(github.mutation_log)
        assert log_after_first == [("gh_issue_add_labels", (10, (STATE_NEEDS_PLAN,)))]

        assert stage.on_enter(item, ctx) is None  # second literal call

        assert item.state == "VERIFY"
        assert github.mutation_log == log_after_first  # nothing new written

    def test_replan_entry_swaps_no_go_for_needs_plan_atomically(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A state:plan-no-go fail-back entry swaps to needs-plan in ONE write."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=20, state="ENTER")

        outcome = stage.on_enter(item, ctx)

        assert outcome is None  # proceed to re-plan, not fast-forward
        assert item.state == "ENTER"  # no premature VERIFY fast-forward
        # Exactly one atomic edit — invariant never transiently broken.
        assert github.mutation_log == [
            ("edit_labels", (20, (STATE_NEEDS_PLAN,), (STATE_PLAN_NO_GO, STATE_PLAN_GO))),
        ]
        assert STATE_PLAN_NO_GO not in github.labels[20]
        assert STATE_PLAN_GO not in github.labels[20]
        assert STATE_NEEDS_PLAN in github.labels[20]

    def test_replan_entry_with_stale_go_swaps_atomically(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Defense-in-depth: a stale state:plan-go on entry is also swapped."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_GO])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=21, state="ENTER")

        outcome = stage.on_enter(item, ctx)

        # The STATE_PLAN_GO guard at line 168 should have caught this and
        # returned ADVANCE, so we never reach the swap. But if a stale
        # STATE_PLAN_GO somehow persisted past that check, the swap would
        # remove it; this test is defense-in-depth.
        assert outcome is not None
        assert outcome.disposition == Disposition.ADVANCE

    def test_replan_entry_idempotent_when_labels_already_swapped(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Re-entry after a successful swap writes nothing (idempotency)."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=22, state="ENTER")

        outcome = stage.on_enter(item, ctx)

        assert outcome is None
        # No swap triggered (neither STATE_PLAN_NO_GO nor STATE_PLAN_GO present).
        # No add triggered (STATE_NEEDS_PLAN already present).
        assert github.mutation_log == []


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
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.on_done_state == "PLAN_WAIT"
        assert result.job.descr == "advise"
        assert result.job.prompt_kwargs["issue_number"] == 3

    def test_plan_wait_requests_plan_job(self, make_ctx: Any, make_work_item: Any) -> None:
        """PLAN_WAIT submits the plan job (planner session) and lands in VERIFY."""
        stage = PlanningStage()
        ctx = make_ctx()
        item = make_work_item(issue=4, state="PLAN_WAIT")
        item.payload["advise_findings"] = "prior learnings"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.on_done_state == "VERIFY"
        assert result.job.descr == "plan"
        assert result.job.prompt_builder is build_plan_prompt
        # Advise findings travel via prompt_kwargs (builders run in-worker;
        # AgentJob is frozen, so no closures over payload).
        assert result.job.prompt_kwargs == {
            "issue_number": 4,
            "advise_findings": "prior learnings",
        }

    def test_plan_job_uses_selected_provider_and_planner_session_role(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Provider selection is distinct from the persisted planner session role."""
        stage = PlanningStage()
        config = type(
            "Cfg",
            (),
            {
                "enable_advise": True,
                "enable_learn": True,
                "force": False,
                "agent": "codex",
                "model": "gpt-default",
                "planner_model": "gpt-plan",
                "reviewer_model": "",
                "implementer_model": "",
                "dry_run": False,
            },
        )()
        ctx = make_ctx(config=config)
        item = make_work_item(issue=9, state="PLAN_WAIT")

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.agent == "codex"
        assert result.job.session_agent == "planner"
        assert result.job.model == "gpt-plan"

    def test_verify_with_plan_advances(self, make_ctx: Any, make_work_item: Any) -> None:
        """VERIFY with an existing plan comment advances without re-posting."""
        stage = PlanningStage()
        github = FakeStageGitHub(has_plan=True)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=5, state="VERIFY")
        item.payload["plan_text"] = "# Implementation Plan\n\nAlready posted."

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE
        assert github.mutation_log == []  # existing plan: no duplicate upsert

    def test_verify_posts_plan_comment_then_advances(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The PIPELINE posts the plan comment (M1).

        VERIFY upserts the durable artifact BEFORE the verify/ADVANCE
        decision (journal order).
        """
        stage = PlanningStage()
        github = FakeStageGitHub(has_plan=False)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=11, state="VERIFY")
        item.payload["plan_text"] = "# Implementation Plan\n\nDo the thing."

        result = stage.step(item, ctx)

        # Durable write happened, in journal order, before ADVANCE existed.
        assert github.mutation_log == [
            ("gh_issue_upsert_comment", (11, PLAN_COMMENT_MARKER)),
        ]
        assert github.comments[11] == ["# Implementation Plan\n\nDo the thing."]
        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE

    def test_verify_posts_exactly_once_on_reentry(self, make_ctx: Any, make_work_item: Any) -> None:
        """Re-entering VERIFY never double-posts.

        The upsert is guarded by has_existing_plan (idempotent on re-entry).
        """
        stage = PlanningStage()
        github = FakeStageGitHub(has_plan=False)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=12, state="VERIFY")
        item.payload["plan_text"] = "# Implementation Plan\n\nOnce only."

        first = stage.step(item, ctx)
        second = stage.step(item, ctx)  # re-entry (e.g. after a restart)

        upserts = [m for m in github.mutation_log if m[0] == "gh_issue_upsert_comment"]
        assert len(upserts) == 1  # exactly one durable post
        assert isinstance(first, StageOutcome)
        assert first.disposition == Disposition.ADVANCE
        assert isinstance(second, StageOutcome)
        assert second.disposition == Disposition.ADVANCE

    def test_verify_normalizes_plan_body_to_marker(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Marker normalization is re-housed from _upsert_plan_comment.

        A markerless (or whitespace-prefixed) plan gets the marker prepended.
        """
        stage = PlanningStage()
        github = FakeStageGitHub(has_plan=False)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=13, state="VERIFY")
        item.payload["plan_text"] = "\n\nSome plan without the heading."

        stage.step(item, ctx)

        (body,) = github.comments[13]
        assert body.startswith(PLAN_COMMENT_MARKER)  # upsert helper keys on this
        assert body == f"{PLAN_COMMENT_MARKER}\n\nSome plan without the heading."

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


class TestPlanningFlowWithFakePool:
    """Drive the whole stage through the canonical FakeWorkerPool (m6)."""

    def test_full_walk_enter_to_advance(self, make_ctx: Any, make_work_item: Any) -> None:
        """Full pool-driven walk of the whole stage.

        ENTER -> ADVISE_WAIT -> PLAN_WAIT -> VERIFY -> ADVANCE, with the
        durable writes in journal order.
        """
        from tests.unit.automation.pipeline.conftest import FakeWorkerPool

        stage = PlanningStage()
        github = FakeStageGitHub()  # unlabeled, no PRs, no plan yet
        ctx = make_ctx(github=github)
        item = make_work_item(issue=40, state="ENTER")

        pool = FakeWorkerPool()
        pool.script(
            JobResult(ok=True, value="advise findings"),  # advise
            JobResult(ok=True, value="# Implementation Plan\n\nSteps."),  # plan
        )

        assert stage.on_enter(item, ctx) is None

        outcome = None
        for _ in range(10):  # bounded driver loop
            result = stage.step(item, ctx)
            if isinstance(result, Continue):
                item.state = result.next_state
                continue
            if isinstance(result, JobRequest):
                pool.submit(result.job, result.on_done_state)  # type: ignore[arg-type]
                _handle, job_result = pool.completion_q.get_nowait()
                assert not job_result.interrupted
                stage.on_job_done(item, job_result, ctx)
                item.state = result.on_done_state
                continue
            outcome = result
            break

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.ADVANCE
        # Both agent jobs ran, in order, with the payload threaded through.
        assert [h.job.descr for h in pool.submitted] == ["advise", "plan"]
        plan_job = pool.submitted[1].job
        assert isinstance(plan_job, AgentJob)  # narrows the job union for mypy
        assert plan_job.prompt_kwargs["advise_findings"] == "advise findings"
        # Durable writes, pinned in journal order: entry label first, then
        # the plan-comment artifact — both before the ADVANCE outcome.
        assert github.mutation_log == [
            ("gh_issue_add_labels", (40, (STATE_NEEDS_PLAN,))),
            ("gh_issue_upsert_comment", (40, PLAN_COMMENT_MARKER)),
        ]
        assert github.comments[40] == ["# Implementation Plan\n\nSteps."]
