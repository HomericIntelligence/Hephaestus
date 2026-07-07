"""Tests for the PR-review stage (doc section "5. pr_review")."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from hephaestus.automation.claude_invoke import ReviewVerdict, parse_review_verdict
from hephaestus.automation.pipeline.jobs import AgentJob, GitJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.pr_review import (
    REVIEW_ERROR_RETRY_CAP,
    PrReviewStage,
    _surviving_threads,
)
from hephaestus.automation.prompts.address_review import get_address_review_prompt
from hephaestus.automation.prompts.implementation import get_impl_resume_feedback_prompt
from hephaestus.automation.state_labels import STATE_SKIP
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _verdict(kind: str) -> ReviewVerdict:
    """Build a ReviewVerdict of the given kind for EVAL tests."""
    return ReviewVerdict(grade=None, verdict=kind, raw=f"review text ({kind})")


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


class TestPrReviewStageOnEnter:
    """on_enter cycle-relative counter reset (attempts are per-lifetime)."""

    def test_on_enter_writes_nothing(self, make_ctx: Any, make_work_item: Any) -> None:
        """on_enter performs no durable writes and always proceeds."""
        stage = PrReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        assert github.mutation_log == []

    def test_on_enter_resets_round_for_new_implementation_pass(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A fresh implement pass (new cycle key) resets the round counter."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="ENTER")
        item.attempts["implement"] = 2  # agent_error fail-back re-implemented
        item.payload["pr_review_cycle"] = 1
        item.payload["pr_review_round"] = 3  # cycle 1 exhausted its rounds
        item.payload["prev_unresolved"] = 4

        stage.on_enter(item, ctx)

        assert item.payload["pr_review_cycle"] == 2
        assert item.payload["pr_review_round"] == 0  # cycle 2 gets a full budget
        assert "prev_unresolved" not in item.payload  # progress trail reset

    def test_on_enter_same_cycle_keeps_round(self, make_ctx: Any, make_work_item: Any) -> None:
        """Same-cycle re-entry (e.g. the ERROR-path RETRY) keeps the round count."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=2, pr=1001, state="ENTER")
        item.payload["pr_review_cycle"] = 0
        item.payload["pr_review_round"] = 2

        stage.on_enter(item, ctx)

        assert item.payload["pr_review_round"] == 2  # progress preserved

    def test_on_enter_double_call_is_idempotent(self, make_ctx: Any, make_work_item: Any) -> None:
        """A literal double on_enter changes nothing the second time."""
        stage = PrReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=3, pr=1001, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        snapshot = dict(item.payload)
        assert stage.on_enter(item, ctx) is None

        assert item.payload == snapshot
        assert github.mutation_log == []


