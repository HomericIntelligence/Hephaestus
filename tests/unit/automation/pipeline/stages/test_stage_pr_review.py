"""Tests for the PR-review stage (doc section "5. pr_review")."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from hephaestus.automation.pipeline.jobs import AgentJob, CompactJob, GitJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import (
    Continue,
    JobRequest,
    ProcessThreadResolutionResult,
    StageOutcome,
)
from hephaestus.automation.pipeline.stages.pr_review import (
    ADOPT_WORKTREE_WAIT,
    REVIEW_CHECKOUT_WAIT,
    REVIEW_ERROR_RETRY_CAP,
    PrReviewStage,
    _handled_process_receipts,
    _is_process_thread_receipt,
    _surviving_threads,
)
from hephaestus.automation.pipeline.work_item import ItemKind
from hephaestus.automation.prompts.address_review import get_address_review_prompt
from hephaestus.automation.prompts.implementation import get_impl_resume_feedback_prompt
from hephaestus.automation.review_audit import ReviewAudit, parse_review_audit
from hephaestus.automation.state_labels import STATE_SKIP
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _valid_audit() -> ReviewAudit:
    """Build a valid structured audit for stage fixtures."""
    return ReviewAudit(
        grade="A",
        summary="fixture audit",
        findings=(),
        raw_feedback="fixture review text",
        valid=True,
    )


def _invalid_audit() -> ReviewAudit:
    """Build a malformed structured audit for failure-path fixtures."""
    return ReviewAudit(
        grade=None,
        summary="",
        findings=(),
        raw_feedback="fixture review text",
        valid=False,
    )


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
            if isinstance(result.job, GitJob) and result.job.op == "verify_pr_review_checkout":
                stage.on_job_done(
                    item,
                    JobResult(ok=True, value={"ready": True, "diff": "checkout diff"}),
                    ctx,
                )
                item.state = result.on_done_state
                continue
            pool.submit(result.job, result.on_done_state)  # type: ignore[arg-type]
            _handle, job_result = pool.completion_q.get_nowait()
            assert not job_result.interrupted  # on_job_done contract precondition
            stage.on_job_done(item, job_result, ctx)
            item.state = result.on_done_state
            continue
        return result
    raise AssertionError("stage driver did not terminate")


def _dispatch_review(stage: Any, item: Any, ctx: Any) -> JobRequest:
    """Cross the synchronous test double through the checkout barrier."""
    barrier = stage.step(item, ctx)
    assert isinstance(barrier, JobRequest)
    assert isinstance(barrier.job, GitJob)
    stage.on_job_done(
        item,
        JobResult(ok=True, value={"ready": True, "diff": "checkout diff"}),
        ctx,
    )
    item.state = barrier.on_done_state
    review = stage.step(item, ctx)
    assert isinstance(review, JobRequest)
    assert isinstance(review.job, AgentJob)
    return review


class TestPrReviewStageOnEnter:
    """on_enter cycle-relative counter reset (attempts are per-lifetime)."""

    def test_on_enter_checks_only_the_external_arm_boundary(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Context is refreshed later, immediately before agent dispatch."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            pr_review_context={
                "pr_diff": "diff --git a/example.py b/example.py\n+new line\n",
                "pr_description": "Closes #1\n\nReview this implementation.",
            }
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        assert "pr_diff" not in item.payload
        assert github.mutation_log == []

    def test_on_enter_defers_context_read_until_review_dispatch(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A stale context does not block ingress before the job boundary."""

        class ContextUnavailableGitHub(FakeStageGitHub):
            def pr_review_context(self, pr_number: int) -> dict[str, str] | None:
                del pr_number
                return None

        stage = PrReviewStage()
        ctx = make_ctx(github=ContextUnavailableGitHub())
        item = make_work_item(issue=1, pr=1001, state="ENTER")

        assert stage.on_enter(item, ctx) is None

    def test_on_enter_confirms_an_unarmed_pr_without_mutation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """PR-review has no authority to defer auto-merge."""
        stage = PrReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        assert github.mutation_log == []

    def test_on_enter_dry_run_keeps_the_read_only_boundary(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Dry-run leaves no stage-side mutation branch around the accessor."""
        stage = PrReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github, dry_run=True)
        item = make_work_item(issue=1, pr=1001, kind=ItemKind.PR, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        assert github.mutation_log == []

    def test_on_enter_blocks_an_existing_external_auto_merge_request(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An operator-owned arm is never disabled or relabeled by review."""
        stage = PrReviewStage()
        ctx = make_ctx(
            github=FakeStageGitHub(
                pr_state={"state": "OPEN", "headRefOid": "a" * 40, "autoMergeRequest": {}}
            )
        )
        item = make_work_item(issue=1, pr=1001, state="ENTER")

        assert stage.on_enter(item, ctx) == StageOutcome(
            Disposition.BLOCKED, "auto_merge_already_armed"
        )

    def test_on_enter_rejects_a_partial_pr_state_without_mutation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Review ingress needs an explicit null auto-merge field before it can mutate."""
        stage = PrReviewStage()
        github = FakeStageGitHub(pr_state={"state": "OPEN", "headRefOid": "a" * 40})
        item = make_work_item(issue=1, pr=1001, state="ENTER")

        assert stage.on_enter(item, make_ctx(github=github)) == StageOutcome(
            Disposition.FINISH_FAIL, "pr_state_unverified"
        )
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

        stage.on_enter(item, ctx)

        assert item.payload["pr_review_cycle"] == 2
        assert item.payload["pr_review_round"] == 0  # cycle 2 gets a full budget

    def test_on_enter_same_cycle_keeps_round(self, make_ctx: Any, make_work_item: Any) -> None:
        """Same-cycle re-entry (e.g. the ERROR-path RETRY) keeps the round count."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=2, pr=1001, state="ENTER")
        item.payload["pr_review_cycle"] = 0
        item.payload["pr_review_round"] = 2

        stage.on_enter(item, ctx)

        assert item.payload["pr_review_round"] == 2  # progress preserved

    def test_on_enter_double_call_rechecks_unarmed_state_without_mutation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A literal double on_enter preserves state while rechecking containment."""
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

    def test_review_wait_dispatches_to_handler(self, make_ctx: Any, make_work_item: Any) -> None:
        """REVIEW_WAIT routes through the dedicated state handler."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="REVIEW_WAIT")
        expected = StageOutcome(Disposition.ADVANCE, "dispatched")

        with patch.object(stage, "_review_wait", create=True, return_value=expected) as mock:
            result = stage.step(item, ctx)

        assert result == expected
        mock.assert_called_once_with(item, ctx)

    def test_review_refreshes_head_snapshot_before_dispatching_agent(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The reviewer may run only after a clean checkout/head barrier."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            pr_review_context={
                "pr_diff": "diff --git a/a.py b/a.py\n+new\n",
                "pr_description": "Closes #1",
                "pr_head_sha": "a" * 40,
            }
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="REVIEW_WAIT")
        item.worktree = "/tmp/repo/review-worktree"
        item.branch = "review-branch"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "verify_pr_review_checkout"
        assert result.job.kwargs["expected_head_sha"] == "a" * 40
        assert result.job.kwargs["base_branch"] == "main"
        assert result.on_done_state == REVIEW_CHECKOUT_WAIT
        assert "reviewed_pr_head_sha" not in item.payload

    def test_checkout_barrier_renews_the_proof_for_an_unchanged_head(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A new review pass gets a distinct proof even when its SHA is unchanged."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            pr_review_context={
                "pr_diff": "diff --git a/a.py b/a.py\n+new\n",
                "pr_description": "Closes #1",
                "pr_head_sha": "a" * 40,
            }
        )
        item = make_work_item(issue=1, pr=1001, state="REVIEW_WAIT")
        item.worktree = "/tmp/repo/review-worktree"
        item.branch = "review-branch"
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "reviewed_pr_proof_generation": 7,
            }
        )

        _dispatch_review(stage, item, make_ctx(github=github))

        assert item.payload["reviewed_pr_head_sha"] == "a" * 40
        assert item.payload["reviewed_pr_proof_generation"] == 8

    def test_review_uses_checkout_diff_not_mutable_remote_context(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An ABA remote read cannot submit B's diff with checkout proof for A."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            pr_review_context={
                "pr_diff": "remote diff for B",
                "pr_description": "Closes #1",
                "pr_head_sha": "a" * 40,
                "pr_base_branch": "main",
            }
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="REVIEW_WAIT")
        item.worktree = "/tmp/repo/review-worktree"
        item.branch = "review-branch"

        barrier = stage.step(item, ctx)
        assert isinstance(barrier, JobRequest)
        stage.on_job_done(
            item,
            JobResult(ok=True, value={"ready": True, "diff": "checkout diff for A"}),
            ctx,
        )
        item.state = barrier.on_done_state
        review = stage.step(item, ctx)

        assert isinstance(review, JobRequest)
        assert isinstance(review.job, AgentJob)
        assert review.job.prompt_kwargs["pr_diff"] == "checkout diff for A"

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

    def test_direct_pr_without_worktree_adopts_before_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A direct PR adopts its exact branch without entering implementation."""
        stage = PrReviewStage()
        github = FakeStageGitHub(pr_head_branch="review-pr")
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, kind=ItemKind.PR, state="ENTER")
        assert item.worktree == ""

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "create_worktree"
        assert result.on_done_state == ADOPT_WORKTREE_WAIT
        assert result.job.kwargs == {
            "issue_number": 1,
            "branch_name": "review-pr",
            "refresh_base": False,
            "isolated": True,
            "sync_to_remote": True,
            "pr_number": 1001,
            "repo_root": str(ctx.paths.repo_root),
        }
        assert item.payload["existing_pr"] is True
        assert item.payload["direct_pr_worktree_pending"] is True
        assert "agent_error_failback" not in item.payload

    def test_issue_seeded_existing_pr_without_worktree_adopts_before_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Drive-green issue seeds adopt their existing PR checkout too."""
        stage = PrReviewStage()
        ctx = make_ctx(github=FakeStageGitHub(pr_head_branch="review-pr"))
        item = make_work_item(issue=1, pr=1001, kind=ItemKind.ISSUE, state="ENTER")
        item.payload["existing_pr"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "create_worktree"
        assert result.job.kwargs["isolated"] is True

    def test_direct_pr_adoption_completion_enters_review_wait(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Completion is recorded before the coordinator changes WAIT state."""
        stage = PrReviewStage()
        ctx = make_ctx(github=FakeStageGitHub(pr_head_branch="review-pr"))
        item = make_work_item(
            issue=1,
            pr=1001,
            kind=ItemKind.PR,
            state="ENTER",
        )
        request = stage.step(item, ctx)
        assert isinstance(request, JobRequest)

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"path": "/tmp/review-pr", "dirty": False}),
            ctx,
        )
        # ``Coordinator._handle_completion`` assigns on_done_state only after
        # this callback, so mirror its durable ordering exactly.
        item.state = ADOPT_WORKTREE_WAIT
        result = stage.step(item, ctx)

        assert result == Continue(next_state="REVIEW_WAIT")
        assert item.worktree == "/tmp/review-pr"
        assert item.payload["direct_pr_worktree"] == "/tmp/review-pr"
        assert "direct_pr_worktree_pending" not in item.payload

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
        """REVIEW_WAIT submits an in-worker parse retaining verdict and comments.

        A submission is NOT an iteration: counters advance only in EVAL and
        only for real verdicts (#1554/#1794).
        """
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="REVIEW_WAIT")

        result = _dispatch_review(stage, item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.on_done_state == "VALIDATE_WAIT"
        assert result.job.descr == "review"
        assert result.job.sandbox == "read-only"
        assert result.job.allowed_tools == "Read,Glob,Grep,Bash,Skill,Agent,WebFetch"
        assert result.job.parse is not None
        assert result.job.prompt_kwargs["pr_number"] == 1001
        assert item.attempts["pr_review_iter"] == 0  # submission burns nothing

    def test_review_wait_reuses_the_reviewer_session_across_rounds(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A later review round continues the compacted reviewer context."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="REVIEW_WAIT")
        item.payload["pr_review_round"] = 2

        result = _dispatch_review(stage, item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.session_agent == "pr-reviewer"

    def test_review_wait_resumes_the_saved_codex_reviewer_session(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A direct provider receives the reviewer session created in round one."""
        stage = PrReviewStage()
        ctx = make_ctx(config=SimpleNamespace(agent="codex"))
        item = make_work_item(issue=1, pr=1001, state="REVIEW_WAIT")
        item.session_ids["pr-reviewer"] = "review-session-id"

        result = _dispatch_review(stage, item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.resume_session_id == "review-session-id"

    def test_nogo_compacts_reviewer_and_writer_before_the_next_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A Claude writer is compacted after a failed round, before re-review."""
        stage = PrReviewStage()
        ctx = make_ctx(github=FakeStageGitHub(unresolved=[(3, 0)]))
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.worktree = "/tmp/review-worktree"
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert result == Continue(next_state="COMPACT_REVIEWER_WAIT")
        item.state = result.next_state
        reviewer_compact = stage.step(item, ctx)
        assert isinstance(reviewer_compact, JobRequest)
        assert isinstance(reviewer_compact.job, CompactJob)
        assert reviewer_compact.job.session_agent == "pr-reviewer"
        assert reviewer_compact.job.sandbox == "read-only"
        assert reviewer_compact.on_done_state == "COMPACT_WRITER_WAIT"

        item.state = reviewer_compact.on_done_state
        writer_compact = stage.step(item, ctx)
        assert isinstance(writer_compact, JobRequest)
        assert isinstance(writer_compact.job, CompactJob)
        assert writer_compact.job.session_agent == "implementer"
        assert writer_compact.job.sandbox == "read-only"
        assert writer_compact.on_done_state == "REVIEW_WAIT"

    def test_validation_continues_the_reused_reviewer_session(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Validation sees the review it validates, not an unrelated reviewer."""
        stage = PrReviewStage()
        ctx = make_ctx(config=SimpleNamespace(agent="codex"))
        item = make_work_item(issue=1, pr=1001, state="VALIDATE_WAIT")
        item.session_ids["pr-reviewer"] = "review-session-id"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.session_agent == "pr-reviewer"
        assert result.job.resume_session_id == "review-session-id"

    def test_codex_nogo_compacts_both_resumable_sessions(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Codex follows the same compact-before-re-review lifecycle as Claude."""
        stage = PrReviewStage()
        ctx = make_ctx(
            config=SimpleNamespace(agent="codex"),
            github=FakeStageGitHub(unresolved=[(3, 0)]),
        )
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.worktree = "/tmp/review-worktree"
        item.payload["review_audit"] = _valid_audit()

        assert stage.step(item, ctx) == Continue(next_state="COMPACT_REVIEWER_WAIT")

    def test_review_parse_posts_structured_comments_and_routes_to_remediation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Structured comments survive the worker boundary and are actionable."""
        events: list[Any] = []
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(1, 0)], by_severity=[(1, 0, 0)])
        ctx = make_ctx(github=github, event_fn=events.append)
        item = make_work_item(issue=1, pr=1001, state="REVIEW_WAIT")
        item.worktree = "/tmp/wt"
        response = (
            "This must be fixed.\n\nReviewer prose is untrusted.\n\n```json\n"
            '{"comments":[{"path":"hephaestus/automation/pipeline/stages/pr_review.py",'
            '"line":500,"side":"RIGHT","severity":"critical","body":"race"}],'
            '"grade":"F","summary":"race found"}\n```'
        )

        review_request = _dispatch_review(stage, item, ctx)
        assert isinstance(review_request, JobRequest)
        assert isinstance(review_request.job, AgentJob)
        assert review_request.job.parse is not None
        stage.on_job_done(
            item,
            JobResult(ok=True, value=review_request.job.parse(response)),
            ctx,
        )
        assert item.payload["review_threads"] == [
            {
                "path": "hephaestus/automation/pipeline/stages/pr_review.py",
                "line": 500,
                "side": "RIGHT",
                "severity": "critical",
                "body": "race",
            }
        ]

        item.state = "VALIDATE_WAIT"
        stage.on_job_done(item, JobResult(ok=True, value='{"unaddressed": []}'), ctx)
        item.state = "POST"
        post = stage.step(item, ctx)
        assert post == Continue(next_state="DIFFICULTY_WAIT")
        assert github.reviews[1001][0]["comments"] == item.payload["review_threads"]

        item.state = "DIFFICULTY_WAIT"
        stage.on_job_done(item, JobResult(ok=True, value="critical"), ctx)
        item.state = "ADDRESS_WAIT"
        stage.on_job_done(item, JobResult(ok=True, value="addressed"), ctx)
        item.state = "PUSH_WAIT"
        stage.on_job_done(item, JobResult(ok=True, value=True), ctx)
        item.state = "EVAL"

        assert stage.step(item, ctx) == Continue(next_state="COMPACT_REVIEWER_WAIT")
        assert github.mutation_log == [("gh_pr_review_post", (1001, "COMMENT"))]
        assert events == []

    def test_review_wait_forwards_nitpick_config(self, make_ctx: Any, make_work_item: Any) -> None:
        """--nitpick must reach the strict PR-review prompt."""
        stage = PrReviewStage()
        ctx = make_ctx(config=SimpleNamespace(agent="claude", nitpick=True))
        item = make_work_item(issue=1, pr=1001, state="REVIEW_WAIT")

        result = _dispatch_review(stage, item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.prompt_kwargs["include_nitpicks"] is True

    def test_validation_and_difficulty_jobs_are_read_only(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Review-only analysis never receives write-capable agent permissions."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="VALIDATE_WAIT")

        validation = stage.step(item, ctx)
        assert isinstance(validation, JobRequest)
        assert isinstance(validation.job, AgentJob)
        assert validation.job.sandbox == "read-only"

        item.state = "DIFFICULTY_WAIT"
        difficulty = stage.step(item, ctx)
        assert isinstance(difficulty, JobRequest)
        assert isinstance(difficulty.job, AgentJob)
        assert difficulty.job.sandbox == "read-only"

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
                "review_audit": _valid_audit(),
                "review_text": "stale",
                "review_threads": [{"id": "t1"}],
                "raw_review_threads": [{"id": "raw-t1"}],
                "posted_thread_ids": ["t1"],
                "remediation_threads": [{"thread_id": "t1"}],
                "validation_result": "stale",
                "difficulty_tiers": "stale",
                "address_error": True,
                "address_output": "stale",
            }
        )

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        for key in (
            "review_audit",
            "review_text",
            "review_threads",
            "raw_review_threads",
            "posted_thread_ids",
            "remediation_threads",
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
        item.payload["review_audit"] = ReviewAudit(
            grade="F",
            summary="Needs work",
            findings=(
                {
                    "path": "a.py",
                    "line": 1,
                    "side": "RIGHT",
                    "severity": "major",
                    "body": "fix",
                },
                {
                    "path": "b.py",
                    "line": 1,
                    "side": "RIGHT",
                    "severity": "major",
                    "body": "doc",
                },
            ),
            raw_feedback="Reviewer prose is untrusted.",
            valid=True,
        )
        item.payload["review_threads"] = [dict(t) for t in item.payload["review_audit"].findings]
        item.payload["reviewed_pr_head_sha"] = "a" * 40

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "DIFFICULTY_WAIT"
        assert github.mutation_log == [("gh_pr_review_post", (1001, "COMMENT"))]
        assert item.payload["posted_thread_ids"] == ["thread-1001-0", "thread-1001-1"]
        assert item.payload["unresolved_auto"] == 2

    def test_post_with_zero_open_automation_threads_skips_to_eval(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A zero-finding review still posts its final structured audit."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload["review_audit"] = ReviewAudit(
            grade="A",
            summary="All clear",
            findings=(),
            raw_feedback="Reviewer prose is untrusted.",
            valid=True,
        )

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "EVAL"
        assert github.mutation_log == [("gh_pr_review_post", (1001, "COMMENT"))]
        assert github.reviews[1001][0]["comments"] == []
        assert "Total grade: A" in github.reviews[1001][0]["summary"]

    @pytest.mark.parametrize("existing_pr", [False, True], ids=["fresh-pr", "existing-pr"])
    def test_empty_audit_addresses_pre_existing_live_blocking_thread(
        self,
        make_ctx: Any,
        make_work_item: Any,
        existing_pr: bool,
    ) -> None:
        """Both address paths consume durable live blockers absent from the audit."""
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(1, 0, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="POST")
        item.worktree = "/tmp/wt"
        item.payload["existing_pr"] = existing_pr
        item.payload["review_audit"] = ReviewAudit(
            grade="F",
            summary="No new audit findings",
            findings=(),
            raw_feedback="",
            valid=True,
        )

        post_result = stage.step(item, ctx)

        assert post_result == Continue(next_state="DIFFICULTY_WAIT")
        assert github.reviews[1001][0]["comments"] == []
        remediation = [
            {
                "thread_id": "live-thread-1001-0",
                "path": "a.py",
                "line": 1,
                "body": "<!-- hephaestus-severity: major -->\nfinding",
            }
        ]
        assert item.payload["remediation_threads"] == remediation

        item.state = "ADDRESS_WAIT"
        address_result = stage.step(item, ctx)

        assert isinstance(address_result, JobRequest)
        assert isinstance(address_result.job, AgentJob)
        if existing_pr:
            assert address_result.job.prompt_builder is get_address_review_prompt
            presented = json.loads(address_result.job.prompt_kwargs["threads_json"])
        else:
            assert address_result.job.prompt_builder is get_impl_resume_feedback_prompt
            presented = json.loads(address_result.job.prompt_kwargs["review_feedback"])["findings"]
        assert presented == remediation

    def test_difficulty_wait_requests_classification(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """DIFFICULTY_WAIT submits the comment-difficulty job."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="DIFFICULTY_WAIT")
        item.payload["remediation_threads"] = [{"thread_id": "t1"}]

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
        item.payload["review_audit"] = _valid_audit()
        item.payload["remediation_threads"] = [
            {"thread_id": "t1", "path": "tests/test_a.py", "line": 3, "body": "fix the tests"}
        ]
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
            "review_feedback": (
                '{"findings": [{"body": "fix the tests", "line": 3, '
                '"path": "tests/test_a.py", "thread_id": "t1"}]}'
            ),
        }

    def test_json_only_audit_finding_reaches_fresh_pr_remediation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Fresh-PR remediation receives findings even when audit prose is empty."""
        stage = PrReviewStage()
        ctx = make_ctx(github=FakeStageGitHub(by_severity=[(1, 0, 0)]))
        item = make_work_item(issue=1, pr=1001, state="REVIEW_WAIT")
        item.worktree = "/tmp/wt"
        audit = parse_review_audit(
            """```json
{"grade":"F","summary":"Needs work","comments":[{"path":"a.py","line":3,
"side":"RIGHT","severity":"major","body":"Guard the missing value"}]}
```"""
        )

        stage.on_job_done(item, JobResult(ok=True, value=audit), ctx)
        item.state = "POST"
        item.payload["reviewed_pr_head_sha"] = "a" * 40
        assert stage.step(item, ctx) == Continue(next_state="DIFFICULTY_WAIT")
        item.state = "ADDRESS_WAIT"
        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.prompt_builder is get_impl_resume_feedback_prompt
        feedback = result.job.prompt_kwargs["review_feedback"]
        assert '"path": "a.py"' in feedback
        assert "Guard the missing value" in feedback
        prompt = result.job.prompt_builder(**result.job.prompt_kwargs)
        assert "BEGIN_" in prompt
        assert "Guard the missing value" in prompt

    def test_address_existing_pr_runs_address_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Existing-PR path runs the address-review session on the threads."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="ADDRESS_WAIT")
        item.worktree = "/tmp/wt"
        item.payload["existing_pr"] = True
        item.payload["remediation_threads"] = [
            {"thread_id": "t1", "path": "x.py", "line": 1, "body": "fix"}
        ]
        item.payload["difficulty_tiers"] = "@ x.py Line 1 - simple - fix"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.job.descr == "address"
        assert result.job.prompt_builder is get_address_review_prompt
        assert result.job.prompt_kwargs["pr_number"] == 1001
        assert result.job.prompt_kwargs["todo_block"] == "@ x.py Line 1 - simple - fix"
        assert json.loads(result.job.prompt_kwargs["threads_json"]) == [
            {"thread_id": "t1", "path": "x.py", "line": 1, "body": "fix"}
        ]

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

    def test_address_refuses_fork_head_without_base_origin_write(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A fetched fork head may be reviewed but never addressed via origin."""
        stage = PrReviewStage()
        ctx = make_ctx(github=FakeStageGitHub(pr_head_writable=False))
        item = make_work_item(issue=1, pr=1001, state="ADDRESS_WAIT")
        item.payload["existing_pr"] = True
        item.worktree = "/tmp/detached-pr-review"

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "pr_head_not_writable")

    @pytest.mark.parametrize("state", ["FOLLOWUP_WAIT", "PR_FINISH"])
    def test_retired_legacy_followup_states_fail_closed(
        self, state: str, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Retired follow-up states do not regain an active stage handler."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state=state)

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {state}")

    def test_on_enter_does_not_restore_receipts_from_a_second_local_journal(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A restart without active item receipts must fail closed without local state I/O."""

        class NoReceiptJournalGitHub(FakeStageGitHub):
            def load_process_review_thread_receipts(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                pytest.fail("PR-review entry must not restore mutation authority from local state")

        item = make_work_item(issue=1, pr=1001, state="REVIEW_WAIT")

        assert PrReviewStage().on_enter(item, make_ctx(github=NoReceiptJournalGitHub())) is None

    def test_unknown_state_fails(self, make_ctx: Any, make_work_item: Any) -> None:
        """An unknown state finishes failed instead of looping silently."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="BOGUS")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL


class TestProcessOwnedReviewThreadLifecycle:
    """Process-owned review threads survive rounds without count inflation."""

    @staticmethod
    def _thread(
        thread_id: str,
        line: int,
        body: str,
        *,
        authors: list[str] | None = None,
    ) -> dict[str, Any]:
        participants = authors or ["hephaestus[bot]"]
        return {
            "id": thread_id,
            "path": "a.py",
            "line": line,
            "side": "RIGHT",
            "severity": "major",
            "body": f"<!-- hephaestus-severity: major -->\n{body}",
            "automation_owned": participants == ["hephaestus[bot]"],
            "author": participants[0],
            "authors": participants,
            "comments": [
                {
                    "id": f"comment-{thread_id}-{index}",
                    "author": author,
                    "body": body,
                    "review_id": f"review-{thread_id}",
                }
                for index, author in enumerate(participants)
            ],
            "review_id": f"review-{thread_id}",
            "created_head_sha": "b" * 40,
        }

    def test_process_receipt_requires_the_original_comment_node_id(self) -> None:
        """A process receipt without its original immutable comment ID is unproven."""
        receipt = self._thread("process-1", 3, "fix this")
        del receipt["comments"][0]["id"]

        assert not _is_process_thread_receipt(receipt)

    def test_stale_line_receipt_requires_explicit_restart_provenance(self) -> None:
        """Only host-normalized restart receipts may retain GitHub's null line."""
        receipt = self._thread("process-1", 3, "fix this")
        receipt["line"] = None

        assert not _is_process_thread_receipt(receipt)

        receipt["restart_stale_line"] = True

        assert _is_process_thread_receipt(receipt)

    def test_external_bot_receipt_requires_verified_bot_actor(self) -> None:
        """A login-shaped user thread must not enter the external-bot path."""
        receipt = self._thread("bot-1", 3, "fix this")
        receipt.update(
            {
                "external_bot": True,
                "author_type": "Bot",
            }
        )
        receipt["comments"][0]["author_type"] = "Bot"

        assert _is_process_thread_receipt(receipt)

        receipt["author_type"] = "User"

        assert not _is_process_thread_receipt(receipt)

    def test_unaddressed_external_bot_receipt_routes_to_remediation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An exact bot finding is fenced into the address path, not a human handoff."""
        bot = self._thread("bot-1", 3, "fix this")
        bot.update(
            {
                "external_bot": True,
                "author_type": "Bot",
                "automation_owned": True,
            }
        )
        bot["comments"][0]["author_type"] = "Bot"

        class BotGitHub(FakeStageGitHub):
            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(bot)]

        item = make_work_item(issue=1, pr=1001, state="POST")
        item.worktree = "/tmp/wt"
        item.payload.update(
            {
                "existing_pr": True,
                "reviewed_pr_head_sha": "a" * 40,
                "process_review_threads": [dict(bot)],
                "validation_process_threads": [dict(bot)],
                "validation_result": {"unaddressed": [{"thread_id": "bot-1"}], "wont_fix": []},
                "review_audit": ReviewAudit("A", "clean", (), "", valid=True),
                "review_threads": [],
            }
        )

        stage = PrReviewStage()
        assert stage.step(item, make_ctx(github=BotGitHub())) == Continue(
            next_state="DIFFICULTY_WAIT"
        )
        assert item.payload["remediation_threads"] == [
            {
                "thread_id": "bot-1",
                "path": "a.py",
                "line": 3,
                "body": "<!-- hephaestus-severity: major -->\nfix this",
            }
        ]

    def test_validation_adopts_a_canonical_live_restart_receipt(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A host-normalized prior automation thread re-enters the one receipt flow."""
        receipt = self._thread("process-1", 3, "fix this")
        live = {**receipt, "process_receipt": dict(receipt)}

        class RestartGitHub(FakeStageGitHub):
            def list_restart_process_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(live)]

        item = make_work_item(issue=1, pr=1001, state="VALIDATE_WAIT")
        item.payload["reviewed_pr_head_sha"] = "a" * 40
        result = PrReviewStage().step(item, make_ctx(github=RestartGitHub()))

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert item.payload["process_review_threads"] == [receipt]
        assert json.loads(result.job.prompt_kwargs["prior_comments_json"]) == [live]
        item.payload["validation_result"] = {"unaddressed": [], "wont_fix": []}
        assert _handled_process_receipts(item) == ([receipt], {"process-1": "addressed"})

    def test_wont_fix_process_thread_is_not_a_resolution_candidate(
        self, make_work_item: Any
    ) -> None:
        """Only a verified code fix may make a process thread eligible for closure."""
        process = self._thread("process-1", 3, "fix this")
        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "process_review_threads": [process],
                "validation_process_threads": [dict(process)],
                "validation_result": {"unaddressed": [], "wont_fix": [{"thread_id": "process-1"}]},
            }
        )

        assert _handled_process_receipts(item) == ([], {})

    def test_process_thread_at_its_creation_head_is_not_a_resolution_candidate(
        self, make_work_item: Any
    ) -> None:
        """A validator claim alone cannot prove the code changed after the finding was posted."""
        process = self._thread("process-1", 3, "fix this")
        process["created_head_sha"] = "a" * 40
        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "process_review_threads": [process],
                "validation_process_threads": [dict(process)],
                "validation_result": {"unaddressed": [], "wont_fix": []},
            }
        )

        assert _handled_process_receipts(item) == ([], {})

    def test_live_process_threads_stand_down_without_duplication(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Existing loop threads stay visible for human resolution, not replacement posts."""

        class LiveThreadGitHub(FakeStageGitHub):
            def __init__(self, live: list[dict[str, Any]]) -> None:
                super().__init__()
                self.live = live
                self.posted_batches: list[list[dict[str, Any]]] = []
                self.next_id = 0

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(thread) for thread in self.live]

            def post_review_threads(
                self, pr_number: int, threads: list[dict[str, Any]], summary: str
            ) -> list[dict[str, Any]]:
                del summary
                self.posted_batches.append([dict(thread) for thread in threads])
                thread_ids: list[str] = []
                for thread in threads:
                    thread_id = f"new-{self.next_id}"
                    self.next_id += 1
                    thread_ids.append(thread_id)
                    self.live.append(
                        TestProcessOwnedReviewThreadLifecycle._thread(
                            thread_id, int(thread["line"]), str(thread["body"])
                        )
                    )
                self._log("gh_pr_review_post", pr_number, "COMMENT")
                new_ids = set(thread_ids)
                return [dict(thread) for thread in self.live if str(thread.get("id")) in new_ids]

        inherited = [
            self._thread(f"inherited-{index}", index + 1, f"old {index}") for index in range(10)
        ]
        prior_process = [
            self._thread(f"process-{index}", index + 20, f"duplicate {index}")
            for index in range(12)
        ]
        github = LiveThreadGitHub(inherited + prior_process)
        ctx = make_ctx(github=github)
        stage = PrReviewStage()
        item = make_work_item(issue=1, pr=1001, state="POST")
        duplicate_findings = [
            {
                "path": "a.py",
                "line": index + 20,
                "side": "RIGHT",
                "severity": "major",
                "body": f"duplicate {index}",
            }
            for index in range(2)
        ]
        new_findings = [
            {
                "path": "a.py",
                "line": index + 50,
                "side": "RIGHT",
                "severity": "major",
                "body": f"new {index}",
            }
            for index in range(7)
        ]
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "process_review_threads": [dict(thread) for thread in prior_process],
                "validation_process_threads": [dict(thread) for thread in prior_process],
                "validation_result": {
                    "unaddressed": [
                        {"thread_id": "process-0"},
                        {"thread_id": "process-1"},
                    ],
                    "wont_fix": [],
                },
                "review_audit": ReviewAudit(
                    grade="F",
                    summary="follow-up audit",
                    findings=tuple(duplicate_findings + new_findings),
                    raw_feedback="",
                    valid=True,
                ),
                "review_threads": duplicate_findings + new_findings,
            }
        )

        result = stage.step(item, ctx)

        assert result == StageOutcome(
            Disposition.FINISH_FAIL,
            "automation_threads_require_human_resolution",
        )
        assert github.posted_batches == []
        assert len(github.live) == 22

    def test_addressed_process_thread_stands_down_when_guarded_accessor_blocks(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A blocked guarded-resolution result remains a human handoff."""

        class ResolverForbiddenGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.live = [
                    TestProcessOwnedReviewThreadLifecycle._thread("process-1", 3, "fix this")
                ]

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(thread) for thread in self.live]

        process = ResolverForbiddenGitHub().live[0]
        github = ResolverForbiddenGitHub()
        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "process_review_threads": [dict(process)],
                "validation_process_threads": [dict(process)],
                "validation_result": {"unaddressed": [], "wont_fix": []},
                "review_audit": ReviewAudit("A", "clean", (), "", valid=True),
                "review_threads": [],
            }
        )

        result = PrReviewStage().step(item, make_ctx(github=github))

        assert result == StageOutcome(
            Disposition.FINISH_FAIL,
            "automation_threads_require_human_resolution",
        )
        assert ("mark_pr_implementation_no_go", (1001,)) in github.mutation_log
        assert not any(name == "mark_pr_implementation_go" for name, _ in github.mutation_log)

    def test_addressed_process_thread_replies_and_resolves_before_post(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Validated handled receipts use the narrow reply-before-resolve accessor."""

        class ResolvingGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.live = [
                    TestProcessOwnedReviewThreadLifecycle._thread("process-1", 3, "fix this")
                ]
                self.calls: list[tuple[int, str, list[dict[str, Any]], dict[str, str]]] = []

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(thread) for thread in self.live]

            def reply_and_resolve_process_review_threads(
                self,
                pr_number: int,
                *,
                reviewed_head_sha: str,
                receipts: list[dict[str, Any]],
                dispositions: dict[str, str],
            ) -> ProcessThreadResolutionResult:
                self.calls.append((pr_number, reviewed_head_sha, receipts, dispositions))
                self.live = []
                return ProcessThreadResolutionResult(resolved_thread_ids=("process-1",))

        github = ResolvingGitHub()
        process = github.live[0]
        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "process_review_threads": [dict(process)],
                "validation_process_threads": [dict(process)],
                "validation_result": {"unaddressed": [], "wont_fix": []},
                "review_audit": ReviewAudit("A", "clean", (), "", valid=True),
                "review_threads": [],
            }
        )

        result = PrReviewStage().step(item, make_ctx(github=github))

        assert result == Continue(next_state="EVAL")
        assert github.calls == [(1001, "a" * 40, [process], {"process-1": "addressed"})]
        assert item.payload["process_review_threads"] == []

    def test_minor_finding_is_audit_only_not_an_inline_merge_blocker(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Minor/nitpick findings are never published as unresolved inline threads."""

        class CapturePostsGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.posted_batches: list[list[dict[str, Any]]] = []

            def post_review_threads(
                self, pr_number: int, threads: list[dict[str, Any]], summary: str
            ) -> list[dict[str, Any]]:
                self.posted_batches.append([dict(thread) for thread in threads])
                return super().post_review_threads(pr_number, threads, summary)

        github = CapturePostsGitHub()
        item = make_work_item(issue=1, pr=1001, state="POST")
        minor = {
            "path": "a.py",
            "line": 3,
            "side": "RIGHT",
            "severity": "minor",
            "body": "Non-blocking improvement.",
        }
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "review_audit": ReviewAudit("A", "clean", (minor,), "", valid=True),
                "review_threads": [minor],
            }
        )

        result = PrReviewStage().step(item, make_ctx(github=github))

        assert result == Continue(next_state="EVAL")
        assert github.posted_batches == [[]]

    def test_validation_receives_only_durable_live_process_thread_facts(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The validator gets host-read thread ids, never agent-supplied ids."""
        stage = PrReviewStage()
        item = make_work_item(issue=1, pr=1001, state="VALIDATE_WAIT")
        process = self._thread("process-1", 3, "fix this")
        inherited = self._thread("inherited", 8, "manual review")

        class ProcessOnlyGitHub(FakeStageGitHub):
            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(process), dict(inherited)]

        ctx = make_ctx(github=ProcessOnlyGitHub())
        item.payload["process_review_threads"] = [dict(process)]

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert json.loads(result.job.prompt_kwargs["prior_comments_json"]) == [process]

    def test_externally_resolved_unaddressed_receipt_is_reopened(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A vanished prior thread cannot suppress the validator's re-opened finding."""

        class ExternallyResolvedGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.live: list[dict[str, Any]] = []
                self.posted: list[dict[str, Any]] = []

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(thread) for thread in self.live]

            def post_review_threads(
                self, pr_number: int, threads: list[dict[str, Any]], summary: str
            ) -> list[dict[str, Any]]:
                del summary
                self.posted = [dict(thread) for thread in threads]
                posted_ids = [f"reopened-{index}" for index, _ in enumerate(threads)]
                for thread_id, thread in zip(posted_ids, threads, strict=True):
                    self.live.append(
                        TestProcessOwnedReviewThreadLifecycle._thread(
                            thread_id,
                            int(thread["line"]),
                            str(thread["body"]),
                        )
                    )
                self._log("gh_pr_review_post", pr_number, "COMMENT")
                return [dict(thread) for thread in self.live if thread["id"] in posted_ids]

        prior = self._thread("process-1", 3, "fix this")
        github = ExternallyResolvedGitHub()
        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "process_review_threads": [prior],
                "validation_process_threads": [prior],
                "validation_result": {
                    "unaddressed": [
                        {
                            "thread_id": "process-1",
                            "path": "a.py",
                            "line": 3,
                            "detail": "fix this",
                        }
                    ],
                    "wont_fix": [],
                },
                "review_audit": ReviewAudit("F", "follow-up", (), "", valid=True),
                "review_threads": [],
            }
        )

        result = PrReviewStage().step(item, make_ctx(github=github))

        assert result == Continue(next_state="DIFFICULTY_WAIT")
        assert len(github.posted) == 1
        assert github.posted[0]["body"] == "Reopened (prior round, still unaddressed): fix this"

    def test_changed_unaddressed_receipt_is_reopened(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A reply makes an old receipt ineligible to suppress a re-open."""

        class ChangedReceiptGitHub(FakeStageGitHub):
            def __init__(self, live: list[dict[str, Any]]) -> None:
                super().__init__()
                self.live = live
                self.posted: list[dict[str, Any]] = []

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(thread) for thread in self.live]

            def post_review_threads(
                self, pr_number: int, threads: list[dict[str, Any]], summary: str
            ) -> list[dict[str, Any]]:
                del summary
                self.posted = [dict(thread) for thread in threads]
                posted_ids = [f"reopened-{index}" for index, _ in enumerate(threads)]
                for thread_id, thread in zip(posted_ids, threads, strict=True):
                    self.live.append(
                        TestProcessOwnedReviewThreadLifecycle._thread(
                            thread_id,
                            int(thread["line"]),
                            str(thread["body"]),
                        )
                    )
                self._log("gh_pr_review_post", pr_number, "COMMENT")
                return [dict(thread) for thread in self.live if thread["id"] in posted_ids]

        prior = self._thread("process-1", 3, "fix this")
        changed = dict(prior)
        changed["comments"] = [
            {"author": "hephaestus[bot]", "body": "fix this"},
            {"author": "hephaestus[bot]", "body": "human follow-up"},
        ]
        github = ChangedReceiptGitHub([changed])
        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "process_review_threads": [prior],
                "validation_process_threads": [prior],
                "validation_result": {
                    "unaddressed": [
                        {
                            "thread_id": "process-1",
                            "path": "a.py",
                            "line": 3,
                            "detail": "fix this",
                        }
                    ],
                    "wont_fix": [],
                },
                "review_audit": ReviewAudit("F", "follow-up", (), "", valid=True),
                "review_threads": [],
            }
        )

        result = PrReviewStage().step(item, make_ctx(github=github))

        assert result == Continue(next_state="DIFFICULTY_WAIT")
        assert len(github.posted) == 1
        assert github.posted[0]["body"] == "Reopened (prior round, still unaddressed): fix this"

    @pytest.mark.parametrize(
        ("pr_state", "validation_result", "authors"),
        [
            pytest.param(
                {"state": "OPEN", "headRefOid": "b" * 40, "autoMergeRequest": None},
                {"unaddressed": [], "wont_fix": []},
                ["hephaestus[bot]"],
                id="head-drift",
            ),
            pytest.param(
                {"state": "OPEN", "headRefOid": "a" * 40, "autoMergeRequest": None},
                {"unaddressed": [{"thread_id": ""}], "wont_fix": []},
                ["hephaestus[bot]"],
                id="malformed-validator-id",
            ),
            pytest.param(
                {"state": "OPEN", "headRefOid": "a" * 40, "autoMergeRequest": None},
                {"unaddressed": [], "wont_fix": []},
                ["hephaestus[bot]", "reviewer"],
                id="human-participant",
            ),
        ],
    )
    def test_unsafe_validated_process_thread_never_resolves(
        self,
        make_ctx: Any,
        make_work_item: Any,
        pr_state: dict[str, Any],
        validation_result: dict[str, Any],
        authors: list[str],
    ) -> None:
        """Head drift, malformed ids, and human replies are all no-resolve boundaries."""

        class ResolutionRecordingGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__(pr_state=pr_state)
                self.live = [
                    TestProcessOwnedReviewThreadLifecycle._thread(
                        "process-1", 3, "fix this", authors=authors
                    )
                ]

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(thread) for thread in self.live]

            def post_review_threads(
                self, pr_number: int, threads: list[dict[str, Any]], summary: str
            ) -> list[dict[str, Any]]:
                self._log("gh_pr_review_post", pr_number, "COMMENT")
                return []

        github = ResolutionRecordingGitHub()
        ctx = make_ctx(github=github)
        stage = PrReviewStage()
        item = make_work_item(issue=1, pr=1001, state="POST")
        process = self._thread("process-1", 3, "fix this")
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "process_review_threads": [process],
                "validation_process_threads": [process],
                "validation_result": validation_result,
                "review_audit": ReviewAudit("A", "clean", (), "", valid=True),
                "review_threads": [],
            }
        )

        result = stage.step(item, ctx)

        if pr_state["headRefOid"] != "a" * 40:
            assert result == Continue(next_state="REVIEW_WAIT")
            assert github.mutation_log == []

    def test_live_process_thread_requires_human_resolution_without_thread_write(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """No receipt revalidation can authorize an automatic thread mutation."""

        class GuardRaceGitHub(FakeStageGitHub):
            def __init__(self, live: dict[str, Any]) -> None:
                super().__init__()
                self.live = live

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(self.live)]

        process = self._thread("process-1", 3, "fix this")
        github = GuardRaceGitHub(process)
        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "process_review_threads": [process],
                "validation_process_threads": [process],
                "validation_result": {"unaddressed": [], "wont_fix": []},
                "review_audit": ReviewAudit("A", "clean", (), "", valid=True),
                "review_threads": [],
            }
        )

        result = PrReviewStage().step(item, make_ctx(github=github))

        assert result == StageOutcome(
            Disposition.FINISH_FAIL,
            "automation_threads_require_human_resolution",
        )
        assert ("mark_pr_implementation_no_go", (1001,)) in github.mutation_log

    def test_truncated_live_thread_facts_skip_validation_and_all_writes(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A pagination/read failure cannot hide a later human participant."""

        class TruncatedThreadsGitHub(FakeStageGitHub):
            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                raise RuntimeError("could not fetch all PR review threads")

        stage = PrReviewStage()
        github = TruncatedThreadsGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="VALIDATE_WAIT")
        item.payload["process_review_threads"] = [self._thread("process-1", 3, "fix this")]

        result = stage.step(item, ctx)

        assert result == Continue(next_state="EVAL")
        assert github.mutation_log == []


