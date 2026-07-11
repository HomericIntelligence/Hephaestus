"""Tests for the plan-review stage (doc section "3. plan_review")."""

from __future__ import annotations

import re
from typing import Any

import pytest

from hephaestus.automation.claude_invoke import ReviewVerdict, parse_review_verdict
from hephaestus.automation.pipeline.jobs import AgentJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.plan_review import (
    PLAN_FINISH,
    REVIEW_ERROR_RETRY_CAP,
    PlanReviewStage,
    build_amend_prompt,
)
from hephaestus.automation.prompts._shared import _UNTRUSTED_NOTICE
from hephaestus.automation.prompts.planning import get_plan_prompt
from hephaestus.automation.protocol import PLAN_COMMENT_MARKER
from hephaestus.automation.state_labels import (
    STATE_NEEDS_PLAN,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _verdict(kind: str) -> ReviewVerdict:
    """Build a ReviewVerdict of the given kind for EVAL tests."""
    return ReviewVerdict(grade=None, verdict=kind, raw=f"review text ({kind})")


def _fence_present(prompt: str, label: str) -> bool:
    """Return True when a prompt has nonce-delimited markers for label."""
    return bool(
        re.search(rf"BEGIN_[0-9A-F]+_{label}\b", prompt)
        and re.search(rf"END_[0-9A-F]+_{label}\b", prompt)
    )


class TestBuildAmendPrompt:
    """build_amend_prompt composes the plan prompt with the feedback block."""

    def test_contains_plan_prompt_and_feedback_block(self) -> None:
        """The output keeps issue context and fences reviewer critique."""
        prompt = build_amend_prompt(
            42,
            "The plan misses the tests section.",
            issue_title="Retry failure",
            issue_body="The loop retries forever.",
            advise_findings="Use the retry helper.",
        )

        assert get_plan_prompt(42) in prompt  # template reused inside the composed prompt
        assert _UNTRUSTED_NOTICE in prompt
        assert _fence_present(prompt, "ISSUE_TITLE")
        assert _fence_present(prompt, "ISSUE_BODY")
        assert _fence_present(prompt, "ADVISE_FINDINGS")
        assert _fence_present(prompt, "PRIOR_REVIEW")
        assert "## Prior reviewer critique — your previous plan got NOGO" in prompt
        assert "Address every concrete finding below in your revised plan:" in prompt
        assert "The plan misses the tests section." in prompt


class TestPlanReviewStageOnEnter:
    """on_enter cycle-relative counter reset (attempts are per-lifetime)."""

    def test_on_enter_plan_go_fast_forwards_advance(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A restart-reseeded item already state:plan-go advances with no job/writes."""
        stage = PlanReviewStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_GO])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="ENTER")

        outcome = stage.on_enter(item, ctx)

        assert outcome is not None
        assert outcome.disposition == Disposition.ADVANCE
        assert github.mutation_log == []  # no mutations on fast-forward

    def test_on_enter_without_plan_go_proceeds_to_cycle_reset(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An item without state:plan-go is unaffected by the new guard."""
        stage = PlanReviewStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="ENTER")

        outcome = stage.on_enter(item, ctx)

        assert outcome is None
        assert item.payload["review_cycle"] == 0
        assert item.payload["review_round"] == 0
        assert github.mutation_log == []

    def test_on_enter_writes_nothing(self, make_ctx: Any, make_work_item: Any) -> None:
        """on_enter performs no durable writes and always proceeds."""
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        assert github.mutation_log == []

    def test_on_enter_resets_round_for_new_cycle(self, make_ctx: Any, make_work_item: Any) -> None:
        """Entering with a fresh plan_cycles value resets the cycle counter."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="ENTER")
        item.attempts["plan_cycles"] = 1  # fail-back happened; new cycle
        item.payload["review_cycle"] = 0
        item.payload["review_round"] = 3  # cycle 1 exhausted its rounds

        stage.on_enter(item, ctx)

        assert item.payload["review_cycle"] == 1
        assert item.payload["review_round"] == 0  # cycle 2 gets a full budget

    def test_on_enter_same_cycle_keeps_round(self, make_ctx: Any, make_work_item: Any) -> None:
        """Same-cycle re-entry (e.g. the ERROR-path RETRY) keeps the round count."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=2, state="ENTER")
        item.payload["review_cycle"] = 0
        item.payload["review_round"] = 1  # one NOGO round already done

        stage.on_enter(item, ctx)

        assert item.payload["review_round"] == 1  # progress preserved

    def test_on_enter_double_call_is_idempotent(self, make_ctx: Any, make_work_item: Any) -> None:
        """A literal double on_enter changes nothing the second time."""
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=3, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        snapshot = dict(item.payload)
        assert stage.on_enter(item, ctx) is None

        assert item.payload == snapshot
        assert github.mutation_log == []


class TestPlanReviewStageStep:
    """step state machine: ENTER -> REVIEW_WAIT -> EVAL -> AMEND/LEARN."""

    def test_enter_routes_to_review(self, make_ctx: Any, make_work_item: Any) -> None:
        """ENTER advances to REVIEW_WAIT."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="ENTER")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "REVIEW_WAIT"

    def test_review_wait_requests_review(self, make_ctx: Any, make_work_item: Any) -> None:
        """REVIEW_WAIT submits the review job with in-worker verdict parsing.

        A submission is NOT an iteration: counters advance only in EVAL and
        only for real verdicts (#1554/#1794).
        """
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=2, state="REVIEW_WAIT")
        item.payload["plan_text"] = "# Plan"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.on_done_state == "EVAL"
        assert result.job.descr == "review"
        assert result.job.parse is parse_review_verdict  # verdict parsed in-worker
        assert item.attempts["plan_review_iter"] == 0  # submission burns nothing
        assert result.job.prompt_kwargs["iteration"] == 0  # 0-based for the prompt
        assert result.job.prompt_kwargs["prior_review"] is None  # first round
        assert result.job.prompt_kwargs["plan_text"] == "# Plan"

    def test_review_wait_threads_prior_review(self, make_ctx: Any, make_work_item: Any) -> None:
        """A later review round passes the prior review text to the prompt."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=3, state="REVIEW_WAIT")
        item.payload["review_round"] = 1  # one review round completed
        item.payload["prior_review"] = "fix the tests section"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.job.prompt_kwargs["iteration"] == 1
        assert result.job.prompt_kwargs["prior_review"] == "fix the tests section"
        assert item.attempts["plan_review_iter"] == 0  # still EVAL's job to count

    def test_review_wait_uses_reviewer_timeout(self, make_ctx: Any, make_work_item: Any) -> None:
        """The review job is bounded by the plan-reviewer timeout, not the plan timeout.

        Migrated from the deleted legacy ``test_planner_loop`` suite: the
        reviewer runs under its own (typically shorter) timeout so a stalled
        review cannot burn the whole planner timeout budget.
        """
        from hephaestus.automation.agent_config import plan_reviewer_claude_timeout

        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=7, state="REVIEW_WAIT")

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.job.timeout_s == plan_reviewer_claude_timeout()

    def test_review_wait_clears_stale_verdict(self, make_ctx: Any, make_work_item: Any) -> None:
        """Submission clears any stale verdict (M3).

        Clearing payload["review_verdict"] at submission means a failed
        later round can never replay an earlier round's verdict in EVAL.
        """
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=4, state="REVIEW_WAIT")
        item.payload["review_verdict"] = _verdict("NOGO")  # stale round-1 verdict

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert "review_verdict" not in item.payload

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
        assert item.attempts["plan_review_iter"] == 1  # real verdict counted
        assert item.payload["review_round"] == 1

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
        """NOGO within the cycle budget continues to AMEND_WAIT, no writes."""
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=4, state="EVAL")
        item.payload["review_round"] = 0  # first review round of the cycle
        item.payload["review_verdict"] = _verdict("NOGO")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "AMEND_WAIT"
        assert github.mutation_log == []
        assert item.payload["review_round"] == 1  # round counted in EVAL
        assert item.attempts["plan_review_iter"] == 1  # lifetime audit trail

    def test_eval_nogo_exhausted_fails_back_nogo(self, make_ctx: Any, make_work_item: Any) -> None:
        """NOGO at the iteration cap applies no-go and fails back ("nogo")."""
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=5, state="EVAL")
        item.payload["review_round"] = 2  # this verdict is round 3/3
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
        item.payload["review_round"] = 2
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
        item.payload["review_round"] = 2
        item.payload["review_verdict"] = _verdict("AMBIGUOUS")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FAIL_BACK
        assert STATE_PLAN_NO_GO in github.labels[7]

    def test_eval_error_leaves_labels_untouched(self, make_ctx: Any, make_work_item: Any) -> None:
        """ERROR retries with zero label writes and burns no iteration.

        Reviewer-infrastructure failure must not stamp a go/no-go label or
        consume review budget (#911/#1554/#1794).
        """
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=8, state="EVAL")
        item.payload["review_verdict"] = _verdict("ERROR")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.RETRY
        assert github.mutation_log == []  # labels untouched on ERROR
        assert item.attempts["plan_review_iter"] == 0  # no iteration burned
        assert item.payload.get("review_round", 0) == 0
        assert item.payload["review_error_retries"] == 1  # bounded retry loop

    def test_eval_missing_verdict_retries(self, make_ctx: Any, make_work_item: Any) -> None:
        """EVAL without a stored verdict retries instead of guessing."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=9, state="EVAL")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.RETRY
        assert item.attempts["plan_review_iter"] == 0  # no iteration burned
        assert item.payload["review_error_retries"] == 1

    def test_amend_wait_requests_plan(self, make_ctx: Any, make_work_item: Any) -> None:
        """AMEND_WAIT submits the amend job carrying the reviewer feedback."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=10, state="AMEND_WAIT")
        item.payload["issue_title"] = "Retry failure"
        item.payload["issue_body"] = "The loop retries forever."
        item.payload["advise_findings"] = "Use the retry helper."
        item.payload["prior_review"] = "Feedback: improve clarity"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.on_done_state == "REVIEW_WAIT"  # loop back to review
        assert result.job.descr == "amend"
        assert result.job.prompt_builder is build_amend_prompt
        # The feedback block travels via prompt_kwargs (builders run
        # in-worker; AgentJob is frozen, so no closures over payload).
        assert result.job.prompt_kwargs == {
            "issue_number": 10,
            "issue_title": "Retry failure",
            "issue_body": "The loop retries forever.",
            "advise_findings": "Use the retry helper.",
            "prior_review": "Feedback: improve clarity",
        }

    def test_learn_wait_requests_learn(self, make_ctx: Any, make_work_item: Any) -> None:
        """LEARN_WAIT submits the learn job carrying the approved plan."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=11, state="LEARN_WAIT")
        item.payload["plan_text"] = "# My Plan\n..."

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.on_done_state == PLAN_FINISH
        assert result.job.descr == "learn"
        assert result.job.prompt_kwargs == {"context": "# My Plan\n..."}

    def test_finish_advances(self, make_ctx: Any, make_work_item: Any) -> None:
        """PLAN_FINISH advances to the next stage."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=12, state=PLAN_FINISH)

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

    def test_nogo_review_verdict_threads_raw_prior_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A real NOGO stores raw review text for the amend prompt."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="REVIEW_WAIT")
        verdict = _verdict("NOGO")
        result = JobResult(ok=True, value=verdict)

        stage.on_job_done(item, result, ctx)

        assert item.payload["review_verdict"] == verdict
        assert item.payload["prior_review"] == verdict.raw

    @pytest.mark.parametrize("kind", ["GO", "ERROR", "AMBIGUOUS"])
    def test_non_nogo_review_verdict_does_not_thread_prior_review(
        self, make_ctx: Any, make_work_item: Any, kind: str
    ) -> None:
        """GO, ERROR, and AMBIGUOUS verdicts do not create amend feedback."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="REVIEW_WAIT")
        verdict = _verdict(kind)
        result = JobResult(ok=True, value=verdict)

        stage.on_job_done(item, result, ctx)

        assert item.payload["review_verdict"] == verdict
        assert "prior_review" not in item.payload

    def test_nogo_review_verdict_requires_raw_text(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A NOGO-shaped verdict without raw text fails instead of str() fallback."""

        class NogoWithoutRaw:
            verdict = "NOGO"

        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="REVIEW_WAIT")
        verdict = NogoWithoutRaw()
        result = JobResult(ok=True, value=verdict)

        with pytest.raises(AssertionError, match="NOGO verdict must expose raw"):
            stage.on_job_done(item, result, ctx)

        assert item.payload["review_verdict"] is verdict
        assert "prior_review" not in item.payload

    def test_amend_result_stored_in_payload(self, make_ctx: Any, make_work_item: Any) -> None:
        """The amended plan text is stored on the payload."""
        stage = PlanReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=2, state="AMEND_WAIT")
        result = JobResult(ok=True, value="# Amended plan here")

        stage.on_job_done(item, result, ctx)

        assert item.payload["plan_text"] == "# Amended plan here"

    def test_amend_result_upserts_durable_plan_comment(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Amended plans are journaled before the next review can approve them."""
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=2, state="AMEND_WAIT")
        result = JobResult(ok=True, value="\n\n# Amended plan here")

        stage.on_job_done(item, result, ctx)

        assert item.payload["plan_text"] == "\n\n# Amended plan here"
        assert github.comments[2] == [f"{PLAN_COMMENT_MARKER}\n\n# Amended plan here"]
        assert github.mutation_log == [
            ("gh_issue_upsert_comment", (2, PLAN_COMMENT_MARKER)),
        ]

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
        item.payload["review_round"] = 2  # this verdict is round 3/3
        item.payload["review_verdict"] = _verdict("NOGO")

        result = stage.step(item, ctx)

        assert github.mutation_log[0][0] == "gh_issue_add_labels"
        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FAIL_BACK


class TestStaleVerdictAndErrorAccounting:
    """M3 (no stale-verdict replay) + M4 (ERROR burns nothing, bounded)."""

    def test_failed_round2_job_retries_instead_of_replaying_round1_nogo(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A failed round-2 job hits EVAL's no-verdict RETRY, not round 1's NOGO.

        Scenario: round 1 produced a NOGO verdict; the amend ran; the round-2
        review job FAILS (on_job_done stores nothing). Because REVIEW_WAIT
        cleared the stale verdict at submission, EVAL must RETRY — without
        amending again and without burning iteration budget.
        """
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=30, state="EVAL")
        item.payload["review_verdict"] = _verdict("NOGO")  # round 1
        item.payload["review_round"] = 0

        assert isinstance(stage.step(item, ctx), Continue)  # round 1 -> AMEND_WAIT
        item.state = "AMEND_WAIT"
        stage.on_job_done(item, JobResult(ok=True, value="# Amended"), ctx)
        item.state = "REVIEW_WAIT"

        request = stage.step(item, ctx)  # round-2 submission clears the verdict
        assert isinstance(request, JobRequest)

        failed = JobResult(ok=False, error="reviewer crashed")
        stage.on_job_done(item, failed, ctx)  # stores nothing
        item.state = "EVAL"
        iter_before = item.attempts["plan_review_iter"]
        round_before = item.payload["review_round"]

        outcome = stage.step(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.RETRY  # NOT a replayed NOGO
        assert item.attempts["plan_review_iter"] == iter_before  # budget intact
        assert item.payload["review_round"] == round_before
        assert github.mutation_log == [
            ("gh_issue_upsert_comment", (30, PLAN_COMMENT_MARKER)),
        ]  # amended plan persisted, no labels on the error path

    def test_error_round_burns_nothing_and_nogo_still_amendable(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An ERROR round burns nothing; the next NOGO can still amend.

        R1 NOGO -> R2 ERROR -> RETRY (iter unchanged, no labels) -> R2 NOGO
        -> amend still available (no premature no-go).
        """
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=31, state="EVAL")
        assert stage.on_enter(item, ctx) is None

        # Round 1: NOGO -> amend (round 1/3 consumed).
        item.payload["review_verdict"] = _verdict("NOGO")
        assert isinstance(stage.step(item, ctx), Continue)
        assert item.attempts["plan_review_iter"] == 1

        # Round 2 attempt: reviewer infrastructure ERROR.
        item.payload["review_verdict"] = _verdict("ERROR")
        outcome = stage.step(item, ctx)
        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.RETRY
        assert item.attempts["plan_review_iter"] == 1  # iter unchanged
        assert item.payload["review_round"] == 1
        assert github.mutation_log == []  # no labels

        # Round 2 rerun: a real NOGO — the cycle still has amends left.
        item.payload["review_verdict"] = _verdict("NOGO")
        result = stage.step(item, ctx)
        assert isinstance(result, Continue)
        assert result.next_state == "AMEND_WAIT"  # no premature no-go
        assert item.payload["review_error_retries"] == 0  # reset on real verdict
        assert github.mutation_log == []

    def test_error_retry_cap_trips(self, make_ctx: Any, make_work_item: Any) -> None:
        """Consecutive reviewer failures beyond the cap FINISH_FAIL, no labels."""
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=32, state="EVAL")

        for expected_retry in range(1, REVIEW_ERROR_RETRY_CAP + 1):
            item.payload["review_verdict"] = _verdict("ERROR")
            outcome = stage.step(item, ctx)
            assert isinstance(outcome, StageOutcome)
            assert outcome.disposition == Disposition.RETRY
            assert item.payload["review_error_retries"] == expected_retry

        item.payload["review_verdict"] = _verdict("ERROR")
        outcome = stage.step(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.FINISH_FAIL
        assert "reviewer error retries exhausted" in outcome.note
        assert github.mutation_log == []  # labels stay untouched on ERROR path
        assert item.attempts["plan_review_iter"] == 0  # nothing ever burned


class TestCycleRelativeBudget:
    """m1: cycle 2 gets a full review budget; attempts stay per-lifetime."""

    def test_full_cycle_two_path(self, make_ctx: Any, make_work_item: Any) -> None:
        """Cycle 2 gets a full, fresh review budget.

        Cycle 1 exhausts 3 NOGOs -> FAIL_BACK(nogo); cycle 2 gets 3 fresh
        rounds (prompt iterations 0..2 again) -> plan_cycles_exhausted.
        """
        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=33, state="ENTER")

        def run_cycle() -> StageOutcome:
            assert stage.on_enter(item, ctx) is None
            assert isinstance(stage.step(item, ctx), Continue)  # ENTER
            item.state = "REVIEW_WAIT"
            prompt_iterations = []
            while True:
                request = stage.step(item, ctx)
                assert isinstance(request, JobRequest)
                assert isinstance(request.job, AgentJob)  # narrow the job union
                prompt_iterations.append(request.job.prompt_kwargs["iteration"])
                stage.on_job_done(item, JobResult(ok=True, value=_verdict("NOGO")), ctx)
                item.state = "EVAL"
                result = stage.step(item, ctx)
                if isinstance(result, StageOutcome):
                    assert prompt_iterations == [0, 1, 2]  # 0-based, per cycle
                    return result
                assert isinstance(result, Continue)  # AMEND_WAIT
                item.state = "AMEND_WAIT"
                amend = stage.step(item, ctx)
                assert isinstance(amend, JobRequest)
                stage.on_job_done(item, JobResult(ok=True, value="# Amended"), ctx)
                item.state = "REVIEW_WAIT"

        first = run_cycle()
        assert first.disposition == Disposition.FAIL_BACK
        assert first.note == "nogo"
        assert item.attempts["plan_review_iter"] == 3
        assert item.attempts["plan_cycles"] == 1

        # Fail-back routes through planning; the item re-enters this stage.
        item.state = "ENTER"
        second = run_cycle()  # full fresh budget: 3 more reviews
        assert second.disposition == Disposition.FAIL_BACK
        assert second.note == "plan_cycles_exhausted"
        assert item.attempts["plan_review_iter"] == 6  # lifetime audit trail
        assert item.attempts["plan_cycles"] == 2


class TestNonFatalLabelWrites:
    """m3: EVAL label-pair writes follow the legacy try/except-warn pattern."""

    def test_add_label_failure_does_not_propagate_and_remove_still_runs(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An add_labels exception is swallowed; remove_labels still runs.

        The add and remove writes are wrapped independently so a half-applied
        pair never propagates.
        """

        class AddFailsGitHub(FakeStageGitHub):
            def add_labels(self, issue_number: int, labels: list[str]) -> None:
                raise RuntimeError("gh add failed")

        stage = PlanReviewStage()
        github = AddFailsGitHub()
        ctx = make_ctx(github=github)
        ctx.config.enable_learn = False
        item = make_work_item(issue=34, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")

        result = stage.step(item, ctx)  # must not raise

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE
        assert github.mutation_log == [
            ("gh_issue_remove_labels", (34, (STATE_PLAN_NO_GO, STATE_NEEDS_PLAN))),
        ]

    def test_remove_label_failure_does_not_propagate(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A remove_labels exception after the add is swallowed too."""

        class RemoveFailsGitHub(FakeStageGitHub):
            def remove_labels(self, issue_number: int, labels: list[str]) -> None:
                raise RuntimeError("gh remove failed")

        stage = PlanReviewStage()
        github = RemoveFailsGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=35, state="EVAL")
        item.payload["review_round"] = 2
        item.payload["review_verdict"] = _verdict("NOGO")

        result = stage.step(item, ctx)  # must not raise

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FAIL_BACK
        assert github.mutation_log == [
            ("gh_issue_add_labels", (35, (STATE_PLAN_NO_GO,))),
        ]


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

    def test_full_walk_enter_to_advance(self, make_ctx: Any, make_work_item: Any) -> None:
        """Full pool-driven walk of the whole stage.

        ENTER -> REVIEW -> EVAL(NOGO) -> AMEND -> REVIEW -> EVAL(GO) ->
        LEARN -> PLAN_FINISH -> ADVANCE.
        """
        from tests.unit.automation.pipeline.conftest import FakeWorkerPool

        stage = PlanReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        ctx.config.enable_learn = True
        item = make_work_item(issue=21, state="ENTER")
        item.payload["plan_text"] = "# Plan v1"

        pool = FakeWorkerPool()
        pool.script(
            JobResult(ok=True, value=_verdict("NOGO")),  # review round 1
            JobResult(ok=True, value="# Plan v2"),  # amend
            JobResult(ok=True, value=_verdict("GO")),  # review round 2
            JobResult(ok=True, value="learn bullets"),  # learn
        )

        assert stage.on_enter(item, ctx) is None

        outcome = None
        for _ in range(20):  # bounded driver loop
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
        # The amended plan and both counters reflect the two real rounds.
        assert item.payload["plan_text"] == "# Plan v2"
        assert item.attempts["plan_review_iter"] == 2
        # All four jobs ran, in order.
        assert [h.job.descr for h in pool.submitted] == ["review", "amend", "review", "learn"]
        # Durable amended-plan write happens before the GO label write and ADVANCE outcome.
        assert github.mutation_log == [
            ("gh_issue_upsert_comment", (21, PLAN_COMMENT_MARKER)),
            ("gh_issue_add_labels", (21, (STATE_PLAN_GO,))),
            ("gh_issue_remove_labels", (21, (STATE_PLAN_NO_GO, STATE_NEEDS_PLAN))),
        ]