class TestPrReviewStageStep:
    """step state machine: ENTER -> REVIEW -> VALIDATE -> POST -> ... -> EVAL."""

    def test_no_issue_number_fails(self, make_ctx: Any, make_work_item: Any) -> None:
        """Step without an issue number finishes failed."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=None, pr=1001, state="ENTER")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL

    def test_no_pr_fails_back_to_implementation(self, make_ctx: Any, make_work_item: Any) -> None:
        """Without a PR there is nothing to review: fail back agent_error."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=None, state="ENTER")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FAIL_BACK
        assert result.note == "agent_error"

    def test_enter_advances_to_review(self, make_ctx: Any, make_work_item: Any) -> None:
        """ENTER advances to REVIEW_WAIT."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="ENTER")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "REVIEW_WAIT"

    def test_review_wait_requests_review_with_in_worker_parse(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """REVIEW_WAIT submits the inline review job; verdict parsed in-worker.

        A submission is NOT an iteration: counters advance only in EVAL and
        only for real verdicts (#1554/#1794).
        """
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="REVIEW_WAIT")

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.on_done_state == "VALIDATE_WAIT"
        assert result.job.descr == "review"
        assert result.job.parse is parse_review_verdict  # verdict parsed in-worker
        assert result.job.prompt_kwargs["pr_number"] == 1001
        assert item.attempts["pr_review_iter"] == 0  # submission burns nothing

    def test_review_wait_forwards_nitpick_config(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """--nitpick must reach the strict PR-review prompt."""
        stage = PrReviewStage()
        ctx = make_ctx(config=SimpleNamespace(agent="claude", nitpick=True))
        item = make_work_item(issue=1, pr=1001, state="REVIEW_WAIT")

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.prompt_kwargs["include_nitpicks"] is True

    def test_review_wait_clears_stale_round_payload(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Submission clears ALL stale round results (M3 pattern).

        A failed later round can never replay an earlier round's verdict,
        threads, or address output in EVAL.
        """
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=4, pr=1001, state="REVIEW_WAIT")
        item.payload.update(
            {
                "review_verdict": _verdict("NOGO"),
                "review_text": "stale",
                "review_threads": [{"id": "t1"}],
                "raw_review_threads": [{"id": "raw-t1"}],
                "posted_thread_ids": ["t1"],
                "validation_result": "stale",
                "difficulty_tiers": "stale",
                "address_error": True,
                "address_output": "stale",
            }
        )

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        for key in (
            "review_verdict",
            "review_text",
            "review_threads",
            "raw_review_threads",
            "posted_thread_ids",
            "validation_result",
            "difficulty_tiers",
            "address_error",
            "address_output",
        ):
            assert key not in item.payload

    def test_validate_wait_requests_validation_job(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """VALIDATE_WAIT submits the prior-comment validation job."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="VALIDATE_WAIT")

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert result.on_done_state == "POST"
        assert result.job.descr == "validate"

    def test_validate_wait_skips_to_eval_when_review_failed(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A failed review job skips the dead round straight to EVAL."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="VALIDATE_WAIT")
        item.payload["review_failed"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "EVAL"
        assert "review_failed" not in item.payload

    def test_post_posts_threads_durably_and_routes_to_difficulty(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """POST durably posts surviving threads, then classifies difficulty."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(2, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload["review_threads"] = [{"id": "t1", "body": "fix"}, {"id": "t2", "body": "doc"}]
        item.payload["review_text"] = "Verdict: NOGO"

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "DIFFICULTY_WAIT"
        assert github.mutation_log == [("gh_pr_review_post", (1001, "COMMENT"))]
        assert item.payload["posted_thread_ids"] == ["thread-1001-0", "thread-1001-1"]
        assert item.payload["unresolved_auto"] == 2

    def test_post_with_zero_open_automation_threads_skips_to_eval(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """No open automation threads: nothing to address, go straight to EVAL."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="POST")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "EVAL"
        assert github.mutation_log == []  # no threads -> no post

    def test_difficulty_wait_requests_classification(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """DIFFICULTY_WAIT submits the comment-difficulty job."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="DIFFICULTY_WAIT")
        item.payload["review_threads"] = [{"id": "t1"}]

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.job.descr == "difficulty"
        assert result.on_done_state == "ADDRESS_WAIT"
        assert '"t1"' in result.job.prompt_kwargs["comments_json"]

    def test_address_fresh_pr_resumes_implementer(self, make_ctx: Any, make_work_item: Any) -> None:
        """Fresh-PR path resumes the implementer with the review feedback."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="ADDRESS_WAIT")
        item.worktree = "/tmp/wt"
        item.payload["review_verdict"] = _verdict("NOGO")
        item.payload["review_text"] = "fix the tests"
        item.payload["pr_review_round"] = 1

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.job.descr == "address"
        assert result.on_done_state == "PUSH_WAIT"
        assert result.job.prompt_builder is get_impl_resume_feedback_prompt
        assert result.job.prompt_kwargs == {
            "issue_number": 1,
            "prev_iteration": 1,
            "verdict": "NOGO",
            "review_text": "fix the tests",
        }

    def test_address_existing_pr_runs_address_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Existing-PR path runs the address-review session on the threads."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="ADDRESS_WAIT")
        item.worktree = "/tmp/wt"
        item.payload["existing_pr"] = True
        item.payload["review_threads"] = [{"id": "t1"}]
        item.payload["difficulty_tiers"] = "@ x.py Line 1 - simple - fix"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.job.descr == "address"
        assert result.job.prompt_builder is get_address_review_prompt
        assert result.job.prompt_kwargs["pr_number"] == 1001
        assert result.job.prompt_kwargs["todo_block"] == "@ x.py Line 1 - simple - fix"

    def test_push_wait_requests_commit_push(self, make_ctx: Any, make_work_item: Any) -> None:
        """PUSH_WAIT submits the commit+push job for the addressing changes."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="PUSH_WAIT")
        item.branch = "1-auto-impl"
        item.worktree = "/tmp/wt"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "commit_push"
        assert result.job.kwargs == {
            "issue_number": 1,
            "worktree_path": "/tmp/wt",
            "branch": "1-auto-impl",
            "agent": "claude",
        }
        assert result.on_done_state == "EVAL"

    def test_followup_wait_requests_follow_up(self, make_ctx: Any, make_work_item: Any) -> None:
        """FOLLOWUP_WAIT submits the follow-up job, then FINISH advances."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="FOLLOWUP_WAIT")

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert result.job.descr == "follow_up"
        assert result.on_done_state == "FINISH"

        item.state = "FINISH"
        outcome = stage.step(item, ctx)
        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.ADVANCE

    def test_unknown_state_fails(self, make_ctx: Any, make_work_item: Any) -> None:
        """An unknown state finishes failed instead of looping silently."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="BOGUS")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL


class TestEvalVerdicts:
    """EVAL: re-housed _evaluate_go_verdict semantics + the budget gate."""

    def test_go_with_zero_threads_marks_arms_and_advances(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Clean GO durably marks implementation-go THEN arms auto-merge.

        Durable-order oracle: mark before arm in the mutation_log, both
        recorded before the advancing outcome exists.
        """
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        ctx.config.enable_follow_up = False
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE
        assert github.mutation_log == [
            ("mark_pr_implementation_go", (1001,)),
            ("arm_auto_merge", (1001,)),
        ]
        assert item.attempts["pr_review_iter"] == 1  # real verdict counted

    def test_go_rechecks_human_threads_inside_arm_helper(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A human thread opened after EVAL's count prevents GO label and arm."""
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(0, 0, 0), (0, 0, 1)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL
        assert result.note == "human_blocked"
        assert github.mutation_log == [("gh_issue_comment", (1001,))]
        assert ("mark_pr_implementation_go", (1001,)) not in github.mutation_log
        assert ("arm_auto_merge", (1001,)) not in github.mutation_log
        assert "Automation stand-down" in github.comments[1001][0]

    def test_go_with_follow_up_enabled_continues_to_followup(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """GO with follow-up enabled writes labels then runs the follow-up step."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "FOLLOWUP_WAIT"
        assert github.mutation_log[0] == ("mark_pr_implementation_go", (1001,))

    def test_failed_mark_write_skips_arming(self, make_ctx: Any, make_work_item: Any) -> None:
        """If the implementation-go mark fails, auto-merge is NOT armed.

        Auto-merge armed on a PR without state:implementation-go would fail
        the pr-policy gate — the pair is ordered and the arm is gated on
        the mark's success (non-fatal beyond that).
        """

        class MarkFailsGitHub(FakeStageGitHub):
            def mark_pr_implementation_go(self, pr_number: int) -> None:
                raise RuntimeError("gh label failed")

        stage = PrReviewStage()
        github = MarkFailsGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")

        result = stage.step(item, ctx)  # must not raise

        assert isinstance(result, Continue)  # still proceeds (non-fatal)
        assert ("arm_auto_merge", (1001,)) not in github.mutation_log

    def test_failed_arm_write_is_non_fatal(self, make_ctx: Any, make_work_item: Any) -> None:
        """A failing arm_auto_merge write is swallowed; the GO still proceeds."""

        class ArmFailsGitHub(FakeStageGitHub):
            def arm_auto_merge(self, pr_number: int) -> None:
                raise RuntimeError("gh merge --auto failed")

        stage = PrReviewStage()
        github = ArmFailsGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")

        result = stage.step(item, ctx)  # must not raise

        assert isinstance(result, Continue)
        assert github.mutation_log == [("mark_pr_implementation_go", (1001,))]

    def test_go_with_human_thread_is_human_blocked_and_unlabeled(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """GO + open human thread -> HUMAN_BLOCKED: finish failed, NO labels.

        The PR stays unlabeled (neither implementation-go nor no-go nor
        skip): a human must act, automation may not resolve their thread.
        """
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 1)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL
        assert result.note == "human_blocked"
        # PR left unlabeled; the only durable write is the explanatory
        # stand-down comment (M3), posted BEFORE the failing outcome.
        assert github.mutation_log == [("gh_issue_comment", (1001,))]

    def test_go_with_automation_thread_downgrades_and_loops(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """GO + open automation thread is downgraded to NOGO: re-review."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(2, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "REVIEW_WAIT"
        # No GO labels while threads open; the downgraded round durably
        # records NO-GO (doc section 5: "NOGO verdict, before retry/regress").
        assert github.mutation_log == [("mark_pr_implementation_no_go", (1001,))]
        assert item.payload["prev_unresolved"] == 2

    def test_nogo_within_soft_budget_loops_to_re_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """NOGO within the soft budget loops back for a fresh review round."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(3, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("NOGO")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "REVIEW_WAIT"
        assert item.payload["pr_review_round"] == 1
        assert item.attempts["pr_review_iter"] == 1  # lifetime audit trail

    def test_ambiguous_counts_as_a_real_not_go_round(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """AMBIGUOUS is a real verdict: it burns a round and loops."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("AMBIGUOUS")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert item.attempts["pr_review_iter"] == 1

    def test_address_error_fails_back_without_burning_a_round(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A hard-failed address/push leg fails back agent_error, no round burned."""
        stage = PrReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["address_error"] = True
        item.payload["review_verdict"] = _verdict("NOGO")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FAIL_BACK
        assert result.note == "agent_error"
        assert item.attempts["pr_review_iter"] == 0  # no round burned
        assert github.mutation_log == []

    # Severity-aware GO gate tests (#1856)
    def test_go_with_only_minor_automation_thread_arms(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """GO with only minor/nitpick automation threads resolves and arms.

        This is the regression case from #1856: previously a GO with minor
        automation threads would deadlock to skip because `unresolved == 0`
        never held. Now severity filtering allows the GO to arm.
        """
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(0, 2, 0)])  # 0 blocking, 2 minor, 0 human
        ctx = make_ctx(github=github)
        ctx.config.enable_follow_up = False
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE
        # Should resolve the minor threads AND arm
        assert ("resolve_automation_threads", (1001,)) in github.mutation_log
        assert ("mark_pr_implementation_go", (1001,)) in github.mutation_log
        assert ("arm_auto_merge", (1001,)) in github.mutation_log

    def test_go_with_blocking_automation_thread_downgrades(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """GO with blocking automation threads downgrades to NOGO.

        Tightening check: blocking threads still block, minor threads don't.
        """
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(1, 0, 0)])  # 1 blocking, 0 minor, 0 human
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")
        item.payload["pr_review_round"] = 1

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "REVIEW_WAIT"
        # Should NOT resolve (no minor threads), should write no-go
        assert ("mark_pr_implementation_no_go", (1001,)) in github.mutation_log
        # resolve_automation_threads should NOT be called
        assert ("resolve_automation_threads", (1001,)) not in github.mutation_log

    def test_go_with_human_thread_still_blocks(self, make_ctx: Any, make_work_item: Any) -> None:
        """GO with human thread still hard-blocks (unregressed).

        Human threads are never filtered by severity.
        """
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(0, 0, 1)])  # 0 blocking, 0 minor, 1 human
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL
        # The underlying function is gh_issue_comment (not post_pr_comment)
        assert github.mutation_log[0][0] == "gh_issue_comment"

    def test_go_zero_threads_does_not_resolve(self, make_ctx: Any, make_work_item: Any) -> None:
        """GO with zero threads does not call resolve_automation_threads.

        Optimization: no minor threads → no resolve needed.
        """
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(0, 0, 0)])  # 0 blocking, 0 minor, 0 human
        ctx = make_ctx(github=github)
        ctx.config.enable_follow_up = False
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE
        # resolve should NOT be in the log
        assert ("resolve_automation_threads", (1001,)) not in github.mutation_log
        assert ("mark_pr_implementation_go", (1001,)) in github.mutation_log


class TestEvalErrorNoBurn:
    """The #1554 doctrine: ERROR burns no budget, stamps no labels."""

    def test_error_verdict_retries_without_burning(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """ERROR retries with zero label writes and burns no round."""
        stage = PrReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=8, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("ERROR")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.RETRY
        assert github.mutation_log == []  # labels untouched on ERROR
        assert item.attempts["pr_review_iter"] == 0  # no round burned
        assert item.payload.get("pr_review_round", 0) == 0
        assert item.payload["review_error_retries"] == 1  # bounded retry loop

    def test_missing_verdict_retries(self, make_ctx: Any, make_work_item: Any) -> None:
        """EVAL without a stored verdict retries instead of guessing."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=9, pr=1001, state="EVAL")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.RETRY
        assert item.attempts["pr_review_iter"] == 0
        assert item.payload["review_error_retries"] == 1

    def test_error_retry_cap_fails_back_to_implementation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Consecutive reviewer failures beyond the cap fail back agent_error."""
        stage = PrReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=10, pr=1001, state="EVAL")

        for expected_retry in range(1, REVIEW_ERROR_RETRY_CAP + 1):
            item.payload["review_verdict"] = _verdict("ERROR")
            outcome = stage.step(item, ctx)
            assert isinstance(outcome, StageOutcome)
            assert outcome.disposition == Disposition.RETRY
            assert item.payload["review_error_retries"] == expected_retry

        item.payload["review_verdict"] = _verdict("ERROR")
        outcome = stage.step(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.FAIL_BACK
        assert outcome.note == "agent_error"
        assert github.mutation_log == []  # labels stay untouched
        assert item.attempts["pr_review_iter"] == 0  # nothing ever burned

    def test_real_verdict_resets_error_retries(self, make_ctx: Any, make_work_item: Any) -> None:
        """Any real verdict resets the consecutive-failure counter."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(1, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=11, pr=1001, state="EVAL")
        item.payload["review_error_retries"] = REVIEW_ERROR_RETRY_CAP  # one from the cap
        item.payload["review_verdict"] = _verdict("NOGO")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert item.payload["review_error_retries"] == 0


class TestProgressAwareBudget:
    """Soft cap 3; rounds 4-6 admitted ONLY while threads strictly decrease."""

    def _eval_round(self, stage: Any, item: Any, ctx: Any, verdict: str = "NOGO") -> Any:
        """Run one EVAL with a fresh verdict (state machine loop shortcut)."""
        item.payload["review_verdict"] = _verdict(verdict)
        item.state = "EVAL"
        return stage.step(item, ctx)

    def test_non_decreasing_at_round_four_exhausts(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Plateaued threads at the soft cap: round 4 is refused -> state:skip.

        Durable-order oracle: the state:skip write is in the mutation_log
        before the SKIP outcome exists.
        """
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(3, 0)])  # plateau at 3 threads
        ctx = make_ctx(github=github)
        item = make_work_item(issue=12, pr=1001, state="EVAL")
        assert stage.on_enter(item, ctx) is None

        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # round 1
        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # round 2
        outcome = self._eval_round(stage, item, ctx)  # round 3: no progress

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.SKIP
        assert outcome.note == "exhaustion"
        # Every real NOGO round durably records NO-GO before its retry /
        # regress (M2); the exhaustion's state:skip write comes LAST.
        assert github.mutation_log == [
            ("mark_pr_implementation_no_go", (1001,)),
            ("mark_pr_implementation_no_go", (1001,)),
            ("mark_pr_implementation_no_go", (1001,)),
            ("gh_issue_add_labels", (12, (STATE_SKIP,))),
        ]
        assert item.payload["pr_review_round"] == 3

    def test_decreasing_threads_earn_extension_rounds(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Strictly decreasing threads admit rounds 4+ until the plateau."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(5, 0), (3, 0), (2, 0), (1, 0), (1, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=13, pr=1001, state="EVAL")
        assert stage.on_enter(item, ctx) is None

        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r1: 5 open
        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r2: 3 open
        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r3: 2<3 -> r4 earned
        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r4: 1<2 -> r5 earned
        outcome = self._eval_round(stage, item, ctx)  # r5: 1==1 plateau

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.SKIP
        assert item.payload["pr_review_round"] == 5
        assert item.attempts["pr_review_iter"] == 5  # lifetime audit trail
        assert item.attempts["pr_review_hard"] == 2  # extension rounds 4 and 5
        assert github.mutation_log[-1] == ("gh_issue_add_labels", (13, (STATE_SKIP,)))

    def test_hard_cap_stops_even_with_progress(self, make_ctx: Any, make_work_item: Any) -> None:
        """Round 6 is the absolute ceiling even while threads keep decreasing."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(9, 0), (8, 0), (7, 0), (6, 0), (5, 0), (4, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=14, pr=1001, state="EVAL")
        assert stage.on_enter(item, ctx) is None

        outcomes = [self._eval_round(stage, item, ctx) for _ in range(6)]

        assert all(isinstance(o, Continue) for o in outcomes[:5])  # rounds 1-5 loop
        final = outcomes[5]
        assert isinstance(final, StageOutcome)
        assert final.disposition == Disposition.SKIP  # 6 == hard cap: stop
        assert item.payload["pr_review_round"] == 6

    def test_budgets_come_from_routes(self, make_ctx: Any) -> None:
        """The soft/hard budgets are ROUTES data, not stage constants."""
        ctx = make_ctx()

        assert ctx.budget("pr_review_iter") == 3
        assert ctx.budget("pr_review_hard") == 6

    def test_budget_override_changes_the_cap(self, make_ctx: Any, make_work_item: Any) -> None:
        """An injected budget_fn (ROUTES stand-in) moves the exhaustion point."""
        from dataclasses import replace

        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(3, 0)])
        ctx = replace(make_ctx(github=github), budget_fn=lambda name: 1)
        item = make_work_item(issue=15, pr=1001, state="EVAL")
        assert stage.on_enter(item, ctx) is None

        outcome = self._eval_round(stage, item, ctx)  # round 1 == soft cap 1

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.SKIP  # cap moved by ROUTES data

    def test_skip_label_write_is_non_fatal(self, make_ctx: Any, make_work_item: Any) -> None:
        """A failing state:skip write never turns the SKIP into a crash."""

        class AddFailsGitHub(FakeStageGitHub):
            def add_labels(self, issue_number: int, labels: list[str]) -> None:
                raise RuntimeError("gh add failed")

        stage = PrReviewStage()
        ctx = make_ctx(github=AddFailsGitHub(unresolved=[(3, 0)]))
        item = make_work_item(issue=16, pr=1001, state="EVAL")
        item.payload["pr_review_round"] = 5  # this round is 6/6
        item.payload["review_verdict"] = _verdict("NOGO")

        result = stage.step(item, ctx)  # must not raise

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.SKIP


class TestPrReviewOnJobDone:
    """on_job_done payload handling (state still at the WAIT state)."""

    def test_review_verdict_and_text_stored(self, make_ctx: Any, make_work_item: Any) -> None:
        """The parsed verdict and its raw review text land on the payload."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="REVIEW_WAIT")
        verdict = _verdict("GO")

        stage.on_job_done(item, JobResult(ok=True, value=verdict), ctx)

        assert item.payload["review_verdict"] == verdict
        assert item.payload["review_text"] == verdict.raw

    def test_failed_review_job_flags_the_dead_round(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A failed review job stores no verdict and flags review_failed."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="REVIEW_WAIT")

        stage.on_job_done(item, JobResult(ok=False, error="reviewer crashed"), ctx)

        assert "review_verdict" not in item.payload
        assert item.payload["review_failed"] is True

    def test_validation_and_difficulty_results_stored(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Validation and difficulty outputs land on the payload."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="VALIDATE_WAIT")

        stage.on_job_done(item, JobResult(ok=True, value='{"unaddressed": []}'), ctx)
        assert item.payload["validation_result"] == '{"unaddressed": []}'

        item.state = "DIFFICULTY_WAIT"
        stage.on_job_done(item, JobResult(ok=True, value="tiers"), ctx)
        assert item.payload["difficulty_tiers"] == "tiers"

    def test_failed_address_or_push_flags_address_error(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Failed address/push jobs flag address_error for EVAL's fail-back."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="ADDRESS_WAIT")

        stage.on_job_done(item, JobResult(ok=False, error="agent crashed"), ctx)
        address_error = item.payload.pop("address_error")
        assert address_error is True

        item.state = "PUSH_WAIT"
        stage.on_job_done(item, JobResult(ok=False, error="push rejected"), ctx)
        assert item.payload["address_error"] is True

    def test_followup_result_is_intentionally_silent(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """FOLLOWUP output is a side effect: no payload key is written."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="FOLLOWUP_WAIT")
        snapshot = dict(item.payload)

        stage.on_job_done(item, JobResult(ok=True, value="filed follow-ups"), ctx)

        assert item.payload == snapshot


class TestFullWalks:
    """Full pool-driven walks of the whole stage (canonical FakeWorkerPool)."""

    def test_nogo_round_then_clean_go_walk(self, make_ctx: Any, make_work_item: Any) -> None:
        """ENTER -> NOGO round (address leg) -> GO round -> mark+arm -> follow-up.

        Round 1: review NOGO, 2 automation threads open -> difficulty ->
        address -> push -> EVAL loops. Round 2: review GO, all threads
        resolved -> mark + arm [durable] -> follow-up -> ADVANCE.
        """
        stage = PrReviewStage()
        # POST calls count_unresolved_threads once per round; EVAL calls the
        # new by_severity method once per round. Two rounds => one entry each
        # per round. Round 1 has 2 open blocking threads (NOGO address leg);
        # round 2 is clean (GO: skips difficulty/address, arms).
        github = FakeStageGitHub(
            unresolved=[(2, 0), (0, 0)],
            by_severity=[(2, 0, 0), (0, 0, 0)],
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=21, pr=1001, state="ENTER")
        item.branch = "21-auto-impl"
        item.worktree = "/tmp/wt21"

        pool = FakeWorkerPool()
        pool.script(
            JobResult(ok=True, value=_verdict("NOGO")),  # review round 1
            JobResult(ok=True, value='{"unaddressed": []}'),  # validate round 1
            JobResult(ok=True, value="tier list"),  # difficulty
            JobResult(ok=True, value="addressed"),  # address
            JobResult(ok=True, value=True),  # push
            JobResult(ok=True, value=_verdict("GO")),  # review round 2
            JobResult(ok=True, value='{"unaddressed": []}'),  # validate round 2
            JobResult(ok=True, value="follow-ups filed"),  # follow-up
        )

        outcome = _drive(stage, item, ctx, pool)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.ADVANCE
        assert [h.job.descr for h in pool.submitted] == [
            "review",
            "validate",
            "difficulty",
            "address",
            "push_fixes",
            "review",
            "validate",
            "follow_up",
        ]
        assert item.attempts["pr_review_iter"] == 2  # two real rounds
        assert github.mutation_log == [
            ("mark_pr_implementation_no_go", (1001,)),  # round 1 NOGO recorded (M2)
            ("mark_pr_implementation_go", (1001,)),
            ("arm_auto_merge", (1001,)),
        ]

    def test_exhaustion_walk_applies_skip(self, make_ctx: Any, make_work_item: Any) -> None:
        """Three plateaued NOGO rounds exhaust the soft budget -> state:skip."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(3, 0)])  # plateau forever
        ctx = make_ctx(github=github)
        item = make_work_item(issue=22, pr=1001, state="ENTER")
        item.worktree = "/tmp/wt22"

        pool = FakeWorkerPool()
        round_jobs = [
            JobResult(ok=True, value=_verdict("NOGO")),  # review
            JobResult(ok=True, value='{"unaddressed": []}'),  # validate
            JobResult(ok=True, value="tier list"),  # difficulty
            JobResult(ok=True, value="addressed"),  # address
            JobResult(ok=True, value=True),  # push
        ]
        pool.script(*(round_jobs * 3))

        outcome = _drive(stage, item, ctx, pool)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.SKIP
        assert outcome.note == "exhaustion"
        assert item.attempts["pr_review_iter"] == 3
        assert github.mutation_log[-1] == ("gh_issue_add_labels", (22, (STATE_SKIP,)))

    def test_reviewer_error_walk_burns_nothing(self, make_ctx: Any, make_work_item: Any) -> None:
        """A failed review job walks straight to EVAL's ERROR path: RETRY.

        The dead round submits no validate/difficulty/address jobs and burns
        no budget (#1554 doctrine).
        """
        stage = PrReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=23, pr=1001, state="ENTER")

        pool = FakeWorkerPool()
        pool.script(JobResult(ok=False, error="reviewer crashed"))  # review job fails

        outcome = _drive(stage, item, ctx, pool)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.RETRY
        assert [h.job.descr for h in pool.submitted] == ["review"]  # dead round short-circuits
        assert item.attempts["pr_review_iter"] == 0
        assert github.mutation_log == []


class TestNoGoLabel:
    """M2: state:implementation-no-go is durably written on NOGO rounds."""

    def test_no_go_written_on_every_nogo_round(self, make_ctx: Any, make_work_item: Any) -> None:
        """Two NOGO rounds record NO-GO twice (per-round, before each loop)."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(3, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=30, pr=1001, state="EVAL")
        assert stage.on_enter(item, ctx) is None

        for _ in range(2):
            item.payload["review_verdict"] = _verdict("NOGO")
            item.state = "EVAL"
            assert isinstance(stage.step(item, ctx), Continue)

        assert github.mutation_log == [
            ("mark_pr_implementation_no_go", (1001,)),
            ("mark_pr_implementation_no_go", (1001,)),
        ]

    def test_no_go_write_failure_is_non_fatal(self, make_ctx: Any, make_work_item: Any) -> None:
        """A failing NO-GO write never turns the round into a crash."""

        class NoGoFailsGitHub(FakeStageGitHub):
            def mark_pr_implementation_no_go(self, pr_number: int) -> None:
                raise RuntimeError("gh label failed")

        stage = PrReviewStage()
        ctx = make_ctx(github=NoGoFailsGitHub(unresolved=[(3, 0)]))
        item = make_work_item(issue=31, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("NOGO")

        result = stage.step(item, ctx)  # must not raise

        assert isinstance(result, Continue)
        assert result.next_state == "REVIEW_WAIT"

    def test_error_round_never_writes_no_go(self, make_ctx: Any, make_work_item: Any) -> None:
        """ERROR is not a verdict: no NO-GO label, no label writes at all."""
        stage = PrReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=32, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("ERROR")

        stage.step(item, ctx)

        assert github.mutation_log == []


class TestHumanBlockedComment:
    """M3: HUMAN_BLOCKED posts a durable stand-down comment before finishing."""

    def test_comment_explains_blockage_before_finish_fail(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The comment names the blocking human threads and the stand-down.

        Journal-order oracle: the comment is the ONLY mutation and exists
        in the mutation_log before the FINISH_FAIL outcome is returned.
        """
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 2)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=33, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL
        assert result.note == "human_blocked"
        assert github.mutation_log == [("gh_issue_comment", (1001,))]
        body = github.comments[1001][0]
        assert "2 unresolved review thread(s) opened by a human" in body
        assert "standing down" in body
        assert "state:implementation-go" in body  # explains the unlabeled state

    def test_comment_failure_is_non_fatal(self, make_ctx: Any, make_work_item: Any) -> None:
        """A failing comment write still finishes failed (never crashes)."""

        class CommentFailsGitHub(FakeStageGitHub):
            def post_pr_comment(self, pr_number: int, body: str) -> None:
                raise RuntimeError("gh comment failed")

        stage = PrReviewStage()
        ctx = make_ctx(github=CommentFailsGitHub(unresolved=[(0, 1)]))
        item = make_work_item(issue=34, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")

        result = stage.step(item, ctx)  # must not raise

        assert isinstance(result, StageOutcome)
        assert result.note == "human_blocked"


class TestRealCommitGate:
    """M4 (#1575): a no-commit address turn is never treated as addressed."""

    def test_push_result_records_the_no_commit_flag(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """commit_push value False -> push_no_commit True; True -> False."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=40, pr=1001, state="PUSH_WAIT")

        stage.on_job_done(item, JobResult(ok=True, value=False), ctx)
        assert item.payload["push_no_commit"] is True

        stage.on_job_done(item, JobResult(ok=True, value=True), ctx)
        assert item.payload["push_no_commit"] is False

    def test_first_no_commit_retries_address_with_directive(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The first no-commit turn retries the address once, no round burned."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=41, pr=1001, state="EVAL")
        threads = [{"id": "t1", "path": "x.py", "line": 3, "body": "fix the bug"}]
        item.payload["review_verdict"] = _verdict("NOGO")
        item.payload["review_threads"] = threads
        item.payload["push_no_commit"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "ADDRESS_WAIT"
        assert item.payload["unaddressed_findings"] == threads
        assert item.payload["no_commit_retry_done"] is True
        assert item.attempts["pr_review_iter"] == 0  # no round burned by the retry

    def test_first_no_commit_retry_uses_raw_review_threads_not_survivors(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The retry directive uses reviewer text, not validator-synthesized survivors."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=45, pr=1001, state="EVAL")
        raw_threads = [{"thread_id": "t1", "path": "x.py", "line": 3, "body": "reviewer text"}]
        surviving_threads = [
            {
                "path": "y.py",
                "line": 7,
                "body": "Reopened (prior round, still unaddressed): synthesized text",
            }
        ]
        item.payload["review_verdict"] = _verdict("NOGO")
        item.payload["raw_review_threads"] = raw_threads
        item.payload["review_threads"] = surviving_threads
        item.payload["push_no_commit"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "ADDRESS_WAIT"
        assert item.payload["unaddressed_findings"] == raw_threads
        assert item.payload["no_commit_retry_done"] is True
        assert item.attempts["pr_review_iter"] == 0

    def test_retry_address_job_carries_the_directive_findings(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The existing-PR retry address prompt carries unaddressed_findings.

        get_address_review_prompt renders them via build_unaddressed_directive
        (reused, not reimplemented) — asserted end-to-end on the built prompt.
        """
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=42, pr=1001, state="ADDRESS_WAIT")
        item.worktree = "/tmp/wt"
        item.payload["existing_pr"] = True
        threads = [{"id": "t1", "path": "x.py", "line": 3, "body": "fix the bug"}]
        item.payload["review_threads"] = threads
        item.payload["unaddressed_findings"] = threads

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.prompt_kwargs["unaddressed_findings"] == threads
        prompt = result.job.prompt_builder(**result.job.prompt_kwargs)
        assert "Make sure to handle x.py:3" in prompt  # the #1575 directive block
        assert "NO commit on the previous turn" in prompt

    def test_no_commit_retry_address_error_consumes_directive_without_burning_round(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A hard-failed no-commit retry is agent_error, not stale carry."""
        stage = PrReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=45, pr=1001, state="EVAL")
        threads = [{"id": "t1", "path": "x.py", "line": 3, "body": "fix the bug"}]
        item.payload["review_verdict"] = _verdict("NOGO")
        item.payload["address_error"] = True
        item.payload["push_no_commit"] = True
        item.payload["no_commit_retry_done"] = True
        item.payload["unaddressed_findings"] = threads
        item.payload["pr_review_round"] = 2
        item.attempts["pr_review_iter"] = 2

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FAIL_BACK
        assert result.note == "agent_error"
        assert item.payload["agent_error_failback"] is True
        assert item.payload["pr_review_round"] == 2
        assert item.attempts["pr_review_iter"] == 2
        assert "address_error" not in item.payload
        assert "push_no_commit" not in item.payload
        assert "no_commit_retry_done" not in item.payload
        assert "unaddressed_findings" not in item.payload
        assert github.mutation_log == []

    def test_second_no_commit_counts_as_an_unaddressed_round(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A second consecutive no-commit turn burns its round normally."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(3, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=43, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("NOGO")
        item.payload["push_no_commit"] = True
        item.payload["no_commit_retry_done"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "REVIEW_WAIT"  # evaluated, not re-retried
        assert item.attempts["pr_review_iter"] == 1  # the round was burned

    def test_real_commit_clears_the_retry_directive(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A push with a real commit spends/clears the retry directive."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(1, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=44, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("NOGO")
        item.payload["push_no_commit"] = False  # commit_push produced a commit
        item.payload["no_commit_retry_done"] = True
        item.payload["unaddressed_findings"] = [{"id": "t1"}]

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert "no_commit_retry_done" not in item.payload
        assert "unaddressed_findings" not in item.payload


class TestSurvivingThreads:
    """m1: POST posts only threads that survive the validation verdict."""

    def test_wont_fix_threads_are_dropped(self) -> None:
        """Threads re-raising a wont_fix (by-design) finding are not posted."""
        threads = [
            {"thread_id": "t1", "body": "real bug"},
            {"thread_id": "t2", "body": "by-design recurrence"},
        ]
        validation = '{"unaddressed": [], "wont_fix": [{"thread_id": "t2", "reason": "abstract"}]}'

        surviving = _surviving_threads(threads, validation)

        assert [t["thread_id"] for t in surviving] == ["t1"]

    def test_unaddressed_prior_findings_are_reopened(self) -> None:
        """Unaddressed prior findings are re-opened as new postable threads."""
        validation = (
            '{"unaddressed": [{"thread_id": "t9", "path": "y.py", "line": 7,'
            ' "original_body": "guard the None", "detail": "still no None guard"}],'
            ' "wont_fix": []}'
        )

        surviving = _surviving_threads([{"thread_id": "t1", "body": "new"}], validation)

        assert len(surviving) == 2
        reopened = surviving[1]
        assert reopened["path"] == "y.py"
        assert reopened["line"] == 7
        assert "still no None guard" in reopened["body"]

    def test_reviewer_reraise_is_not_duplicated(self) -> None:
        """A finding the reviewer already re-raised is not re-opened twice."""
        validation = '{"unaddressed": [{"thread_id": "t1", "detail": "x"}], "wont_fix": []}'

        surviving = _surviving_threads([{"thread_id": "t1", "body": "re-raised"}], validation)

        assert len(surviving) == 1

    def test_unparseable_validation_fails_open(self) -> None:
        """Garbage validator output filters nothing (legacy fail-open)."""
        threads = [{"thread_id": "t1", "body": "keep me"}]

        assert _surviving_threads(threads, "not json at all") == threads
        assert _surviving_threads(threads, None) == threads
        assert _surviving_threads(threads, "") == threads

    def test_fenced_json_block_is_parsed_last_wins(self) -> None:
        """The validator's LAST fenced JSON block is the verdict (legacy rule)."""
        validation = (
            "Reasoning prose...\n```json\n"
            '{"unaddressed": [], "wont_fix": []}\n```\n'
            "More prose, corrected verdict:\n```json\n"
            '{"unaddressed": [], "wont_fix": [{"thread_id": "t1", "reason": "by design"}]}\n```\n'
        )

        surviving = _surviving_threads([{"thread_id": "t1", "body": "x"}], validation)

        assert surviving == []

    def test_post_filters_through_validation_result(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """POST posts the SURVIVING set and updates the round's thread list."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(1, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=50, pr=1001, state="POST")
        item.payload["review_threads"] = [
            {"thread_id": "t1", "body": "real bug"},
            {"thread_id": "t2", "body": "by-design"},
        ]
        item.payload["validation_result"] = (
            '{"unaddressed": [{"thread_id": "t9", "path": "y.py", "line": 7,'
            ' "detail": "still missing"}],'
            ' "wont_fix": [{"thread_id": "t2", "reason": "documented"}]}'
        )
        item.payload["review_text"] = "Verdict: NOGO"

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert github.mutation_log == [("gh_pr_review_post", (1001, "COMMENT"))]
        posted = github.reviews[1001][0]["comments"]
        assert item.payload["raw_review_threads"] == [
            {"thread_id": "t1", "body": "real bug"},
            {"thread_id": "t2", "body": "by-design"},
        ]
        assert [t.get("thread_id") for t in item.payload["review_threads"]] == ["t1", None]
        assert [t.get("thread_id") for t in posted] == ["t1", None]
        assert posted[1]["body"].startswith("Reopened (prior round, still unaddressed):")


class TestProgressCountsAutomationOnly:
    """m2: human-resolved threads never earn extension rounds (legacy parity)."""

    def _eval_round(self, stage: Any, item: Any, ctx: Any) -> Any:
        item.payload["review_verdict"] = _verdict("NOGO")
        item.state = "EVAL"
        return stage.step(item, ctx)

    def test_human_resolution_earns_no_extension(self, make_ctx: Any, make_work_item: Any) -> None:
        """Total unresolved decreases only via HUMAN threads: no round 4.

        5->4->3 total would have earned extensions under a total-count
        metric, but the automation count plateaus at 3 — the extension gate
        must refuse round 4 and exhaust at the soft cap.
        """
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(3, 2), (3, 1), (3, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=51, pr=1001, state="EVAL")
        assert stage.on_enter(item, ctx) is None

        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r1
        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r2
        outcome = self._eval_round(stage, item, ctx)  # r3: automation plateau

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.SKIP
        assert outcome.note == "exhaustion"

    def test_automation_decrease_still_earns_extension(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Control: the same walk with a decreasing AUTOMATION count extends."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(5, 2), (4, 2), (3, 2), (3, 2)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=52, pr=1001, state="EVAL")
        assert stage.on_enter(item, ctx) is None

        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r1: 5 auto
        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r2: 4 auto
        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r3: 3<4 -> r4
        outcome = self._eval_round(stage, item, ctx)  # r4: 3==3 plateau

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.SKIP


class TestAgentErrorFailbackFlag:
    """M1 (pr_review side): every agent_error fail-back flags the re-entry."""

    def test_error_cap_failback_sets_the_flag(self, make_ctx: Any, make_work_item: Any) -> None:
        """The reviewer-error-cap fail-back marks agent_error_failback."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=60, pr=1001, state="EVAL")
        item.payload["review_error_retries"] = REVIEW_ERROR_RETRY_CAP
        item.payload["review_verdict"] = _verdict("ERROR")

        outcome = stage.step(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.note == "agent_error"
        assert item.payload["agent_error_failback"] is True

    def test_address_error_failback_sets_the_flag(self, make_ctx: Any, make_work_item: Any) -> None:
        """The address-failure fail-back marks agent_error_failback."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=61, pr=1001, state="EVAL")
        item.payload["address_error"] = True

        outcome = stage.step(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.note == "agent_error"
        assert item.payload["agent_error_failback"] is True

    def test_missing_worktree_for_address_fails_closed(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """No worktree: the address job must never run in the shared checkout."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=62, pr=1001, state="ADDRESS_WAIT")
        item.payload["existing_pr"] = True
        assert item.worktree == ""  # the dangerous configuration

        outcome = stage.step(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.FAIL_BACK
        assert outcome.note == "agent_error"
        assert item.payload["agent_error_failback"] is True

    def test_on_enter_new_cycle_resets_error_retries(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A fresh implementation cycle restarts the reviewer-error streak."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=63, pr=1001, state="ENTER")
        item.attempts["implement"] = 1  # GATE consumed a fail-back re-entry
        item.payload["pr_review_cycle"] = 0
        item.payload["review_error_retries"] = REVIEW_ERROR_RETRY_CAP + 1

        stage.on_enter(item, ctx)

        assert "review_error_retries" not in item.payload