class TestPrReviewRestartSafetyGuards:
    """Unreachable PR-narrowing guards use the restart-safety no_pr contract."""

    @pytest.mark.parametrize(
        ("method_name", "state"),
        [
            pytest.param("_post", "POST", id="post"),
            pytest.param("_address", "ADDRESS_WAIT", id="address"),
            pytest.param("_eval", "EVAL", id="eval"),
        ],
    )
    def test_unreachable_pr_none_guards_finish_no_pr(
        self, make_ctx: Any, make_work_item: Any, method_name: str, state: str
    ) -> None:
        """Direct calls to the unreachable guards finish failed with no_pr."""
        stage = PrReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=None, state=state)

        result = getattr(stage, method_name)(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        assert "agent_error_failback" not in item.payload
        assert github.mutation_log == []


class TestEvalVerdicts:
    """EVAL: re-housed _evaluate_go_verdict semantics + the budget gate."""

    def test_on_enter_stands_down_without_auto_merge_mutation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Direct pr_review ingress is read-only while the PR is unarmed."""
        stage = PrReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        assert github.mutation_log == []

    def test_clean_structural_audit_advances_with_untrusted_feedback(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Only valid structure plus fresh thread facts reaches the label boundary."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = ReviewAudit(
            grade="F",
            summary="Needs no actionable changes",
            findings=(),
            raw_feedback="External review prose is not an authorization signal.",
            valid=True,
        )

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.ADVANCE, "review audit; merge wait pending")
        assert ("mark_pr_implementation_go", (1001,)) in github.mutation_log

    def test_non_structured_audit_payload_cannot_authorize_clean_pr(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An arbitrary payload object is inert without a structured audit."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = object()

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.RETRY, "review audit format failure")
        assert not any(name == "mark_pr_implementation_go" for name, _ in github.mutation_log)

    def test_legacy_audit_payload_key_cannot_authorize_clean_pr(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Only the canonical structured-audit key may reach the label boundary."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_verdict"] = ReviewAudit(
            grade="A",
            summary="Clean review",
            findings=(),
            raw_feedback="",
            valid=True,
        )

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.RETRY, "review audit format failure")
        assert not any(name == "mark_pr_implementation_go" for name, _ in github.mutation_log)

    def test_final_comment_contains_audit_only(self) -> None:
        """The durable review comment contains no textual decision field."""
        body = PrReviewStage._final_review_comment(
            ReviewAudit(
                grade="A",
                summary="Safe summary",
                findings=(),
                raw_feedback="Private reviewer detail",
                valid=True,
            )
        )

        assert "Total grade: A" in body
        assert "Safe summary" in body
        assert "private reviewer detail" not in body

    def test_go_with_zero_threads_marks_implementation_go_and_advances_to_merge_wait(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The PR-review gate marks GO; merge_wait later verifies the exact head."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        ctx.config.enable_follow_up = False
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result == StageOutcome(Disposition.ADVANCE, "review audit; merge wait pending")
        assert github.mutation_log == [("mark_pr_implementation_go", (1001,))]
        assert not github.comments.get(1001)
        assert ("arm_auto_merge", (1001,)) not in github.mutation_log
        assert item.attempts["pr_review_iter"] == 1  # real verdict counted

    def test_thread_added_during_go_write_preserves_external_labels_and_requires_human_handoff(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A late thread must not let this run relabel an external state."""

        class ThreadAddedDuringGoGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self._late_thread = False

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                if not self._late_thread:
                    return []
                return [
                    {
                        "id": "late-thread",
                        "severity": "major",
                        "automation_owned": True,
                        "author": "hephaestus[bot]",
                        "authors": ["hephaestus[bot]"],
                        "comments": [],
                    }
                ]

            def mark_pr_implementation_go(self, pr_number: int) -> None:
                super().mark_pr_implementation_go(pr_number)
                self._late_thread = True
                # An external actor writes its own state after this run's GO.
                self._pr_impl_state = (False, True)

        stage = PrReviewStage()
        github = ThreadAddedDuringGoGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert result == StageOutcome(
            Disposition.FINISH_FAIL,
            "late_threads_require_human_resolution",
        )
        assert ("mark_pr_implementation_go", (1001,)) in github.mutation_log
        assert not any(
            name in {"mark_pr_implementation_no_go", "gh_issue_remove_labels"}
            for name, _args in github.mutation_log
        )
        assert github.pr_has_implementation_state_label(1001) == (False, True)
        assert "review activity changed" in github.comments[1001][0].lower()

    def test_clean_go_does_not_call_the_removed_auto_merge_mutator(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A legacy mutator override cannot affect the label-only GO path."""

        class DeferFailsGitHub(FakeStageGitHub):
            def defer_auto_merge(self, pr_number: int) -> None:
                raise RuntimeError(f"PR #{pr_number} remains armed")

        stage = PrReviewStage()
        github = DeferFailsGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        ctx.config.enable_follow_up = False
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.ADVANCE, "review audit; merge wait pending"
        )
        assert 1001 not in github.comments

    def test_clean_go_marks_the_loop_owned_approval_label(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """PR review grants eligibility but never arms auto-merge itself."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        ctx.config.enable_follow_up = False
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.ADVANCE, "review audit; merge wait pending")
        assert github.mutation_log == [("mark_pr_implementation_go", (1001,))]
        assert ("arm_auto_merge", (1001,)) not in github.mutation_log

    def test_clean_go_applies_the_approval_label(self, make_ctx: Any, make_work_item: Any) -> None:
        """Clean GO applies the review-owned approval label."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        ctx.config.enable_follow_up = False
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result == StageOutcome(Disposition.ADVANCE, "review audit; merge wait pending")
        assert ("mark_pr_implementation_go", (1001,)) in github.mutation_log

    @pytest.mark.parametrize(
        "readback",
        [
            pytest.param((False, False), id="absent"),
            pytest.param((True, True), id="contradictory"),
            pytest.param(RuntimeError("label read failed"), id="error"),
        ],
    )
    def test_go_readback_is_independent_and_fail_closed(
        self, make_ctx: Any, make_work_item: Any, readback: Any
    ) -> None:
        """A GO mutation cannot admit merge-wait without confirmed labels."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            unresolved=[(0, 0)],
            pr_impl_readbacks=[readback],
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=36, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()
        item.payload["reviewed_pr_head_sha"] = "a" * 40

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "implementation_go_readback_failed")
        assert github.mutation_log == [("mark_pr_implementation_go", (1001,))]

    def test_post_go_label_head_drift_restarts_review_without_clearing_labels(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Head drift discards local proof without claiming label ownership."""

        class PostWriteDriftGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.go_written = False

            def mark_pr_implementation_go(self, pr_number: int) -> None:
                super().mark_pr_implementation_go(pr_number)
                self.go_written = True

            def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
                del pr_number
                return {
                    "state": "OPEN",
                    "headRefOid": ("b" if self.go_written else "a") * 40,
                    "autoMergeRequest": None,
                }

        stage = PrReviewStage()
        github = PostWriteDriftGitHub()
        item = make_work_item(issue=36, pr=1001, state="EVAL")
        item.payload.update({"review_audit": _valid_audit(), "reviewed_pr_head_sha": "a" * 40})

        result = stage.step(item, make_ctx(github=github))

        assert result == Continue(next_state="REVIEW_WAIT")
        assert "reviewed_pr_head_sha" not in item.payload
        assert github.pr_has_implementation_state_label(1001) == (True, False)
        assert ("mark_pr_implementation_go", (1001,)) in github.mutation_log
        assert not any(name == "gh_issue_remove_labels" for name, _ in github.mutation_log)

    def test_post_go_new_head_and_external_go_never_removes_external_label(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A post-write read cannot distinguish this process's GO from an external one."""

        class ExternalGoAfterPushGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.external_go_written = False

            def mark_pr_implementation_go(self, pr_number: int) -> None:
                super().mark_pr_implementation_go(pr_number)
                # Model an external actor replacing the label after pushing a
                # new head. The accessor has no ownership token for that GO.
                self._pr_impl_state = (True, False)
                self.external_go_written = True

            def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
                del pr_number
                return {
                    "state": "OPEN",
                    "headRefOid": ("b" if self.external_go_written else "a") * 40,
                    "autoMergeRequest": None,
                }

        stage = PrReviewStage()
        github = ExternalGoAfterPushGitHub()
        item = make_work_item(issue=38, pr=1001, state="EVAL")
        item.payload.update({"review_audit": _valid_audit(), "reviewed_pr_head_sha": "a" * 40})

        result = stage.step(item, make_ctx(github=github))

        assert result == Continue(next_state="REVIEW_WAIT")
        assert "reviewed_pr_head_sha" not in item.payload
        assert github.pr_has_implementation_state_label(1001) == (True, False)
        assert github.mutation_log == [("mark_pr_implementation_go", (1001,))]

    def test_post_go_label_auto_merge_arm_stands_down_without_clearing(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A post-write external arm makes the label unsafe to revoke."""

        class PostWriteArmGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.go_written = False

            def mark_pr_implementation_go(self, pr_number: int) -> None:
                super().mark_pr_implementation_go(pr_number)
                self.go_written = True

            def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
                del pr_number
                return {
                    "state": "OPEN",
                    "headRefOid": "a" * 40,
                    "autoMergeRequest": {"enabledAt": "now"} if self.go_written else None,
                }

        stage = PrReviewStage()
        github = PostWriteArmGitHub()
        item = make_work_item(issue=37, pr=1001, state="EVAL")
        item.payload.update({"review_audit": _valid_audit(), "reviewed_pr_head_sha": "a" * 40})

        result = stage.step(item, make_ctx(github=github))

        assert result == StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        assert github.pr_has_implementation_state_label(1001) == (True, False)
        assert not any(name == "gh_issue_remove_labels" for name, _ in github.mutation_log)

    def test_invalid_structured_audit_cannot_advance(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A malformed audit cannot apply a state label or reach merge wait."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _invalid_audit()

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.RETRY, "review audit format failure")
        assert ("mark_pr_implementation_go", (1001,)) not in github.mutation_log
        assert ("mark_pr_implementation_no_go", (1001,)) not in github.mutation_log
        assert item.payload["review_audit"].grade is None
        assert item.payload["review_audit"].valid is False

    def test_go_rechecks_human_threads_and_stands_down(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A late human thread stands down without deleting unknown label state."""
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(0, 0, 0), (0, 0, 1)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL
        assert result.note == "human_blocked"
        assert github.mutation_log == [("gh_issue_comment", (1001,))]
        assert ("mark_pr_implementation_go", (1001,)) not in github.mutation_log
        assert ("arm_auto_merge", (1001,)) not in github.mutation_log
        assert "Automation stand-down" in github.comments[1001][0]

    def test_go_with_follow_up_enabled_advances_to_merge_wait(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A clean GO bypasses legacy follow-up work for merge wait."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.ADVANCE, "review audit; merge wait pending")
        assert github.mutation_log == [("mark_pr_implementation_go", (1001,))]

    def test_go_with_human_thread_is_human_blocked_without_label_mutation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """GO + open human thread -> HUMAN_BLOCKED without label mutation.

        A human may concurrently own either implementation-state label, and
        GitHub offers no compare-and-set deletion.  Automation must stand down
        rather than erase a state it cannot prove it owns.
        """
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 1)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL
        assert result.note == "human_blocked"
        assert github.mutation_log == [("gh_issue_comment", (1001,))]

    def test_failed_audit_with_human_thread_has_no_go_shaped_stand_down(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A human-blocked F audit cannot publish false GO prose."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 1)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = ReviewAudit(
            grade="F",
            summary="Material findings remain",
            findings=(),
            raw_feedback="",
            valid=True,
        )

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "human_blocked")
        body = github.comments[1001][0]
        assert "reached GO" not in body
        assert "prevent a transition" in body

    def test_go_with_automation_thread_downgrades_and_loops(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """GO + open automation thread is downgraded to NOGO: re-review."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(2, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "REVIEW_WAIT"
        # No GO labels while threads open; the downgraded round durably
        # records NO-GO (doc section 5: "NOGO verdict, before retry/regress").
        assert github.mutation_log == [("mark_pr_implementation_no_go", (1001,))]

    def test_nogo_within_soft_budget_loops_to_re_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """NOGO within the soft budget loops back for a fresh review round."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(3, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

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
        item.payload["review_audit"] = _invalid_audit()

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.RETRY, "review audit format failure")
        assert item.attempts.get("pr_review_iter", 0) == 0

    def test_address_error_fails_back_without_burning_a_round(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A hard-failed address/push leg fails back agent_error, no round burned."""
        stage = PrReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["address_error"] = True
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FAIL_BACK
        assert result.note == "agent_error"
        assert item.attempts["pr_review_iter"] == 0  # no round burned
        assert github.mutation_log == []

    # Severity-aware GO gate tests (#1856)
    def test_same_login_human_reply_to_process_advisory_thread_blocks_go(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A reply sharing the host login is not proof the process wrote it."""

        class SameLoginReplyGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.live = {
                    "id": "process-advisory",
                    "path": "a.py",
                    "line": 3,
                    "side": "RIGHT",
                    "severity": "nitpick",
                    "body": "<!-- hephaestus-severity: nitpick -->\noriginal finding",
                    # The production adapter sets this from current_login;
                    # it is deliberately true despite the human reply.
                    "automation_owned": True,
                    "author": "mvillmow",
                    "authors": ["mvillmow", "mvillmow"],
                    "review_id": "review-process-advisory",
                    "comments": [
                        {"author": "mvillmow", "body": "original finding"},
                        {"author": "mvillmow", "body": "human reply"},
                    ],
                }

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(self.live)]

        stage = PrReviewStage()
        github = SameLoginReplyGitHub()
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload.update(
            {
                "review_audit": _valid_audit(),
                "reviewed_pr_head_sha": "a" * 40,
                "process_review_threads": [
                    {
                        **github.live,
                        "authors": ["mvillmow"],
                        "review_id": "review-process-advisory",
                        "comments": [{"author": "mvillmow", "body": "original finding"}],
                    }
                ],
            }
        )

        result = stage.step(item, make_ctx(github=github))

        assert result == StageOutcome(
            Disposition.FINISH_FAIL,
            "automation_threads_require_human_resolution",
        )
        assert not any(name == "mark_pr_implementation_go" for name, _ in github.mutation_log)

    def test_go_with_only_minor_automation_thread_requires_human_resolution(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Even advisory threads are GitHub merge blockers and stay human-owned."""
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(0, 2, 0)])  # 0 blocking, 2 minor, 0 human
        ctx = make_ctx(github=github)
        ctx.config.enable_follow_up = False
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()
        item.payload["process_review_threads"] = github.list_unresolved_review_threads(1001)

        result = stage.step(item, ctx)

        assert result == StageOutcome(
            Disposition.FINISH_FAIL,
            "automation_threads_require_human_resolution",
        )
        assert ("mark_pr_implementation_no_go", (1001,)) in github.mutation_log
        assert ("mark_pr_implementation_go", (1001,)) not in github.mutation_log

    def test_advisory_go_head_drift_has_zero_mutations(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A drifted reviewed head blocks thread resolution as well as the GO label."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            by_severity=[(0, 1, 0)],
            pr_state={
                "state": "OPEN",
                "headRefOid": "b" * 40,
                "autoMergeRequest": None,
            },
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()
        item.payload["process_review_threads"] = github.list_unresolved_review_threads(1001)

        result = stage.step(item, ctx)

        assert result == Continue(next_state="REVIEW_WAIT")
        assert "reviewed_pr_head_sha" not in item.payload
        assert github.mutation_log == []

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
        item.payload["review_audit"] = _valid_audit()
        item.payload["pr_review_round"] = 1

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "REVIEW_WAIT"
        # Should NOT resolve (no minor threads), should write no-go
        assert ("mark_pr_implementation_no_go", (1001,)) in github.mutation_log

    def test_unknown_automation_marker_cannot_resolve_or_authorize_go(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An unknown durable severity remains blocking through the GO gate."""

        class UnknownMarkerGitHub(FakeStageGitHub):
            unknown_resolved = False

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                if self.unknown_resolved:
                    return []
                return [
                    {
                        "id": f"unknown-{pr_number}",
                        "body": "<!-- hephaestus-severity: unknown -->\nfinding",
                        "automation_owned": True,
                        "author": "hephaestus[bot]",
                    }
                ]

        stage = PrReviewStage()
        github = UnknownMarkerGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert result == Continue(next_state="REVIEW_WAIT")
        assert ("mark_pr_implementation_no_go", (1001,)) in github.mutation_log
        assert not any(name == "mark_pr_implementation_go" for name, _ in github.mutation_log)

    def test_go_with_human_thread_still_blocks(self, make_ctx: Any, make_work_item: Any) -> None:
        """GO with human thread still hard-blocks (unregressed).

        Human threads are never filtered by severity.
        """
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(0, 0, 1)])  # 0 blocking, 0 minor, 1 human
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL
        # The underlying comment function is gh_issue_comment (not post_pr_comment).
        assert github.mutation_log[-1][0] == "gh_issue_comment"

    def test_go_zero_threads_advances(self, make_ctx: Any, make_work_item: Any) -> None:
        """A clean fresh read is the only route that can advance to merge wait."""
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(0, 0, 0)])  # 0 blocking, 0 minor, 0 human
        ctx = make_ctx(github=github)
        ctx.config.enable_follow_up = False
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result == StageOutcome(Disposition.ADVANCE, "review audit; merge wait pending")
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
        item.payload["review_audit"] = _invalid_audit()

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
            item.payload["review_audit"] = _invalid_audit()
            outcome = stage.step(item, ctx)
            assert isinstance(outcome, StageOutcome)
            assert outcome.disposition == Disposition.RETRY
            assert item.payload["review_error_retries"] == expected_retry

        item.payload["review_audit"] = _invalid_audit()
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
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert item.payload["review_error_retries"] == 0


class TestProgressAwareBudget:
    """Soft cap 3; rounds 4-6 admitted ONLY while threads strictly decrease."""

    def _eval_round(
        self,
        stage: Any,
        item: Any,
        ctx: Any,
        *,
        pre_address: int | None = None,
    ) -> Any:
        """Run one EVAL with a fresh structured audit (state machine loop shortcut).

        ``pre_address`` mirrors what POST would have written to
        ``payload["unresolved_auto"]`` THIS round (the pre-address
        snapshot) — EVAL's progress gate now compares it against this
        same round's post-address count instead of a value carried from
        the previous round (#1863).
        """
        item.payload["review_audit"] = _valid_audit()
        item.payload["reviewed_pr_head_sha"] = "a" * 40
        if pre_address is not None:
            item.payload["unresolved_auto"] = pre_address
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
        outcome = self._eval_round(stage, item, ctx, pre_address=3)  # round 3: 3==3 plateau

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
            ("gh_issue_upsert_comment", (12, "<!-- hephaestus-state-skip-reason -->")),
        ]
        assert item.payload["pr_review_round"] == 3

    def test_decreasing_threads_earn_extension_rounds(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Strictly decreasing threads admit rounds 4+ until the plateau."""
        stage = PrReviewStage()
        # by_severity/unresolved FIFO supplies each round's POST-ADDRESS count
        # (EVAL's automation_unresolved): r1=5, r2=3, r3=2, r4=1, r5=1(repeats).
        # pre_address seeds each round's PRE-ADDRESS count (POST's
        # unresolved_auto) so soft_cap+ rounds prove progress WITHIN
        # themselves (#1863): r3 pre=4>post=2, r4 pre=2>post=1, r5 pre=1==post=1.
        github = FakeStageGitHub(unresolved=[(5, 0), (3, 0), (2, 0), (1, 0), (1, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=13, pr=1001, state="EVAL")
        assert stage.on_enter(item, ctx) is None

        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r1: 5 (< soft_cap)
        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r2: 3 (< soft_cap)
        assert isinstance(
            self._eval_round(stage, item, ctx, pre_address=4), Continue
        )  # r3 == soft_cap: pre=4, post=2 -> progress, r4 earned
        assert isinstance(
            self._eval_round(stage, item, ctx, pre_address=2), Continue
        )  # r4: pre=2, post=1 -> progress, r5 earned
        outcome = self._eval_round(stage, item, ctx, pre_address=1)  # r5: pre=1, post=1 -> plateau

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.SKIP
        assert item.payload["pr_review_round"] == 5
        assert item.attempts["pr_review_iter"] == 5  # lifetime audit trail
        assert item.attempts["pr_review_hard"] == 2  # extension rounds 4 and 5
        assert github.mutation_log[-2:] == [
            ("gh_issue_add_labels", (13, (STATE_SKIP,))),
            ("gh_issue_upsert_comment", (13, "<!-- hephaestus-state-skip-reason -->")),
        ]

    def test_hard_cap_stops_even_with_progress(self, make_ctx: Any, make_work_item: Any) -> None:
        """Round 6 is the absolute ceiling even while threads keep decreasing."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(9, 0), (8, 0), (7, 0), (6, 0), (5, 0), (4, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=14, pr=1001, state="EVAL")
        assert stage.on_enter(item, ctx) is None

        # post-address per round (FIFO): r1=9 r2=8 r3=7 r4=6 r5=5 r6=4.
        # pre_address seeded 1 higher than that round's own post-address
        # so r3-r5 each prove within-round progress; r6's pre_address is
        # irrelevant -- the hard cap stops it regardless (#1863).
        pre_addresses = [None, None, 8, 7, 6, 5]
        outcomes = [
            self._eval_round(stage, item, ctx, pre_address=pre_addresses[i]) for i in range(6)
        ]

        assert all(isinstance(o, Continue) for o in outcomes[:5])  # rounds 1-5 loop
        final = outcomes[5]
        assert isinstance(final, StageOutcome)
        assert final.disposition == Disposition.SKIP  # 6 == hard cap: stop
        assert item.payload["pr_review_round"] == 6

    def test_progress_within_soft_cap_round_earns_extension(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """#1863: address work landing ON the soft-cap round earns an extension.

        Round 3 (soft cap) starts with 6 pre-address unresolved (POST's
        unresolved_auto) and the address leg drives it down to 3
        post-address (EVAL's automation_unresolved) -- progress made
        WITHIN round 3. Round 2 ALSO ended at 3 post-address, so the OLD
        cross-round comparison (round 3 post=3 vs round 2's stored
        post=3) would see a plateau and strand the PR at the soft cap.
        The fix reads round 3's OWN pre-address snapshot (6), so 3 < 6
        is recognized as progress and round 4 is earned. Round 4 then
        plateaus (pre=3, post=3) and the PR exhausts at round 4.
        """
        stage = PrReviewStage()
        # by_severity supplies each round's POST-ADDRESS count (EVAL read):
        # r1=5, r2=3, r3=3, r4=3.
        github = FakeStageGitHub(by_severity=[(5, 0, 0), (3, 0, 0), (3, 0, 0), (3, 0, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=16, pr=1001, state="EVAL")
        assert stage.on_enter(item, ctx) is None

        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r1: post=5 (< soft_cap)
        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r2: post=3 (< soft_cap)
        r3 = self._eval_round(
            stage, item, ctx, pre_address=6
        )  # r3 == soft_cap: pre=6, post=3 -> progress WITHIN round 3, r4 earned
        assert isinstance(r3, Continue)
        outcome = self._eval_round(
            stage, item, ctx, pre_address=3
        )  # r4: pre=3, post=3 -> plateau, exhausted

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.SKIP
        assert item.payload["pr_review_round"] == 4
        assert item.attempts["pr_review_hard"] == 1  # only round 4 is > soft_cap (3)

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
        item.payload["review_audit"] = _valid_audit()
        item.payload["reviewed_pr_head_sha"] = "a" * 40

        result = stage.step(item, ctx)  # must not raise

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.SKIP

    @pytest.mark.parametrize(
        ("late_pr_state", "expected"),
        [
            pytest.param(
                {
                    "state": "OPEN",
                    "headRefOid": "a" * 40,
                    "autoMergeRequest": {"enabledAt": "now"},
                },
                StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed"),
                id="external-arm",
            ),
            pytest.param(
                {"state": "OPEN", "headRefOid": "a" * 40},
                StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified"),
                id="partial-state",
            ),
        ],
    )
    def test_exhaustion_rechecks_unarmed_before_writing_skip(
        self,
        make_ctx: Any,
        make_work_item: Any,
        late_pr_state: dict[str, Any],
        expected: StageOutcome,
    ) -> None:
        """A late arm or ambiguous state cannot receive ``state:skip``."""

        class LateStateGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__(unresolved=[(3, 0)])
                self._states = [
                    {"state": "OPEN", "headRefOid": "a" * 40, "autoMergeRequest": None},
                    late_pr_state,
                ]

            def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
                del pr_number
                return self._states.pop(0)

        github = LateStateGitHub()
        item = make_work_item(issue=17, pr=1001, state="EVAL")
        item.payload["pr_review_round"] = 2
        item.payload["unresolved_auto"] = 3
        item.payload["review_audit"] = _valid_audit()

        outcome = PrReviewStage().step(item, make_ctx(github=github))

        assert outcome == expected
        assert github.mutation_log == [("mark_pr_implementation_no_go", (1001,))]
        assert 17 not in github.labels

    def test_exhaustion_push_after_no_go_re_reviews_without_skip(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A push after the head-bound NO-GO write cannot skip the new head."""

        class PushedHeadGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__(unresolved=[(3, 0)])
                self._states = [
                    {"state": "OPEN", "headRefOid": "a" * 40, "autoMergeRequest": None},
                    {"state": "OPEN", "headRefOid": "b" * 40, "autoMergeRequest": None},
                ]

            def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
                del pr_number
                return self._states.pop(0)

        github = PushedHeadGitHub()
        item = make_work_item(issue=18, pr=1001, state="EVAL")
        item.payload["pr_review_round"] = 2
        item.payload["unresolved_auto"] = 3
        item.payload["review_audit"] = _valid_audit()

        outcome = PrReviewStage().step(item, make_ctx(github=github))

        assert outcome == Continue(next_state="REVIEW_WAIT")
        assert github.mutation_log == [("mark_pr_implementation_no_go", (1001,))]
        assert 18 not in github.labels
        assert "reviewed_pr_head_sha" not in item.payload


class TestPrReviewOnJobDone:
    """on_job_done payload handling (state still at the WAIT state)."""

    def test_review_audit_and_feedback_stored(self, make_ctx: Any, make_work_item: Any) -> None:
        """The parsed structural audit and bounded feedback land on the payload."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="REVIEW_WAIT")
        verdict = _valid_audit()

        stage.on_job_done(item, JobResult(ok=True, value=verdict), ctx)

        assert item.payload["review_audit"] == verdict
        assert item.payload["review_feedback"] == verdict.raw_feedback
        assert "review_verdict" not in item.payload
        assert "review_text" not in item.payload

    def test_failed_review_job_flags_the_dead_round(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A failed review job stores no verdict and flags review_failed."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="REVIEW_WAIT")

        stage.on_job_done(item, JobResult(ok=False, error="reviewer crashed"), ctx)

        assert "review_audit" not in item.payload
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


class TestFullWalks:
    """Full pool-driven walks of the whole stage (canonical FakeWorkerPool)."""

    def test_nogo_round_then_clean_go_walk(self, make_ctx: Any, make_work_item: Any) -> None:
        """ENTER -> NOGO round (address leg) -> GO -> merge wait.

        Round 1: review NOGO, 2 automation threads open -> difficulty ->
        address -> push -> EVAL loops. Round 2: review GO, all threads
        resolved -> merge-wait advance.
        """
        stage = PrReviewStage()
        # POST calls count_unresolved_threads once per round; EVAL calls the
        # new by_severity method once per round. Two rounds => one entry each
        # per round. Round 1 has 2 open blocking threads (NOGO address leg);
        # round 2 is clean (GO: skips difficulty/address, then advances).
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
            JobResult(ok=True, value=_valid_audit()),  # review round 1
            JobResult(ok=True, value='{"unaddressed": []}'),  # validate round 1
            JobResult(ok=True, value="tier list"),  # difficulty
            JobResult(ok=True, value="addressed"),  # address
            JobResult(ok=True, value=True),  # push
            JobResult(ok=True, value=True),  # compact reviewer before round 2
            JobResult(ok=True, value=True),  # compact writer before round 2
            JobResult(ok=True, value=_valid_audit()),  # review round 2
            JobResult(ok=True, value='{"unaddressed": []}'),  # validate round 2
        )

        outcome = _drive(stage, item, ctx, pool)

        assert isinstance(outcome, StageOutcome)
        assert outcome == StageOutcome(Disposition.ADVANCE, "review audit; merge wait pending")
        assert [h.job.descr for h in pool.submitted] == [
            "review",
            "validate",
            "difficulty",
            "address",
            "push_fixes",
            "compact_session",
            "compact_session",
            "review",
            "validate",
        ]
        assert item.attempts["pr_review_iter"] == 1  # only the fresh-head round counts
        assert not any(name == "mark_pr_implementation_no_go" for name, _ in github.mutation_log)
        assert ("mark_pr_implementation_go", (1001,)) in github.mutation_log
        assert github.mutation_log.count(("gh_pr_review_post", (1001, "COMMENT"))) == 2

    def test_unresolved_process_thread_walk_requires_human_resolution(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A later review pass stops at the GitHub conversation-resolution gate."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(3, 0)])  # plateau forever
        ctx = make_ctx(github=github)
        item = make_work_item(issue=22, pr=1001, state="ENTER")
        item.worktree = "/tmp/wt22"

        pool = FakeWorkerPool()
        round_jobs = [
            JobResult(ok=True, value=_valid_audit()),  # review
            JobResult(ok=True, value='{"unaddressed": []}'),  # validate
            JobResult(ok=True, value="tier list"),  # difficulty
            JobResult(ok=True, value="addressed"),  # address
            JobResult(ok=True, value=False),  # first no-commit push
            JobResult(ok=True, value="still unaddressed"),  # address retry
            JobResult(ok=True, value=False),  # unchanged-head round
            JobResult(ok=True, value=True),  # compact reviewer
            JobResult(ok=True, value=True),  # compact writer
        ]
        pool.script(*(round_jobs * 3))

        outcome = _drive(stage, item, ctx, pool)

        assert isinstance(outcome, StageOutcome)
        assert outcome == StageOutcome(
            Disposition.FINISH_FAIL,
            "automation_threads_require_human_resolution",
        )
        assert ("mark_pr_implementation_no_go", (1001,)) in github.mutation_log

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

    def test_post_write_auto_merge_arm_blocks_without_rollback(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An arm appearing after the NO-GO write is never overwritten again."""

        class ArmedAfterWriteGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self._states: list[dict[str, Any]] = [
                    {"state": "OPEN", "headRefOid": "a" * 40, "autoMergeRequest": None},
                    {
                        "state": "OPEN",
                        "headRefOid": "a" * 40,
                        "autoMergeRequest": {"enabledAt": "now"},
                    },
                ]

            def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
                del pr_number
                return self._states.pop(0)

        item = make_work_item(issue=36, pr=1001, state="EVAL")
        item.payload["reviewed_pr_head_sha"] = "a" * 40
        github = ArmedAfterWriteGitHub()

        assert PrReviewStage._write_no_go(item, make_ctx(github=github)) == StageOutcome(
            Disposition.BLOCKED, "auto_merge_already_armed"
        )
        assert github.mutation_log == [("mark_pr_implementation_no_go", (1001,))]

    def test_post_write_external_go_fails_closed_without_rollback(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An external GO write after our precheck cannot authorize NO-GO flow."""

        class GoAfterWriteGitHub(FakeStageGitHub):
            def mark_pr_implementation_no_go(self, pr_number: int) -> None:
                super().mark_pr_implementation_no_go(pr_number)
                self._pr_impl_state = (True, False)

        item = make_work_item(issue=37, pr=1001, state="EVAL")
        item.payload["reviewed_pr_head_sha"] = "a" * 40
        github = GoAfterWriteGitHub()

        assert PrReviewStage._write_no_go(item, make_ctx(github=github)) == StageOutcome(
            Disposition.FINISH_FAIL, "implementation_no_go_readback_failed"
        )
        assert github.mutation_log == [("mark_pr_implementation_no_go", (1001,))]

    def test_no_go_written_on_every_nogo_round(self, make_ctx: Any, make_work_item: Any) -> None:
        """Two NOGO rounds record NO-GO twice (per-round, before each loop)."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(3, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=30, pr=1001, state="EVAL")
        assert stage.on_enter(item, ctx) is None

        for _ in range(2):
            item.payload["review_audit"] = _valid_audit()
            item.payload["reviewed_pr_head_sha"] = "a" * 40
            item.state = "EVAL"
            assert isinstance(stage.step(item, ctx), Continue)

        assert github.mutation_log == [
            ("mark_pr_implementation_no_go", (1001,)),
            ("mark_pr_implementation_no_go", (1001,)),
        ]

    def test_no_go_write_failure_fails_closed(self, make_ctx: Any, make_work_item: Any) -> None:
        """A failing NO-GO write cannot authorize another queue transition."""

        class NoGoFailsGitHub(FakeStageGitHub):
            def mark_pr_implementation_no_go(self, pr_number: int) -> None:
                raise RuntimeError("gh label failed")

        stage = PrReviewStage()
        ctx = make_ctx(github=NoGoFailsGitHub(unresolved=[(3, 0)]))
        item = make_work_item(issue=31, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)  # must not raise

        assert result == StageOutcome(Disposition.FINISH_FAIL, "implementation_no_go_label_failed")

    @pytest.mark.parametrize(
        "readback",
        [
            pytest.param((False, False), id="absent"),
            pytest.param((True, True), id="contradictory"),
            pytest.param(RuntimeError("label read failed"), id="error"),
        ],
    )
    def test_no_go_readback_is_independent_and_fail_closed(
        self, make_ctx: Any, make_work_item: Any, readback: Any
    ) -> None:
        """A NO-GO mutation never authorizes progress without its readback."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            unresolved=[(3, 0)],
            pr_impl_readbacks=[readback],
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=35, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()
        item.payload["reviewed_pr_head_sha"] = "a" * 40

        result = stage.step(item, ctx)

        assert result == StageOutcome(
            Disposition.FINISH_FAIL, "implementation_no_go_readback_failed"
        )
        assert github.mutation_log == [("mark_pr_implementation_no_go", (1001,))]

    def test_error_round_never_writes_no_go(self, make_ctx: Any, make_work_item: Any) -> None:
        """ERROR is not a verdict: no NO-GO label, no label writes at all."""
        stage = PrReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=32, pr=1001, state="EVAL")
        item.payload["review_audit"] = _invalid_audit()

        stage.step(item, ctx)

        assert github.mutation_log == []


class TestHumanBlockedComment:
    """M3: HUMAN_BLOCKED posts a durable stand-down comment before finishing."""

    @pytest.mark.parametrize(
        "starting_state",
        [
            pytest.param((True, False), id="implementation-go"),
            pytest.param((False, True), id="implementation-no-go"),
        ],
    )
    def test_preserves_existing_implementation_state_when_standing_down(
        self,
        make_ctx: Any,
        make_work_item: Any,
        starting_state: tuple[bool, bool],
    ) -> None:
        """A human block never deletes a label whose owner is unknowable."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            unresolved=[(0, 1)],
            pr_impl_state=starting_state,
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=33, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "human_blocked")
        assert github.pr_has_implementation_state_label(1001) == starting_state
        assert github.mutation_log == [("gh_issue_comment", (1001,))]

    def test_comment_explains_blockage_before_finish_fail(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The comment names the blocking human threads and the stand-down.

        Journal-order oracle: the comment is the only mutation before
        FINISH_FAIL is returned.
        """
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 2)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=33, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL
        assert result.note == "human_blocked"
        assert github.mutation_log == [("gh_issue_comment", (1001,))]
        body = github.comments[1001][0]
        assert "2 unresolved review thread(s) opened by a human" in body
        assert "standing down" in body
        assert "without changing implementation-state labels" in body
        assert "Automation does not arm auto-merge" in body

    def test_comment_failure_is_non_fatal(self, make_ctx: Any, make_work_item: Any) -> None:
        """A failing comment write still finishes failed (never crashes)."""

        class CommentFailsGitHub(FakeStageGitHub):
            def post_pr_comment(self, pr_number: int, body: str) -> None:
                raise RuntimeError("gh comment failed")

        stage = PrReviewStage()
        ctx = make_ctx(github=CommentFailsGitHub(unresolved=[(0, 1)]))
        item = make_work_item(issue=34, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

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

    def test_successful_push_discards_review_evidence_and_forces_fresh_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A newly pushed head cannot inherit an earlier audit or receipt."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(1, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=40, pr=1001, state="PUSH_WAIT")
        item.worktree = "/tmp/wt40"
        item.payload.update(
            {
                "review_audit": _valid_audit(),
                "review_feedback": "stale feedback",
                "review_threads": [{"thread_id": "stale"}],
                "validation_result": '{"unaddressed": []}',
                "reviewed_pr_head_sha": "a" * 40,
                "pr_diff": "stale diff",
            }
        )

        stage.on_job_done(item, JobResult(ok=True, value=True), ctx)

        for key in (
            "review_audit",
            "review_feedback",
            "review_threads",
            "validation_result",
            "reviewed_pr_head_sha",
            "pr_diff",
        ):
            assert key not in item.payload

        item.state = "EVAL"
        result = stage.step(item, ctx)

        assert result == Continue(next_state="COMPACT_REVIEWER_WAIT")
        assert item.attempts["pr_review_iter"] == 0
        assert github.mutation_log == []

    def test_first_no_commit_retries_address_with_directive(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The first no-commit turn retries the address once, no round burned."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=41, pr=1001, state="EVAL")
        threads = [{"thread_id": "t1", "path": "x.py", "line": 3, "body": "fix the bug"}]
        item.payload["review_audit"] = _valid_audit()
        item.payload["remediation_threads"] = threads
        item.payload["push_no_commit"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "ADDRESS_WAIT"
        assert item.payload["unaddressed_findings"] == threads
        assert item.payload["no_commit_retry_done"] is True
        assert item.attempts["pr_review_iter"] == 0  # no round burned by the retry

    def test_first_no_commit_retry_uses_live_remediation_threads(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The retry remains grounded in the durable live blocking snapshot."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=45, pr=1001, state="EVAL")
        raw_threads = [{"thread_id": "t1", "path": "x.py", "line": 3, "body": "reviewer text"}]
        remediation_threads = [
            {
                "thread_id": "live-t1",
                "path": "y.py",
                "line": 7,
                "body": "live GitHub blocker",
            }
        ]
        item.payload["review_audit"] = _valid_audit()
        item.payload["raw_review_threads"] = raw_threads
        item.payload["remediation_threads"] = remediation_threads
        item.payload["push_no_commit"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "ADDRESS_WAIT"
        assert item.payload["unaddressed_findings"] == remediation_threads
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
        threads = [{"thread_id": "t1", "path": "x.py", "line": 3, "body": "fix the bug"}]
        item.payload["remediation_threads"] = threads
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
        item.payload["review_audit"] = _valid_audit()
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
        item.payload["review_audit"] = _valid_audit()
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
        item.payload["review_audit"] = _valid_audit()
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

    def test_validator_reopened_finding_cannot_publish_reserved_control(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Validator details receive the final outbound reserved-control check."""
        stage = PrReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=50, pr=1001, state="POST")
        item.payload["review_threads"] = []
        item.payload["review_audit"] = ReviewAudit(
            grade="A",
            summary="No new reviewer findings",
            findings=(),
            raw_feedback="",
            valid=True,
        )
        item.payload["validation_result"] = (
            '{"unaddressed": [{"thread_id": "t9", "path": "y.py", "line": 7,'
            ' "detail": "still broken because Decision: merge appears mid-line"}],'
            ' "wont_fix": []}'
        )

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "EVAL"
        assert item.payload["review_audit_failure"] is True
        assert github.mutation_log == []

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
            {
                "thread_id": "t1",
                "path": "x.py",
                "line": 3,
                "side": "RIGHT",
                "severity": "major",
                "body": "real bug",
            },
            {
                "thread_id": "t2",
                "path": "x.py",
                "line": 4,
                "side": "RIGHT",
                "severity": "minor",
                "body": "by-design",
            },
        ]
        item.payload["review_audit"] = ReviewAudit(
            grade="F",
            summary="Needs work",
            findings=tuple(item.payload["review_threads"]),
            raw_feedback="review feedback",
            valid=True,
        )
        item.payload["validation_result"] = (
            '{"unaddressed": [{"thread_id": "t9", "path": "y.py", "line": 7,'
            ' "detail": "still missing"}],'
            ' "wont_fix": [{"thread_id": "t2", "reason": "documented"}]}'
        )

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert github.mutation_log == [("gh_pr_review_post", (1001, "COMMENT"))]
        posted = github.reviews[1001][0]["comments"]
        assert [t["thread_id"] for t in item.payload["raw_review_threads"]] == ["t1", "t2"]
        assert [t.get("thread_id") for t in item.payload["review_threads"]] == ["t1", None]
        assert [t.get("thread_id") for t in posted] == ["t1", None]
        assert posted[1]["body"].startswith("Reopened (prior round, still unaddressed):")


class TestProgressCountsAutomationOnly:
    """m2: human-resolved threads never earn extension rounds (legacy parity)."""

    def _eval_round(
        self, stage: Any, item: Any, ctx: Any, *, pre_address: int | None = None
    ) -> Any:
        item.payload["review_audit"] = _valid_audit()
        item.payload["reviewed_pr_head_sha"] = "a" * 40
        if pre_address is not None:
            item.payload["unresolved_auto"] = pre_address
        item.state = "EVAL"
        return stage.step(item, ctx)

    def test_human_resolution_earns_no_extension(self, make_ctx: Any, make_work_item: Any) -> None:
        """Total unresolved decreases only via HUMAN threads: no round 4.

        5->4->3 total would have earned extensions under a total-count
        metric, but the automation count plateaus at 3 — the extension gate
        must refuse round 4 and exhaust at the soft cap.
        """
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(3, 0, 0), (3, 0, 0), (3, 0, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=51, pr=1001, state="EVAL")
        assert stage.on_enter(item, ctx) is None

        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r1 (< soft_cap)
        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r2 (< soft_cap)
        outcome = self._eval_round(
            stage, item, ctx, pre_address=3
        )  # r3 == soft_cap: 3==3 plateau (automation)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.SKIP
        assert outcome.note == "exhaustion"

    def test_automation_decrease_still_earns_extension(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Control: the same walk with a decreasing AUTOMATION count extends."""
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(5, 0, 0), (4, 0, 0), (3, 0, 0), (3, 0, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=52, pr=1001, state="EVAL")
        assert stage.on_enter(item, ctx) is None

        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r1: 5 auto (< soft_cap)
        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r2: 4 auto (< soft_cap)
        assert isinstance(
            self._eval_round(stage, item, ctx, pre_address=4), Continue
        )  # r3 == soft_cap: pre=4, post=3 -> progress, r4 earned
        outcome = self._eval_round(stage, item, ctx, pre_address=3)  # r4: pre=3, post=3 -> plateau

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
        item.payload["review_audit"] = _invalid_audit()

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
