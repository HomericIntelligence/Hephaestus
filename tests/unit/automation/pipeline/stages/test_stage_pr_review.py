"""Tests for the PR-review stage (doc section "5. pr_review")."""

from __future__ import annotations

import io
import json
import sys
import tarfile
import threading
import time
from collections import deque
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from hephaestus.agents.execution_policy import AgentRole
from hephaestus.agents.pi_session import create_pi_binding
from hephaestus.automation.pipeline.github_jobs import (
    DeliverReplyHandoffRequest,
    FrozenJson,
    GitHubJob,
    PrReviewReconciled,
    ReconcilePrReviewRequest,
)
from hephaestus.automation.pipeline.jobs import (
    AgentJob,
    BuildTestJob,
    CompactJob,
    GitJob,
    JobResult,
)
from hephaestus.automation.pipeline.reply_handoff import (
    implementation_reply_handoff_journal_entry,
    journaled_implementation_reply_handoff,
)
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import (
    Continue,
    ImplementationThreadReplyResult,
    JobRequest,
    StageOutcome,
    pr_review as stage_module,
)
from hephaestus.automation.pipeline.stages.pr_review import (
    ADOPT_WORKTREE_WAIT,
    CLEANUP_REVIEW_WORKTREE_WAIT,
    DIRECT_PUSH_REMOTE_CHANGED_RESTART_CAP,
    DIRECT_PUSH_RETRY_CAP,
    HOST_VERIFICATION_WAIT,
    REVIEW_CHECKOUT_WAIT,
    REVIEW_ERROR_RETRY_CAP,
    PrReviewStage,
    _address_replies,
    _implementation_reply_handoff,
    _is_postable_finding,
    _normalize_remediation_threads,
    _parse_validation_result,
    _pr_is_current_open_head,
    _reviewer_thread_decisions,
    _validation_receipt_fingerprints,
    _validation_thread_snapshots,
    _without_duplicate_live_findings,
)
from hephaestus.automation.pipeline.stages.pr_review_threads import _scope_retraction_paths
from hephaestus.automation.pipeline.stages.pr_review_verification import (
    _FULL_UNIT_COVERAGE_SPEC,
)
from hephaestus.automation.pipeline.work_item import ItemKind
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner
from hephaestus.automation.review_audit import ReviewAudit, parse_review_audit
from hephaestus.automation.review_journal import IssueComment
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


def _make_hephaestus_checkout(root: Path) -> str:
    """Create the minimum immutable-checkout markers for the host profile."""
    (root / "pyproject.toml").write_text("[project]\nname = 'HomericIntelligence-Hephaestus'\n")
    (root / "hephaestus").mkdir()
    (root / "tests").mkdir()
    return str(root)


def test_pr_review_post_dispatches_without_inline_github_calls(
    make_ctx: Any,
    make_work_item: Any,
) -> None:
    """POST freezes one reconciliation request within a small dispatch bound."""

    class InlineGitHubForbidden:
        def list_unresolved_review_threads(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("GitHub thread read ran inline")

        def reviewer_validation_receipts(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("GitHub receipt read ran inline")

        def pr_review_context(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("GitHub metadata read ran inline")

        def reconcile_reviewer_validated_threads(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("GitHub reconciliation ran inline")

        def post_review_threads(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("GitHub review publication ran inline")

    item = make_work_item(issue=3, pr=7, state="POST")
    item.payload.update(
        {
            "reviewed_pr_head_sha": "a" * 40,
            "review_audit": _valid_audit(),
            "review_threads": [],
            "pr_diff": "diff --git a/a.py b/a.py",
        }
    )
    stage = PrReviewStage()
    ctx = make_ctx(github=InlineGitHubForbidden())

    started = time.monotonic()
    result = stage.step(item, ctx)
    elapsed = time.monotonic() - started

    assert isinstance(result, JobRequest)
    assert isinstance(result.job, GitHubJob)
    assert isinstance(result.job.request, ReconcilePrReviewRequest)
    assert result.job.request.reviewed_head_sha == "a" * 40
    assert result.job.request.findings.thaw() == []
    assert elapsed < 0.25

    stage.on_job_done(
        item,
        JobResult(
            ok=True,
            value=PrReviewReconciled(
                request=result.job.request,
                action="apply",
                posted_receipts=FrozenJson.snapshot([]),
                unresolved_threads=FrozenJson.snapshot([]),
                remediation_threads=FrozenJson.snapshot([]),
            ),
        ),
        ctx,
    )
    assert item.state == "POST"
    item.state = result.on_done_state
    assert stage.step(item, ctx) == Continue(next_state="EVAL")


def test_pr_review_recovery_handoff_dispatches_without_inline_github_calls(
    make_ctx: Any,
    make_work_item: Any,
) -> None:
    """Recovery freezes the exact handoff before any GitHub state read or reply."""

    class InlineGitHubForbidden:
        def gh_pr_state(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("GitHub head read ran inline")

        def post_implementation_thread_replies(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("GitHub reply post ran inline")

    item = make_work_item(issue=3, pr=7, state="EVAL")
    item.payload["pending_implementation_reply_handoff"] = {
        "head_sha": "a" * 40,
        "batch_nonce": "b" * 32,
        "threads": [
            {
                "id": "thread-1",
                "path": "a.py",
                "line": 1,
                "side": "RIGHT",
                "body": "fix",
                "comments": [{"id": "comment-1", "body": "fix"}],
            }
        ],
        "replies": {"thread-1": "Fixed."},
    }
    stage = PrReviewStage()
    ctx = make_ctx(github=InlineGitHubForbidden())

    assert stage.step(item, ctx) == Continue(next_state="RECOVERY_REPLY_WAIT")
    item.state = "RECOVERY_REPLY_WAIT"
    started = time.monotonic()
    result = stage.step(item, ctx)
    elapsed = time.monotonic() - started

    assert isinstance(result, JobRequest)
    assert isinstance(result.job, GitHubJob)
    assert isinstance(result.job.request, DeliverReplyHandoffRequest)
    assert result.job.request.handoff.thaw() == item.payload["pending_implementation_reply_handoff"]
    assert elapsed < 0.25


def _drive(stage: Any, item: Any, ctx: Any, pool: FakeWorkerPool, max_steps: int = 80) -> Any:
    """Drive a stage through the canonical FakeWorkerPool until an outcome."""
    entry = stage.on_enter(item, ctx)
    if entry is not None:
        return entry
    for _ in range(max_steps):
        result = _complete_github_job(stage, item, ctx)
        if isinstance(result, Continue):
            item.state = result.next_state
            continue
        if isinstance(result, JobRequest):
            if (
                isinstance(result.job, GitJob)
                and result.job.op == "create_worktree"
                and result.job.descr == "direct_pr_review_worktree"
            ):
                stage.on_job_done(
                    item,
                    JobResult(ok=True, value={"path": "/tmp/detached-review", "dirty": False}),
                    ctx,
                )
                item.state = result.on_done_state
                continue
            if isinstance(result.job, GitJob) and result.job.op == "verify_pr_review_checkout":
                stage.on_job_done(
                    item,
                    JobResult(ok=True, value={"ready": True, "diff": "checkout diff"}),
                    ctx,
                )
                item.state = result.on_done_state
                continue
            pool.submit(result.job, result.on_done_state)
            _handle, job_result = pool.completion_q.get_nowait()
            assert not job_result.interrupted  # on_job_done contract precondition
            stage.on_job_done(item, job_result, ctx)
            item.state = result.on_done_state
            continue
        return result
    raise AssertionError("stage driver did not terminate")


def _complete_github_job(stage: PrReviewStage, item: Any, ctx: Any) -> Any:
    """Run one PR-review GitHub request only after its stage dispatch."""
    request = stage.step(item, ctx)
    if request == Continue(next_state="RECOVERY_REPLY_WAIT"):
        item.state = "RECOVERY_REPLY_WAIT"
        request = stage.step(item, ctx)
    if not isinstance(request, JobRequest) or not isinstance(request.job, GitHubJob):
        return request
    try:
        if isinstance(request.job.request, ReconcilePrReviewRequest):
            receipt: object = PipelineGitHubJobRunner._reconcile_pr_review(
                request.job.request,
                ctx.github,
            )
        elif isinstance(request.job.request, DeliverReplyHandoffRequest):
            from hephaestus.automation.pipeline.reply_handoff import attempt_reply_handoff

            receipt = attempt_reply_handoff(request.job.request, ctx.github)
        else:  # pragma: no cover - PR review owns two GitHub operations
            raise AssertionError(f"unexpected request: {request.job.request!r}")
        result = JobResult(ok=True, value=receipt)
    except Exception as error:
        result = JobResult(ok=False, error=f"{type(error).__name__}: {error}")
    stage.on_job_done(item, result, ctx)
    item.state = request.on_done_state
    return stage.step(item, ctx)


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
        ctx = make_ctx(github=FakeStageGitHub(pr_head_branch="1-auto-impl"))
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

    def test_on_enter_stows_the_writer_checkout_before_creating_a_detached_reviewer(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Every PR review gets a disposable detached checkout, including new PRs."""
        stage = PrReviewStage()
        ctx = make_ctx(github=FakeStageGitHub(pr_head_branch="1-auto-impl"))
        item = make_work_item(issue=1, pr=1001, state="ENTER")
        item.branch = "1-auto-impl"
        item.worktree = "/tmp/implementation-writer"

        assert stage.on_enter(item, ctx) is None
        assert item.worktree == ""
        assert item.payload["writer_worktree"] == "/tmp/implementation-writer"

        request = stage.step(item, ctx)

        assert isinstance(request, JobRequest)
        assert isinstance(request.job, GitJob)
        assert request.job.op == "create_worktree"
        assert request.job.kwargs["isolated"] is True

    def test_on_enter_routes_unreplied_threads_to_implementation_before_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Existing threads without current-head responses never trigger a new audit."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            unresolved=[(1, 0)], pr_head_branch="1-auto-impl-direct-" + "b" * 32
        )
        item = make_work_item(issue=1, pr=1001, kind=ItemKind.PR, state="ENTER")

        outcome = stage.on_enter(item, make_ctx(github=github))

        assert outcome == StageOutcome(Disposition.FAIL_BACK, "implementation_remediation")
        assert item.branch == "1-auto-impl-direct-" + "b" * 32
        assert item.payload["existing_pr"] is True
        assert item.payload["implementation_remediation"] is True
        assert item.payload["remediation_threads"] == [
            {
                "thread_id": "live-thread-1001-0",
                "path": "a.py",
                "line": 1,
                "body": "<!-- hephaestus-severity: major -->\nfinding",
            }
        ]
        assert item.payload["remediation_thread_snapshots"][0]["id"] == "live-thread-1001-0"
        assert ("mark_pr_implementation_no_go", (1001,)) in github.mutation_log

    def test_on_enter_routes_fully_replied_threads_to_comment_validation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A current-head implementation reply proceeds to reviewer validation only."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            unresolved=[(1, 0)], pr_head_branch="1-auto-impl-direct-" + "b" * 32
        )
        github._thread_replies["live-thread-1001-0"] = [
            {
                "id": "implementation-reply-live-thread-1001-0",
                "author": "hephaestus[bot]",
                "body": "[Response] Fixed and tested.",
            }
        ]
        item = make_work_item(issue=1, pr=1001, kind=ItemKind.PR, state="ENTER")
        ctx = make_ctx(github=github)

        assert stage.on_enter(item, ctx) is None
        assert item.payload["existing_pr"] is True
        assert item.payload["reviewer_comment_validation_only"] is True

        item.state = REVIEW_CHECKOUT_WAIT
        item.payload["review_checkout_ready"] = True
        item.payload["review_checkout_expected_head"] = "a" * 40
        result = _complete_github_job(stage, item, ctx)

        assert result == Continue(next_state="VALIDATE_WAIT")
        assert "review_audit" not in item.payload

    def test_on_enter_fails_closed_when_existing_thread_read_fails(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A failed thread read cannot be bypassed by starting a new review."""

        class ThreadReadFailsGitHub(FakeStageGitHub):
            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                raise RuntimeError("GitHub unavailable")

        stage = PrReviewStage()
        item = make_work_item(issue=1, pr=1001, kind=ItemKind.PR, state="ENTER")

        assert stage.on_enter(item, make_ctx(github=ThreadReadFailsGitHub())) == StageOutcome(
            Disposition.FINISH_FAIL, "review_threads_unavailable"
        )

    def test_checkout_rechecks_new_unreplied_thread_before_submitting_a_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A thread appearing after entry goes to implementation, not a broad audit."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(1, 0)])
        item = make_work_item(issue=1, pr=1001, state=REVIEW_CHECKOUT_WAIT)
        item.worktree = "/tmp/detached-review"
        item.payload.update(
            {
                "review_checkout_ready": True,
                "review_checkout_expected_head": "a" * 40,
                "review_worktree_expected_head": "a" * 40,
                "pr_diff": "",
            }
        )

        result = stage.step(item, make_ctx(github=github))

        assert result == Continue(next_state="ADDRESS_WAIT")
        assert item.payload["remediation_threads"][0]["thread_id"] == "live-thread-1001-0"

    def test_checkout_rejects_empty_diff_before_review_or_go(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An empty diff must revoke all round-scoped review evidence."""
        stage = PrReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state=REVIEW_CHECKOUT_WAIT)
        item.worktree = "/tmp/detached-review"
        item.payload.update(
            {
                "review_worktree": item.worktree,
                "review_checkout_expected_head": "a" * 40,
                "review_worktree_expected_head": "a" * 40,
                "review_checkout_ready": True,
                "reviewed_pr_head_sha": "a" * 40,
                "reviewed_pr_proof_generation": 7,
                "host_verification_receipts": [{"head_sha": "a" * 40, "ok": False}],
                "pr_diff": "\n\t",
                "review_audit": _valid_audit(),
                "review_feedback": [{"body": "stale finding"}],
                "review_changed_paths": ["example.py"],
            }
        )

        result = stage.step(item, ctx)

        assert result == Continue(next_state=CLEANUP_REVIEW_WORKTREE_WAIT)
        assert item.payload["empty_diff_reimplementation"] is True
        assert "reviewed_pr_head_sha" not in item.payload
        assert "host_verification_receipts" not in item.payload
        assert "pr_diff" not in item.payload
        assert "review_audit" not in item.payload
        assert "review_feedback" not in item.payload
        assert "review_changed_paths" not in item.payload
        assert "reviewed_pr_proof_generation" in item.payload
        assert github.mutation_log == []

        item.state = CLEANUP_REVIEW_WORKTREE_WAIT
        removal = stage.step(item, ctx)
        assert isinstance(removal, JobRequest)
        assert isinstance(removal.job, GitJob)
        assert removal.job.op == "remove_worktree"
        stage.on_job_done(item, JobResult(ok=True), ctx)

        assert stage.step(item, ctx) == StageOutcome(Disposition.FAIL_BACK, "empty_pr_diff")
        assert item.payload["empty_diff_reimplementation"] is True

    def test_host_verification_rechecks_new_unreplied_thread_before_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A thread appearing during host checks cannot lead to a broad review batch."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(1, 0)])
        item = make_work_item(issue=1, pr=1001, state=HOST_VERIFICATION_WAIT)
        item.worktree = "/tmp/detached-review"
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "host_verification_repository_profile": "hephaestus",
                "pr_diff": "diff --git a/example.py b/example.py\n+new line\n",
            }
        )
        specs = stage_module._host_verification_specs(item.payload["pr_diff"])
        item.payload["host_verification_receipts"] = [
            {
                "head_sha": "a" * 40,
                "argv": list(spec.argv),
                "immutable_source": True,
                "ok": True,
                "stdout_tail": "",
                "stderr_tail": "",
            }
            for spec in specs
        ]

        result = stage.step(item, make_ctx(github=github))

        assert result == Continue(next_state="ADDRESS_WAIT")
        assert item.payload["remediation_threads"][0]["thread_id"] == "live-thread-1001-0"

    def test_comment_validation_resolves_threads_without_posting_a_second_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Comment validation reconciles the original thread instead of publishing findings."""

        class ResolvingGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.live = [
                    {
                        "id": "thread-1",
                        "path": "a.py",
                        "line": 3,
                        "side": "RIGHT",
                        "body": "[Review] Fix the guard.",
                        "comments": [
                            {
                                "id": "review-comment-1",
                                "author": "reviewer",
                                "body": "[Review] Fix the guard.",
                            },
                            {
                                "id": "implementation-reply-thread-1",
                                "author": "hephaestus[bot]",
                                "body": "[Response] Added the guard and tests.",
                            },
                        ],
                    }
                ]

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(thread) for thread in self.live]

            def reviewer_validation_receipts(
                self,
                pr_number: int,
                *,
                reviewed_head_sha: str,
                threads: list[dict[str, Any]],
            ) -> list[dict[str, Any]]:
                del pr_number
                return [
                    {
                        **thread,
                        "implementation_reply_id": "implementation-reply-thread-1",
                        "implementation_reply_body": "[Response] Added the guard and tests.",
                        "implementation_head_sha": reviewed_head_sha,
                    }
                    for thread in threads
                ]

            def reconcile_reviewer_validated_threads(self, *args: Any, **kwargs: Any) -> Any:
                del args, kwargs
                self.live = []
                return SimpleNamespace(
                    resolved_thread_ids=("thread-1",),
                    feedback_thread_ids=(),
                    blocked_thread_ids=(),
                )

        github = ResolvingGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload.update(
            {
                "reviewer_comment_validation_only": True,
                "reviewed_pr_head_sha": "a" * 40,
                "validation_result": '{"resolved":["thread-1"],"unaddressed":[]}',
            }
        )

        result = _complete_github_job(PrReviewStage(), item, ctx)

        assert result == Continue(next_state="EVAL")
        assert 1001 not in github.reviews
        assert item.payload["review_audit"] == ReviewAudit(
            grade="A",
            summary="Reviewer validated the implementation responses to all open threads.",
            findings=(),
            raw_feedback="",
            valid=True,
        )

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

        result = _complete_github_job(stage, item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "verify_pr_review_checkout"
        assert result.job.kwargs["expected_head_sha"] == "a" * 40
        assert result.job.kwargs["base_branch"] == "main"
        assert "require_base_ancestor" not in result.job.kwargs
        assert result.on_done_state == REVIEW_CHECKOUT_WAIT
        assert "reviewed_pr_head_sha" not in item.payload

    def test_direct_pr_binds_a_single_read_only_snapshot_before_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A direct PR review never rebases or publishes the reviewed branch."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            pr_review_context={
                "pr_diff": "diff --git a/a.py b/a.py\n+new\n",
                "pr_description": "Closes #1",
                "pr_head_sha": "a" * 40,
                "pr_base_sha": "b" * 40,
                "pr_base_branch": "main",
            }
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, kind=ItemKind.PR, state="REVIEW_WAIT")
        item.worktree = "/tmp/repo/review-worktree"
        item.branch = "review-branch"
        item.payload["direct_pr_worktree"] = item.worktree

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "verify_pr_review_checkout"
        assert result.job.kwargs == {
            "worktree_path": "/tmp/repo/review-worktree",
            "branch": "review-branch",
            "expected_head_sha": "a" * 40,
            "expected_base_sha": "b" * 40,
            "base_branch": "main",
            "pr_number": 1001,
        }
        assert result.on_done_state == REVIEW_CHECKOUT_WAIT
        assert "direct_pr_rebase_attempted" not in item.payload

    def test_read_only_direct_pr_does_not_rebase_or_publish_before_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A fork head is reviewed from its immutable PR ref without a write attempt."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            pr_head_writable=False,
            pr_review_context={
                "pr_diff": "diff --git a/a.py b/a.py\n+new\n",
                "pr_description": "Closes #1",
                "pr_head_sha": "a" * 40,
                "pr_base_branch": "main",
            },
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, kind=ItemKind.PR, state="REVIEW_WAIT")
        item.worktree = "/tmp/repo/review-worktree"
        item.branch = "fork-review-branch"
        item.payload["direct_pr_worktree"] = item.worktree

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "verify_pr_review_checkout"
        assert "require_base_ancestor" not in result.job.kwargs
        assert "direct_pr_rebase_attempted" not in item.payload

    def test_direct_pr_checkout_head_drift_finishes_without_rebase_or_retry(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A changed snapshot ends this review attempt without mutating the branch."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            pr_review_context={
                "pr_diff": "diff --git a/a.py b/a.py\n+new\n",
                "pr_description": "Closes #1",
                "pr_head_sha": "b" * 40,
                "pr_base_branch": "main",
            }
        )
        ctx = make_ctx(github=github)
        item = make_work_item(
            issue=1,
            pr=1001,
            kind=ItemKind.PR,
            state=REVIEW_CHECKOUT_WAIT,
        )
        item.worktree = "/tmp/repo/review-worktree"
        item.branch = "review-branch"
        item.payload.update(
            {
                "direct_pr_worktree": item.worktree,
                "review_checkout_expected_head": "a" * 40,
                "review_checkout_ready": False,
            }
        )

        retry = stage.step(item, ctx)

        assert retry == StageOutcome(Disposition.FINISH_FAIL, "review_checkout_head_drift")

    def test_review_cleanup_removes_detached_checkout_without_retaining_it(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A direct PR review has no writer checkout to restore after cleanup."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(
            issue=1,
            pr=1001,
            kind=ItemKind.PR,
            state=CLEANUP_REVIEW_WORKTREE_WAIT,
        )
        item.worktree = "/tmp/detached-review"
        item.payload.update(
            {
                "review_worktree": item.worktree,
                "review_worktree_expected_head": "a" * 40,
                "review_worktree_cleanup_done": "pending",
                "review_worktree_cleanup_outcome": Disposition.FAIL_BACK.value,
                "review_worktree_cleanup_note": "implementation_remediation",
            }
        )

        removal = stage.step(item, ctx)
        assert isinstance(removal, JobRequest)
        assert isinstance(removal.job, GitJob)
        assert removal.job.op == "remove_worktree"
        assert removal.job.kwargs["expected_head"] == "a" * 40
        assert removal.job.kwargs["expected_detached"] is True
        stage.on_job_done(item, JobResult(ok=True), ctx)

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.FAIL_BACK, "implementation_remediation"
        )
        assert item.worktree == ""

    def test_review_cleanup_retry_restarts_from_a_fresh_snapshot(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A retry never re-enters cleanup after removing its old snapshot."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(
            issue=1,
            pr=1001,
            kind=ItemKind.PR,
            state=CLEANUP_REVIEW_WORKTREE_WAIT,
        )
        item.worktree = "/tmp/detached-review"
        item.branch = "review-branch"
        item.payload.update(
            {
                "existing_pr": True,
                "writer_worktree": "/tmp/implementation-writer",
                "review_worktree": item.worktree,
                "review_worktree_expected_head": "a" * 40,
                "review_worktree_cleanup_done": "pending",
                "review_worktree_cleanup_outcome": Disposition.RETRY.value,
                "review_worktree_cleanup_note": "review audit format failure",
            }
        )

        removal = stage.step(item, ctx)
        assert isinstance(removal, JobRequest)
        stage.on_job_done(item, JobResult(ok=True), ctx)

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.RETRY, "review audit format failure"
        )
        assert item.state == "ENTER"
        assert item.worktree == ""
        assert item.payload["writer_worktree"] == "/tmp/implementation-writer"

        fresh_snapshot = stage.step(item, ctx)
        assert isinstance(fresh_snapshot, JobRequest)
        assert fresh_snapshot.job.descr == "direct_pr_review_worktree"

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
            JobResult(
                ok=True,
                value={
                    "ready": True,
                    "diff": "checkout diff for A",
                    "changed_paths": ["old.py", "new.py"],
                },
            ),
            ctx,
        )
        item.state = barrier.on_done_state
        review = stage.step(item, ctx)

        assert isinstance(review, JobRequest)
        assert isinstance(review.job, AgentJob)
        assert review.job.prompt_kwargs["pr_diff"] == "checkout diff for A"
        assert item.payload["review_changed_paths"] == ["old.py", "new.py"]

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
            "sync_to_remote": False,
            "pr_number": 1001,
            "repo_root": str(ctx.paths.repo_root),
            "source_lane": "review",
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
            JobResult(
                ok=True,
                value={
                    "path": "/tmp/review-pr",
                    "dirty": False,
                },
            ),
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
        item.payload["reviewed_pr_head_sha"] = "a" * 40
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
        github = FakeStageGitHub(by_severity=[(0, 0, 0), (1, 0, 0)])
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
        post = _complete_github_job(stage, item, ctx)
        assert post == Continue(next_state="ADDRESS_WAIT")
        assert github.reviews[1001][0]["comments"] == item.payload["review_threads"]

        item.state = "ADDRESS_WAIT"
        assert stage.step(item, ctx) == StageOutcome(
            Disposition.FAIL_BACK, "implementation_remediation"
        )
        assert github.mutation_log == [
            ("gh_pr_review_post", (1001, "COMMENT")),
            ("mark_pr_implementation_no_go", (1001,)),
        ]
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

    def test_validation_job_is_read_only(self, make_ctx: Any, make_work_item: Any) -> None:
        """Review-only analysis never receives write-capable agent permissions."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="VALIDATE_WAIT")
        item.payload["reviewed_pr_head_sha"] = "a" * 40

        validation = stage.step(item, ctx)
        assert isinstance(validation, JobRequest)
        assert isinstance(validation.job, AgentJob)
        assert validation.job.sandbox == "read-only"

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
                "address_error": True,
                "address_output": "stale",
            }
        )

        result = _complete_github_job(stage, item, ctx)

        assert isinstance(result, JobRequest)
        for key in (
            "review_audit",
            "review_text",
            "review_threads",
            "raw_review_threads",
            "posted_thread_ids",
            "remediation_threads",
            "validation_result",
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
        item.payload["reviewed_pr_head_sha"] = "a" * 40

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert result.on_done_state == "POST"
        assert result.job.descr == "validate"

    def test_validate_wait_includes_fresh_head_bound_pr_metadata(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A PR-body-only remediation is visible to the reply validator."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            pr_review_context={
                "pr_title": "docs(policy): remove stale claim",
                "pr_description": "Current factual summary.\n\nCloses #1",
                "pr_head_sha": "a" * 40,
                "pr_base_branch": "main",
            }
        )
        item = make_work_item(issue=1, pr=1001, state="VALIDATE_WAIT")
        item.payload["reviewed_pr_head_sha"] = "a" * 40

        result = stage.step(item, make_ctx(github=github))

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.prompt_kwargs["pr_title"] == "docs(policy): remove stale claim"
        assert result.job.prompt_kwargs["pr_description"] == (
            "Current factual summary.\n\nCloses #1"
        )

    def test_validate_wait_fails_closed_when_fresh_metadata_head_drifted(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Metadata from a different head cannot resolve reviewed-head threads."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            pr_review_context={
                "pr_title": "changed title",
                "pr_description": "changed body",
                "pr_head_sha": "b" * 40,
                "pr_base_branch": "main",
            }
        )
        item = make_work_item(issue=1, pr=1001, state="VALIDATE_WAIT")
        item.payload["reviewed_pr_head_sha"] = "a" * 40

        result = stage.step(item, make_ctx(github=github))

        assert result == Continue(next_state="EVAL")
        assert item.payload["review_audit_failure"] is True

    def test_checkout_runs_registered_host_verification_before_primary_reviewer(
        self, tmp_path: Path, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A changed regression receives only hermetic host checks first."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state=REVIEW_CHECKOUT_WAIT)
        item.worktree = _make_hephaestus_checkout(tmp_path)
        item.payload.update(
            {
                "review_checkout_expected_head": "a" * 40,
                "review_checkout_ready": True,
                "pr_diff": (
                    "diff --git a/tests/unit/automation/pipeline/stages/test_stage_pr_review.py "
                    "b/tests/unit/automation/pipeline/stages/test_stage_pr_review.py\n"
                    "diff --git a/tests/unit/automation/pipeline/test_worker_pool.py "
                    "b/tests/unit/automation/pipeline/test_worker_pool.py\n"
                    "diff --git a/tests/performance/test_worker_pool_load.py "
                    "b/tests/performance/test_worker_pool_load.py\n"
                    "--- a/tests/performance/test_worker_pool_load.py\n"
                    "+++ b/tests/performance/test_worker_pool_load.py\n"
                    "diff --git a/coverage.toml b/coverage.toml\n"
                    "--- a/coverage.toml\n"
                    "+++ b/coverage.toml\n"
                ),
            }
        )

        request = stage.step(item, ctx)
        expected = (
            (
                "review_python_ruff_check",
                ("uv", "run", "ruff", "check", "hephaestus/", "tests/"),
            ),
            (
                "review_python_ruff_format",
                ("uv", "run", "ruff", "format", "--check", "hephaestus/", "tests/"),
            ),
            (
                "review_python_mypy",
                (
                    "uv",
                    "run",
                    "mypy",
                    "--cache-dir=/dev/null",
                    "hephaestus/",
                    "scripts/",
                    "tests/",
                ),
            ),
            (
                "review_changed_unit_test_0",
                (
                    "uv",
                    "run",
                    "pytest",
                    "-o",
                    "addopts=",
                    "tests/unit/automation/pipeline/stages/test_stage_pr_review.py",
                    "-q",
                    "--tb=short",
                ),
            ),
            (
                "review_worker_pool_agent_execution_error",
                (
                    "uv",
                    "run",
                    "pytest",
                    "-o",
                    "addopts=",
                    "tests/unit/automation/pipeline/test_worker_pool.py::TestAgentErrorHandling::test_codex_event_failure_is_explicit_agent_error",
                    "-q",
                    "--tb=short",
                ),
            ),
            (
                "review_stalled_consumer_verification",
                (
                    "uv",
                    "run",
                    "pytest",
                    "-o",
                    "addopts=",
                    "tests/performance/test_worker_pool_load.py",
                    "-q",
                    "--load-report=../scratch/outputs/worker-pool.json",
                ),
            ),
            (
                "review_full_unit_coverage",
                _FULL_UNIT_COVERAGE_SPEC.argv,
            ),
        )
        receipts: list[dict[str, object]] = []
        for index, (description, argv) in enumerate(expected):
            assert isinstance(request, JobRequest)
            assert isinstance(request.job, BuildTestJob)
            assert request.job.descr == description
            assert request.job.argv == argv
            assert request.on_done_state == "HOST_VERIFICATION_WAIT"
            assert request.job.expected_head_sha == "a" * 40
            assert request.job.immutable_source is True
            receipt = {
                "argv": list(argv),
                "error": "",
                "failure_kind": "none",
                "head_sha": "a" * 40,
                "immutable_source": True,
                "ok": True,
                "platform": "darwin",
                "status": "passed",
                "stderr_tail": "",
                "stdout_tail": f"{index + 1} passed in 0.32s",
            }
            receipts.append(receipt)
            stage.on_job_done(
                item,
                JobResult(
                    ok=True,
                    value={
                        "head_sha": "a" * 40,
                        "immutable_source": True,
                        "failure_kind": "none",
                    },
                    stdout_tail=str(receipt["stdout_tail"]),
                ),
                ctx,
            )
            item.state = request.on_done_state
            request = stage.step(item, ctx)

        review = request

        assert isinstance(review, JobRequest)
        assert isinstance(review.job, AgentJob)
        assert review.job.descr == "review"
        assert review.job.sandbox == "read-only"
        assert review.job.prompt_kwargs["host_verifications_json"] == json.dumps(
            receipts, sort_keys=True
        )

        # The coordinator calls on_job_done before installing the next state.
        # A reviewer sent from HOST_VERIFICATION_WAIT must retain its audit
        # rather than being consumed as an additional host receipt.
        audit = _valid_audit()
        stage.on_job_done(item, JobResult(ok=True, value=audit), ctx)

        assert item.payload["review_audit"] == audit
        assert item.payload["host_verification_receipts"] == receipts

    def test_comment_validation_carries_fresh_host_verification_receipts(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Reply validation receives exact-head host evidence without a broad review."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(1, 0)])
        github._thread_replies["live-thread-1001-0"] = [
            {
                "id": "implementation-reply-live-thread-1001-0",
                "author": "hephaestus[bot]",
                "body": "[Response] The regression passes on this head.",
            }
        ]
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state=REVIEW_CHECKOUT_WAIT)
        item.worktree = "/tmp/detached-review"
        item.payload.update(
            {
                "review_checkout_expected_head": "a" * 40,
                "review_checkout_ready": True,
                "reviewer_comment_validation_only": True,
                "pr_diff": (
                    "diff --git a/tests/unit/validation/test_test_layout.py "
                    "b/tests/unit/validation/test_test_layout.py\n"
                    "--- a/tests/unit/validation/test_test_layout.py\n"
                    "+++ b/tests/unit/validation/test_test_layout.py\n"
                ),
            }
        )

        result = stage.step(item, ctx)
        while isinstance(result, JobRequest) and isinstance(result.job, BuildTestJob):
            receipt = {
                "head_sha": "a" * 40,
                "argv": list(result.job.argv),
                "immutable_source": True,
                "failure_kind": "none",
                "ok": True,
                "stdout_tail": "65 passed in 0.5s",
                "stderr_tail": "",
            }
            stage.on_job_done(item, JobResult(ok=True, value=receipt), ctx)
            item.state = result.on_done_state
            result = stage.step(item, ctx)

        receipts = item.payload["host_verification_receipts"]
        assert receipts
        assert result == Continue(next_state="VALIDATE_WAIT")
        item.state = result.next_state
        validation = stage.step(item, ctx)

        assert isinstance(validation, JobRequest)
        assert isinstance(validation.job, AgentJob)
        assert validation.job.descr == "validate"
        assert json.loads(validation.job.prompt_kwargs["host_verifications_json"]) == receipts
        assert 1001 not in github.reviews

    def test_comment_validation_stays_validation_only_if_threads_resolve_during_host_checks(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A last-thread resolution during host checks must not start a broad audit."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            pr_head_branch="1-auto-impl-direct-" + "b" * 32,
            by_severity=[(1, 0, 0), (1, 0, 0), (0, 0, 0)],
        )
        github._thread_replies["live-thread-1001-0"] = [
            {
                "id": "implementation-reply-live-thread-1001-0",
                "author": "hephaestus[bot]",
                "body": "[Response] The regression passes on this head.",
            }
        ]
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        assert item.payload["reviewer_comment_validation_only"] is True

        item.state = REVIEW_CHECKOUT_WAIT
        item.worktree = "/tmp/detached-review"
        item.payload.update(
            {
                "review_checkout_expected_head": "a" * 40,
                "review_checkout_ready": True,
                "pr_diff": (
                    "diff --git a/tests/unit/validation/test_test_layout.py "
                    "b/tests/unit/validation/test_test_layout.py\n"
                    "--- a/tests/unit/validation/test_test_layout.py\n"
                    "+++ b/tests/unit/validation/test_test_layout.py\n"
                ),
            }
        )

        result = stage.step(item, ctx)
        while isinstance(result, JobRequest) and isinstance(result.job, BuildTestJob):
            receipt = {
                "head_sha": "a" * 40,
                "argv": list(result.job.argv),
                "immutable_source": True,
                "failure_kind": "none",
                "ok": True,
                "stdout_tail": "65 passed in 0.5s",
                "stderr_tail": "",
            }
            stage.on_job_done(item, JobResult(ok=True, value=receipt), ctx)
            item.state = result.on_done_state
            result = stage.step(item, ctx)

        assert result == Continue(next_state="VALIDATE_WAIT")
        item.state = result.next_state
        validation = stage.step(item, ctx)

        assert isinstance(validation, JobRequest)
        assert isinstance(validation.job, AgentJob)
        assert validation.job.descr == "validate"
        assert 1001 not in github.reviews

    def test_deleted_unit_tests_do_not_schedule_changed_pytest_specs(self) -> None:
        """Deleted tests have no new-side path for host pytest to execute."""
        deleted_path = "tests/unit/automation/pipeline/stages/test_deleted.py"
        kept_path = "tests/unit/automation/pipeline/stages/test_kept.py"
        specs = stage_module._host_verification_specs(
            f"diff --git a/{deleted_path} b/{deleted_path}\n"
            "deleted file mode 100644\n"
            f"--- a/{deleted_path}\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-def test_removed() -> None:\n"
            "-    pass\n"
            f"diff --git a/{kept_path} b/{kept_path}\n"
            f"--- a/{kept_path}\n"
            f"+++ b/{kept_path}\n"
            "@@ -1 +1 @@\n"
            "-def test_old() -> None: pass\n"
            "+def test_new() -> None: pass\n"
        )

        changed_pytest_specs = tuple(
            spec for spec in specs if spec.descr.startswith("review_changed_unit_test_")
        )

        assert len(changed_pytest_specs) == 1
        assert changed_pytest_specs[0].changed_path == kept_path
        assert kept_path in changed_pytest_specs[0].argv
        assert deleted_path not in changed_pytest_specs[0].argv

    def test_changed_conftest_verifies_containing_directory_once(self) -> None:
        """A support-only conftest change must not be a no-tests pytest target."""
        directory = "tests/unit/automation/pipeline/stages"
        conftest_path = f"{directory}/conftest.py"
        test_path = f"{directory}/test_stage_pr_review.py"
        specs = stage_module._host_verification_specs(
            f"diff --git a/{conftest_path} b/{conftest_path}\n"
            f"--- a/{conftest_path}\n"
            f"+++ b/{conftest_path}\n"
            "@@ -1 +1 @@\n-old = True\n+new = True\n"
            f"diff --git a/{test_path} b/{test_path}\n"
            f"--- a/{test_path}\n"
            f"+++ b/{test_path}\n"
            "@@ -1 +1 @@\n-old = True\n+new = True\n"
        )

        changed_pytest_specs = tuple(
            spec for spec in specs if spec.descr.startswith("review_changed_unit_test_")
        )

        assert len(changed_pytest_specs) == 1
        assert changed_pytest_specs[0].changed_path == conftest_path
        assert changed_pytest_specs[0].argv == (
            "uv",
            "run",
            "pytest",
            "-o",
            "addopts=",
            directory,
            "-q",
            "--tb=short",
        )

    def test_nested_changed_conftests_keep_shallowest_directory_once(self) -> None:
        """A parent conftest directory target already covers nested conftest changes."""
        parent_directory = "tests/unit/automation"
        child_directory = f"{parent_directory}/pipeline"
        parent_conftest_path = f"{parent_directory}/conftest.py"
        child_conftest_path = f"{child_directory}/conftest.py"
        specs = stage_module._host_verification_specs(
            f"diff --git a/{parent_conftest_path} b/{parent_conftest_path}\n"
            f"--- a/{parent_conftest_path}\n"
            f"+++ b/{parent_conftest_path}\n"
            "@@ -1 +1 @@\n-old = True\n+new = True\n"
            f"diff --git a/{child_conftest_path} b/{child_conftest_path}\n"
            f"--- a/{child_conftest_path}\n"
            f"+++ b/{child_conftest_path}\n"
            "@@ -1 +1 @@\n-old = True\n+new = True\n"
        )

        changed_pytest_specs = tuple(
            spec for spec in specs if spec.descr.startswith("review_changed_unit_test_")
        )

        assert len(changed_pytest_specs) == 1
        assert changed_pytest_specs[0].changed_path == parent_conftest_path
        assert changed_pytest_specs[0].argv == (
            "uv",
            "run",
            "pytest",
            "-o",
            "addopts=",
            parent_directory,
            "--ignore=tests/unit/automation/pipeline/test_worker_pool.py",
            "-q",
            "--tb=short",
        )

    def test_changed_ancestor_conftest_directory_preserves_nonhermetic_exclusion(
        self,
    ) -> None:
        """Directory targets must not re-collect host-excluded unit tests."""
        directory = "tests/unit/automation"
        conftest_path = f"{directory}/conftest.py"
        ordinary_path = "tests/unit/automation/stages/test_plan_review.py"
        specs = stage_module._host_verification_specs(
            f"diff --git a/{conftest_path} b/{conftest_path}\n"
            f"--- a/{conftest_path}\n"
            f"+++ b/{conftest_path}\n"
            "@@ -1 +1 @@\n-old = True\n+new = True\n"
            f"diff --git a/{ordinary_path} b/{ordinary_path}\n"
            f"--- a/{ordinary_path}\n"
            f"+++ b/{ordinary_path}\n"
            "@@ -1 +1 @@\n-old = True\n+new = True\n"
        )

        changed_pytest_specs = tuple(
            spec for spec in specs if spec.descr.startswith("review_changed_unit_test_")
        )

        assert len(changed_pytest_specs) == 1
        assert changed_pytest_specs[0].changed_path == conftest_path
        assert changed_pytest_specs[0].argv == (
            "uv",
            "run",
            "pytest",
            "-o",
            "addopts=",
            directory,
            "--ignore=tests/unit/automation/pipeline/test_worker_pool.py",
            "-q",
            "--tb=short",
        )

    def test_changed_conftest_directory_host_verification_receipt_is_head_bound(
        self, tmp_path: Path, completion_q: Any
    ) -> None:
        """The emitted conftest directory target runs through the immutable receipt path."""
        directory = "tests/unit/host_conftest_receipt"
        conftest_path = f"{directory}/conftest.py"
        specs = stage_module._host_verification_specs(
            f"diff --git a/{conftest_path} b/{conftest_path}\n"
            f"--- a/{conftest_path}\n"
            f"+++ b/{conftest_path}\n"
            "@@ -1 +1 @@\n-old = True\n+new = True\n"
        )
        spec = next(spec for spec in specs if spec.descr.startswith("review_changed_unit_test_"))
        assert spec.argv == (
            "uv",
            "run",
            "pytest",
            "-o",
            "addopts=",
            directory,
            "-q",
            "--tb=short",
        )

        source_fixture = tmp_path / "source-fixture"
        test_directory = source_fixture / directory
        test_directory.mkdir(parents=True)
        (test_directory / "conftest.py").write_text(
            "import pytest\n\n"
            "@pytest.fixture\n"
            "def conftest_marker() -> str:\n"
            "    return 'from-conftest'\n",
            encoding="utf-8",
        )
        (test_directory / "test_uses_conftest.py").write_text(
            "def test_uses_directory_conftest(conftest_marker: str) -> None:\n"
            "    assert conftest_marker == 'from-conftest'\n",
            encoding="utf-8",
        )
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            tar.add(source_fixture / "tests", arcname="tests")

        expected_head = "b" * 40
        checkout = tmp_path / "review-checkout"
        checkout.mkdir()
        runtime_environment = tmp_path / "runtime"
        runtime_environment.mkdir()
        checkout_checks: list[tuple[Path, str]] = []
        module = "hephaestus.automation.pipeline.worker_pool"

        def checkout_matches_immutable_head(checkout_path: Path, head_sha: str) -> None:
            checkout_checks.append((checkout_path, head_sha))
            return None

        def quota_backed_scratch(root: Path) -> object:
            scratch = root / "scratch"
            scratch.mkdir()
            return nullcontext(scratch)

        def quota_backed_pi_smoke_logs(root: Path, source: Path) -> object:
            del root
            logs = source / "pi-smoke-logs"
            logs.mkdir()
            return nullcontext(logs)

        def prepare_immutable_git_metadata(
            _checkout: Path,
            _expected_head: str,
            _source: Path,
            root: Path,
            _git_executable: str,
        ) -> Path:
            metadata = root / "metadata.git"
            metadata.mkdir()
            return metadata

        def host_verification_command(
            *,
            argv: tuple[str, ...],
            source: Path,
            scratch: Path,
            runtime_environment: Path,
            git_metadata: Path,
            pi_smoke_logs: Path,
        ) -> tuple[str, ...]:
            del source, scratch, runtime_environment, git_metadata, pi_smoke_logs
            assert argv == (sys.executable, *spec.argv[1:])
            return (sys.executable, "-m", "pytest", *spec.argv[3:])

        job = BuildTestJob(
            repo="test/repo",
            cwd=checkout,
            argv=spec.argv,
            timeout_s=60,
            descr=spec.descr,
            expected_head_sha=expected_head,
            immutable_source=True,
        )
        shutdown = threading.Event()
        pool = WorkerPool(
            size=1,
            shutdown=shutdown,
            completion_q=completion_q,
            lock_dir=tmp_path / "locks",
        )
        try:
            with (
                patch(f"{module}.sys.platform", "darwin"),
                patch(
                    f"{module}._checkout_matches_immutable_head",
                    side_effect=checkout_matches_immutable_head,
                ),
                patch(f"{module}._trusted_uv_executable", return_value=sys.executable),
                patch(f"{module}._trusted_git_executable", return_value=sys.executable),
                patch(
                    f"{module}._verifier_owned_runtime_environment",
                    return_value=runtime_environment,
                ),
                patch(
                    f"{module}._bounded_git_archive",
                    return_value=(archive.getvalue(), ""),
                ),
                patch(
                    f"{module}._prepare_immutable_git_metadata",
                    side_effect=prepare_immutable_git_metadata,
                ),
                patch(f"{module}._quota_backed_scratch", side_effect=quota_backed_scratch),
                patch(
                    f"{module}._quota_backed_pi_smoke_logs",
                    side_effect=quota_backed_pi_smoke_logs,
                ),
                patch(
                    f"{module}._host_verification_command",
                    side_effect=host_verification_command,
                ),
            ):
                result = pool._run_build_test(job)
        finally:
            pool.shutdown()

        assert result.ok is True
        assert result.value == {
            "head_sha": expected_head,
            "immutable_source": True,
            "failure_kind": "none",
            "platform": "darwin",
            "status": "passed",
        }
        assert checkout_checks == [(checkout, expected_head), (checkout, expected_head)]
        assert "passed" in result.stdout_tail
        receipt = {
            "argv": list(spec.argv),
            "error": "",
            "failure_kind": "none",
            "head_sha": expected_head,
            "immutable_source": True,
            "ok": result.ok,
            "stderr_tail": result.stderr_tail,
            "stdout_tail": result.stdout_tail,
        }
        assert stage_module._host_verification_receipt_matches(receipt, spec, expected_head)
        assert not stage_module._host_verification_receipt_matches(
            {**receipt, "ok": False}, spec, expected_head
        )
        assert not stage_module._host_verification_receipt_matches(receipt, spec, "c" * 40)

        skipped = {
            "argv": list(spec.argv),
            "error": "unsupported_host_verification_boundary",
            "failure_kind": "runner",
            "head_sha": expected_head,
            "immutable_source": False,
            "ok": False,
            "platform": "linux",
            "status": "skipped",
            "stderr_tail": "",
            "stdout_tail": "",
        }
        assert stage_module._host_verification_receipt_matches(skipped, spec, expected_head)
        assert not stage_module._host_verification_receipt_matches(
            {**skipped, "platform": "darwin"}, spec, expected_head
        )
        assert not stage_module._host_verification_receipt_matches(
            {**skipped, "status": "failed"}, spec, expected_head
        )
        assert not stage_module._host_verification_receipt_matches(
            {**skipped, "platform": ""}, spec, expected_head
        )

    def test_python_changes_run_complete_host_validation_before_primary_reviewer(
        self, tmp_path: Path, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A read-only Python review receives deterministic static receipts."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state=REVIEW_CHECKOUT_WAIT)
        item.worktree = _make_hephaestus_checkout(tmp_path)
        item.payload.update(
            {
                "review_checkout_expected_head": "a" * 40,
                "review_checkout_ready": True,
                "pr_diff": (
                    "diff --git a/hephaestus/automation/pipeline/worker_pool.py "
                    "b/hephaestus/automation/pipeline/worker_pool.py\n"
                ),
            }
        )

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, BuildTestJob)
        assert result.job.argv == ("uv", "run", "ruff", "check", "hephaestus/", "tests/")
        assert result.job.descr == "review_python_ruff_check"
        assert result.on_done_state == "HOST_VERIFICATION_WAIT"

    def test_non_hephaestus_repository_has_no_hephaestus_host_plan(self) -> None:
        specs = stage_module._host_verification_specs(
            "diff --git a/scripts/validate.py b/scripts/validate.py\n",
            profile=None,
        )

        assert specs == ()

    def test_non_hephaestus_checkout_reports_unsupported_host_verification(
        self, tmp_path: Path, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = PrReviewStage()
        item = make_work_item(issue=1, pr=1001, state=REVIEW_CHECKOUT_WAIT)
        item.worktree = str(tmp_path)
        item.payload.update(
            {
                "review_checkout_expected_head": "a" * 40,
                "review_checkout_ready": True,
                "pr_diff": "diff --git a/scripts/validate.py b/scripts/validate.py\n",
            }
        )

        result = stage.step(item, make_ctx())

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.descr == "review"
        assert json.loads(result.job.prompt_kwargs["host_verifications_json"]) == [
            {
                "head_sha": "a" * 40,
                "immutable_source": True,
                "reason": "repository_profile_unavailable",
                "status": "unsupported",
            }
        ]

    def test_python_validation_config_changes_run_host_plan(
        self, tmp_path: Path, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Dependency and tool configuration cannot bypass host validation."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state=REVIEW_CHECKOUT_WAIT)
        item.worktree = _make_hephaestus_checkout(tmp_path)
        item.payload.update(
            {
                "review_checkout_expected_head": "a" * 40,
                "review_checkout_ready": True,
                "pr_diff": "diff --git a/uv.lock b/uv.lock\n",
            }
        )

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, BuildTestJob)
        assert result.job.descr == "review_python_ruff_check"

    def test_migration_docs_change_runs_version_currency_host_verification(
        self, tmp_path: Path, make_ctx: Any, make_work_item: Any
    ) -> None:
        """MIGRATION.md release claims receive an exact-head docs guard receipt."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state=REVIEW_CHECKOUT_WAIT)
        item.worktree = _make_hephaestus_checkout(tmp_path)
        item.payload.update(
            {
                "review_checkout_expected_head": "a" * 40,
                "review_checkout_ready": True,
                "pr_diff": (
                    "diff --git a/docs/MIGRATION.md b/docs/MIGRATION.md\n"
                    "--- a/docs/MIGRATION.md\n"
                    "+++ b/docs/MIGRATION.md\n"
                    "@@ -1,4 +1,4 @@\n"
                    "-old release text\n"
                    "+new release text\n"
                ),
            }
        )

        request = stage.step(item, ctx)

        expected_argv = (
            "uv",
            "run",
            "pytest",
            "-o",
            "addopts=",
            "tests/unit/docs/test_version_currency.py",
            "-q",
            "--tb=short",
        )
        assert isinstance(request, JobRequest)
        assert isinstance(request.job, BuildTestJob)
        assert request.job.argv == expected_argv
        assert request.job.descr == "review_migration_version_currency"
        assert request.job.expected_head_sha == "a" * 40
        assert request.job.immutable_source is True

        stage.on_job_done(
            item,
            JobResult(
                ok=True,
                value={
                    "head_sha": "a" * 40,
                    "immutable_source": True,
                    "failure_kind": "none",
                },
                stdout_tail="1 passed in 0.1s",
            ),
            ctx,
        )
        item.state = request.on_done_state
        review = stage.step(item, ctx)

        receipt = item.payload["host_verification_receipts"][0]
        assert receipt["argv"] == list(expected_argv)
        assert receipt["head_sha"] == "a" * 40
        assert receipt["ok"] is True
        assert receipt["immutable_source"] is True
        assert isinstance(review, JobRequest)
        assert isinstance(review.job, AgentJob)
        assert json.loads(review.job.prompt_kwargs["host_verifications_json"]) == [receipt]

    def test_integration_changes_do_not_add_a_nonhermetic_host_suite(
        self, tmp_path: Path, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Integration suites remain with CI rather than the no-network reviewer."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state=REVIEW_CHECKOUT_WAIT)
        item.worktree = _make_hephaestus_checkout(tmp_path)
        item.payload.update(
            {
                "review_checkout_expected_head": "a" * 40,
                "review_checkout_ready": True,
                "pr_diff": (
                    "diff --git a/tests/integration/test_flow.py b/tests/integration/test_flow.py\n"
                ),
            }
        )
        request = stage.step(item, ctx)
        for _ in range(3):
            assert isinstance(request, JobRequest)
            item.state = request.on_done_state
            stage.on_job_done(
                item,
                JobResult(
                    ok=True,
                    value={
                        "head_sha": "a" * 40,
                        "immutable_source": True,
                        "failure_kind": "none",
                    },
                ),
                ctx,
            )
            request = stage.step(item, ctx)

        assert isinstance(request, JobRequest)
        assert isinstance(request.job, AgentJob)
        assert request.job.descr == "review"

    def test_actionable_host_failure_hands_remediation_to_implementation(
        self, tmp_path: Path, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A failed host check never turns the reviewer into a writer."""
        secret = "sk" + "-live_12345678901234567890"
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state=REVIEW_CHECKOUT_WAIT)
        item.worktree = _make_hephaestus_checkout(tmp_path)
        item.payload.update(
            {
                "existing_pr": True,
                "review_checkout_expected_head": "a" * 40,
                "review_checkout_ready": True,
                "pr_diff": "diff --git a/hephaestus/example.py b/hephaestus/example.py\n",
            }
        )
        request = stage.step(item, ctx)
        assert isinstance(request, JobRequest)
        item.state = request.on_done_state
        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                error=f"rc=1 token={secret}",
                stdout_tail="Found 1 error.",
                value={
                    "head_sha": "a" * 40,
                    "immutable_source": True,
                    "failure_kind": "validation",
                },
            ),
            ctx,
        )

        assert stage.step(item, ctx) == Continue(next_state="ADDRESS_WAIT")
        item.state = "ADDRESS_WAIT"
        result = stage.step(item, ctx)
        assert result == StageOutcome(Disposition.FAIL_BACK, "implementation_remediation")
        failure = item.payload["host_verification_failure"]
        assert failure["argv"] == ["uv", "run", "ruff", "check", "hephaestus/", "tests/"]
        assert failure["stdout_tail"] == "Found 1 error."
        assert secret not in failure["error"]
        assert "<redacted>" in failure["error"]

    def test_failed_host_verification_fails_closed_before_primary_reviewer(
        self, tmp_path: Path, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A failed fixed command never reaches a reviewer or GO path."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state=REVIEW_CHECKOUT_WAIT)
        item.worktree = _make_hephaestus_checkout(tmp_path)
        item.payload.update(
            {
                "review_checkout_expected_head": "a" * 40,
                "review_checkout_ready": True,
                "pr_diff": (
                    "diff --git a/tests/performance/test_worker_pool_load.py "
                    "b/tests/performance/test_worker_pool_load.py\n"
                ),
            }
        )

        request = stage.step(item, ctx)
        assert isinstance(request, JobRequest)
        assert isinstance(request.job, BuildTestJob)
        for _ in range(3):
            item.state = request.on_done_state
            stage.on_job_done(
                item,
                JobResult(
                    ok=True,
                    value={
                        "head_sha": "a" * 40,
                        "immutable_source": True,
                        "failure_kind": "none",
                    },
                ),
                ctx,
            )
            request = stage.step(item, ctx)
            assert isinstance(request, JobRequest)
            assert isinstance(request.job, BuildTestJob)
        assert request.job.descr == "review_stalled_consumer_verification"
        assert isinstance(request.job, BuildTestJob)
        item.state = request.on_done_state
        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                error="rc=1",
                value={
                    "head_sha": "a" * 40,
                    "immutable_source": True,
                    "failure_kind": "test",
                },
                stderr_tail="1 failed in 0.44s",
            ),
            ctx,
        )

        assert stage.step(item, ctx) == Continue(next_state="ADDRESS_WAIT")
        assert ("mark_pr_implementation_no_go", (1001,)) in ctx.github.mutation_log
        assert item.payload["host_verification_failure"] == {
            "argv": list(request.job.argv),
            "path": "tests/performance/test_worker_pool_load.py",
            "head_sha": "a" * 40,
            "failure_kind": "test",
            "error": "rc=1",
            "stdout_tail": "",
            "stderr_tail": "1 failed in 0.44s",
        }
        comments = [
            comment.body if isinstance(comment, IssueComment) else comment
            for comment in ctx.github.comments[1001]
        ]
        assert len(comments) == 1
        comment = comments[0]
        assert comment.startswith("<!-- hephaestus-host-verification-failure:")
        assert "a" * 40 in comment
        assert "tests/performance/test_worker_pool_load.py" in comment
        assert "uv run pytest" in comment
        assert "**Failure classification**\n\n    test" in comment
        assert "1 failed in 0.44s" in comment

        assert stage.step(item, ctx) == Continue(next_state="ADDRESS_WAIT")
        repeated_comments = [
            entry.body if isinstance(entry, IssueComment) else entry
            for entry in ctx.github.comments[1001]
        ]
        assert repeated_comments == comments
        assert "review_audit_failure" not in item.payload

    def test_timed_out_host_verification_writes_no_go_and_routes_to_address(
        self, tmp_path: Path, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A verified test timeout is actionable, but never enters EVAL retry."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state=REVIEW_CHECKOUT_WAIT)
        item.worktree = _make_hephaestus_checkout(tmp_path)
        item.payload.update(
            {
                "review_checkout_expected_head": "a" * 40,
                "review_checkout_ready": True,
                "pr_diff": (
                    "diff --git a/tests/performance/test_worker_pool_load.py "
                    "b/tests/performance/test_worker_pool_load.py\n"
                ),
            }
        )
        request = stage.step(item, ctx)
        assert isinstance(request, JobRequest)
        for _ in range(3):
            item.state = request.on_done_state
            stage.on_job_done(
                item,
                JobResult(
                    ok=True,
                    value={
                        "head_sha": "a" * 40,
                        "immutable_source": True,
                        "failure_kind": "none",
                    },
                ),
                ctx,
            )
            request = stage.step(item, ctx)
            assert isinstance(request, JobRequest)
        assert isinstance(request.job, BuildTestJob)
        assert request.job.descr == "review_stalled_consumer_verification"
        item.state = request.on_done_state
        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                error="timeout",
                value={
                    "head_sha": "a" * 40,
                    "immutable_source": True,
                    "failure_kind": "test",
                },
            ),
            ctx,
        )

        assert stage.step(item, ctx) == Continue(next_state="ADDRESS_WAIT")
        assert ("mark_pr_implementation_no_go", (1001,)) in ctx.github.mutation_log
        assert item.payload["host_verification_failure"]["error"] == "timeout"
        assert "review_audit_failure" not in item.payload

    def test_unsupported_host_boundary_is_explicitly_skipped(
        self, tmp_path: Path, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Only an attested unsupported platform may skip review checks."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state=REVIEW_CHECKOUT_WAIT)
        item.worktree = _make_hephaestus_checkout(tmp_path)
        item.payload.update(
            {
                "review_checkout_expected_head": "a" * 40,
                "review_checkout_ready": True,
                "pr_diff": (
                    "diff --git a/tests/performance/test_worker_pool_load.py "
                    "b/tests/performance/test_worker_pool_load.py\n"
                ),
            }
        )
        request = stage.step(item, ctx)
        assert isinstance(request, JobRequest)
        item.state = request.on_done_state
        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                error="unsupported_host_verification_boundary",
                value={
                    "failure_kind": "runner",
                    "head_sha": "a" * 40,
                    "immutable_source": False,
                    "platform": "linux",
                    "status": "skipped",
                },
            ),
            ctx,
        )

        next_request = stage.step(item, ctx)

        assert isinstance(next_request, JobRequest)
        assert next_request.on_done_state == HOST_VERIFICATION_WAIT
        receipt = item.payload["host_verification_receipts"][0]
        assert "bypassed" not in receipt
        assert receipt["error"] == "unsupported_host_verification_boundary"
        assert receipt["platform"] == "linux"
        assert receipt["status"] == "skipped"
        assert ("mark_pr_implementation_no_go", (1001,)) not in ctx.github.mutation_log

    def test_unsupported_host_skip_with_mismatched_head_fails_closed(
        self, tmp_path: Path, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A worker cannot normalize a forged skip onto the reviewed head."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state=REVIEW_CHECKOUT_WAIT)
        item.worktree = _make_hephaestus_checkout(tmp_path)
        item.payload.update(
            {
                "review_checkout_expected_head": "a" * 40,
                "review_checkout_ready": True,
                "pr_diff": (
                    "diff --git a/tests/performance/test_worker_pool_load.py "
                    "b/tests/performance/test_worker_pool_load.py\n"
                ),
            }
        )
        request = stage.step(item, ctx)
        assert isinstance(request, JobRequest)
        item.state = request.on_done_state
        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                error="unsupported_host_verification_boundary",
                value={
                    "failure_kind": "runner",
                    "head_sha": "b" * 40,
                    "immutable_source": False,
                    "platform": "linux",
                    "status": "skipped",
                },
            ),
            ctx,
        )

        stage.step(item, ctx)

        receipt = item.payload["host_verification_receipts"][0]
        assert receipt["status"] == "failed"
        assert ("mark_pr_implementation_no_go", (1001,)) in ctx.github.mutation_log

    def test_host_failure_comment_error_preserves_no_go_and_fails_closed(
        self, tmp_path: Path, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A missing diagnostic comment cannot silently enter remediation."""

        class CommentFailureGitHub(FakeStageGitHub):
            def upsert_issue_comment(self, *_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("comment unavailable")

        github = CommentFailureGitHub()
        stage = PrReviewStage()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state=REVIEW_CHECKOUT_WAIT)
        item.worktree = _make_hephaestus_checkout(tmp_path)
        item.payload.update(
            {
                "review_checkout_expected_head": "a" * 40,
                "review_checkout_ready": True,
                "pr_diff": "diff --git a/hephaestus/example.py b/hephaestus/example.py\n",
            }
        )
        request = stage.step(item, ctx)
        assert isinstance(request, JobRequest)
        item.state = request.on_done_state
        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                error="rc=1",
                stdout_tail="failure",
                value={
                    "head_sha": "a" * 40,
                    "immutable_source": True,
                    "failure_kind": "validation",
                },
            ),
            ctx,
        )

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.FINISH_FAIL, "host_verification_comment_failed"
        )
        assert ("mark_pr_implementation_no_go", (1001,)) in github.mutation_log

    def test_forged_diff_content_cannot_trigger_host_verification(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Only a real diff header may activate a registered host command."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state=REVIEW_CHECKOUT_WAIT)
        item.payload.update(
            {
                "review_checkout_expected_head": "a" * 40,
                "review_checkout_ready": True,
                "pr_diff": "+++ b/tests/performance/test_worker_pool_load.py\n",
            }
        )

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.descr == "review"

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

    def test_post_posts_threads_durably_and_routes_to_implementation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """POST durably posts surviving threads, then hands them to implementation."""
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

        result = _complete_github_job(stage, item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "ADDRESS_WAIT"
        assert github.mutation_log == [("gh_pr_review_post", (1001, "COMMENT"))]
        assert item.payload["posted_thread_ids"] == ["thread-1001-0", "thread-1001-1"]
        assert item.payload["unresolved_threads_before_address"] == 2

    def test_post_with_zero_open_automation_threads_is_a_publication_no_op(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A zero-finding review emits no unanchored review-level response."""
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
        item.payload["reviewed_pr_head_sha"] = "a" * 40

        result = _complete_github_job(stage, item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "EVAL"
        assert github.mutation_log == []
        assert 1001 not in github.reviews

    @pytest.mark.parametrize("existing_pr", [False, True], ids=["fresh-pr", "existing-pr"])
    def test_empty_audit_hands_pre_existing_live_blocking_thread_to_implementation(
        self,
        make_ctx: Any,
        make_work_item: Any,
        existing_pr: bool,
    ) -> None:
        """Every open thread is handed to implementation without a review-stage write job."""
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
        item.payload["reviewed_pr_head_sha"] = "a" * 40

        post_result = _complete_github_job(stage, item, ctx)

        assert post_result == Continue(next_state="ADDRESS_WAIT")
        assert 1001 not in github.reviews
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

        assert address_result == StageOutcome(Disposition.FAIL_BACK, "implementation_remediation")
        assert item.payload["implementation_remediation"] is True

    def test_address_fresh_pr_hands_off_to_implementation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Fresh PR feedback is not implemented from the review stage."""
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

        assert result == StageOutcome(Disposition.FAIL_BACK, "implementation_remediation")
        assert item.payload["implementation_remediation"] is True

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
        assert _complete_github_job(stage, item, ctx) == Continue(next_state="ADDRESS_WAIT")
        item.state = "ADDRESS_WAIT"
        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FAIL_BACK, "implementation_remediation")
        assert item.payload["implementation_remediation"] is True

    def test_address_hands_review_threads_to_implementation_without_a_write_job(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The review stage labels no-go and hands remediation to the writer stage."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="ADDRESS_WAIT")
        item.worktree = "/tmp/wt"
        item.payload["existing_pr"] = True
        item.payload["remediation_threads"] = [
            {"thread_id": "t1", "path": "x.py", "line": 1, "body": "fix"}
        ]
        item.payload["reviewed_pr_head_sha"] = "a" * 40

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FAIL_BACK, "implementation_remediation")
        assert item.payload["implementation_remediation"] is True
        assert ("mark_pr_implementation_no_go", (1001,)) in ctx.github.mutation_log

    @pytest.mark.parametrize("state", ["PUSH_WAIT", "ADDRESS_WAIT"])
    def test_legacy_review_writer_states_fail_back_to_implementation(
        self, state: str, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Interrupted legacy writer states cannot mutate from pr_review."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state=state)
        item.worktree = "/tmp/review-checkout"
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "remediation_threads": [
                    {"thread_id": "thread-1", "path": "a.py", "line": 1, "body": "fix"}
                ],
            }
        )

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FAIL_BACK, "implementation_remediation")
        assert item.payload["implementation_remediation"] is True

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
            def load_legacy_review_thread_receipts(self, pr_number: int) -> list[dict[str, Any]]:
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


class TestReviewThreadLifecycle:
    """Open review threads remain actionable through the two-role lifecycle."""

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

    def test_reviewer_decisions_are_limited_to_host_replied_threads(self) -> None:
        """Only a fresh host reply receipt may become a resolution candidate."""
        replied = {
            **self._thread("thread-1", 3, "fix this"),
            "implementation_reply_id": "reply-1",
            "implementation_reply_body": (
                "Added the missing guard and regression test.\n\n"
                "<!-- hephaestus-implementation-reply:fixture -->"
            ),
            "implementation_head_sha": "a" * 40,
        }
        replied["comments"].append(
            {
                "id": "reply-1",
                "author": "hephaestus[bot]",
                "body": replied["implementation_reply_body"],
                "review_id": "review-thread-1",
            }
        )
        inherited = self._thread("thread-2", 8, "manual review")

        snapshots = _validation_thread_snapshots([replied, inherited], [replied])

        assert snapshots is not None
        assert snapshots[0]["implementation_reply_submitted"] is True
        assert snapshots[1]["implementation_reply_submitted"] is False
        assert _reviewer_thread_decisions(
            [replied],
            {"resolved": ["thread-1"], "unaddressed": []},
        ) == ({"thread-1"}, {})

    def test_reviewer_decisions_require_an_explicit_exhaustive_partition(self) -> None:
        """A missing decision or legacy dismissal bucket cannot resolve a thread."""
        receipt = {"id": "thread-1"}

        assert _reviewer_thread_decisions([receipt], {"resolved": [], "unaddressed": []}) is None
        assert _reviewer_thread_decisions([receipt], {"unaddressed": [], "wont_fix": []}) is None

    def test_implementation_replies_require_an_exact_all_thread_mapping(self) -> None:
        """An address agent cannot complete a pass by silently omitting a thread."""
        threads = [self._thread("thread-1", 3, "first"), self._thread("thread-2", 4, "second")]

        assert (
            _address_replies(
                {
                    "addressed": ["thread-1"],
                    "replies": {"thread-1": "Fixed the first concern."},
                },
                threads,
            )
            is None
        )
        assert _address_replies(
            {
                "addressed": ["thread-1", "thread-2"],
                "replies": {
                    "thread-1": "Fixed the first concern.",
                    "thread-2": "Fixed the second concern.",
                },
            },
            threads,
        ) == {
            "thread-1": "Fixed the first concern.",
            "thread-2": "Fixed the second concern.",
        }

    @pytest.mark.parametrize(
        ("result", "threads"),
        [
            (None, []),
            ({"addressed": "thread-1", "replies": {}}, []),
            ({"addressed": [], "replies": {}}, ["not-a-thread"]),
            (
                {"addressed": ["thread-1"], "replies": {"thread-1": "fixed"}},
                [{"id": ""}],
            ),
            (
                {"addressed": ["thread-1"], "replies": {"thread-1": "fixed"}},
                [{"id": "thread-1"}, {"thread_id": "thread-1"}],
            ),
            (
                {"addressed": [None], "replies": {"thread-1": "fixed"}},
                [{"id": "thread-1"}],
            ),
            (
                {
                    "addressed": ["thread-1", "thread-1"],
                    "replies": {"thread-1": "fixed"},
                },
                [{"id": "thread-1"}],
            ),
            (
                {"addressed": ["thread-1"], "replies": {"thread-1": "   "}},
                [{"id": "thread-1"}],
            ),
        ],
    )
    def test_implementation_reply_mapping_fails_closed_for_invalid_input(
        self, result: object, threads: list[object]
    ) -> None:
        """Malformed implementation handoffs cannot skip or forge a reply."""
        assert _address_replies(result, threads) is None  # type: ignore[arg-type]

    def test_implementation_reply_handoff_is_immutable_and_well_formed(self) -> None:
        """The retry journal preserves only a valid, copied host snapshot."""
        thread = self._thread("thread-1", 3, "first")
        handoff = _implementation_reply_handoff(
            "a" * 40,
            [thread],
            {"thread-1": "  Fixed the first concern.  "},
            "b" * 32,
        )

        assert handoff == {
            "head_sha": "a" * 40,
            "threads": [thread],
            "replies": {"thread-1": "Fixed the first concern."},
            "batch_nonce": "b" * 32,
        }
        assert (
            _implementation_reply_handoff(
                "c" * 64,
                [self._thread("thread-2", 4, "second")],
                {"thread-2": "Fixed the second concern."},
                "d" * 32,
            )
            is not None
        )
        thread["body"] = "mutated after the handoff"
        assert handoff["threads"][0]["body"] != thread["body"]
        assert (
            _implementation_reply_handoff(
                "not-a-sha", [self._thread("thread-1", 3, "x")], {}, "b" * 32
            )
            is None
        )
        assert _implementation_reply_handoff("a" * 40, ["not-a-thread"], {}, "b" * 32) is None
        assert (
            _implementation_reply_handoff("a" * 40, [{"id": ""}], {"": "fixed"}, "b" * 32) is None
        )

    def test_implementation_reply_handoff_journal_requires_actor_and_exact_source_comments(
        self,
    ) -> None:
        """Recovery binds to our immutable source comments, not mutable anchors."""
        thread = self._thread("thread-1", 3, "first")
        handoff = _implementation_reply_handoff(
            "a" * 40,
            [thread],
            {"thread-1": "Fixed the first concern."},
            "b" * 32,
        )
        assert handoff is not None
        entry = implementation_reply_handoff_journal_entry(1001, handoff)
        assert entry is not None
        _marker, body = entry
        assert body.splitlines()[1].startswith("<!-- ")

        assert (
            journaled_implementation_reply_handoff(
                [IssueComment(body=body, viewer_did_author=False)],
                pr_number=1001,
                threads=[thread],
            )
            is None
        )
        moved_thread = {
            **thread,
            "path": "renamed.py",
            "line": 999,
            "body": "a refreshed derived summary",
            "pr_state": {
                "state": "OPEN",
                "headRefOid": "c" * 40,
                "autoMergeRequest": None,
            },
        }
        recovered = journaled_implementation_reply_handoff(
            [IssueComment(body=body, viewer_did_author=True)],
            pr_number=1001,
            threads=[moved_thread],
        )
        assert recovered is not None
        assert recovered["replies"] == handoff["replies"]
        assert (
            journaled_implementation_reply_handoff(
                [IssueComment(body=body, viewer_did_author=True)],
                pr_number=1001,
                threads=[
                    {
                        **moved_thread,
                        "comments": [
                            {
                                **moved_thread["comments"][0],
                                "body": "an externally changed source comment",
                            }
                        ],
                    }
                ],
            )
            is None
        )

    def test_validation_receipts_require_one_complete_immutable_thread_snapshot(self) -> None:
        """Validation binds a thread decision to its exact host-read reply."""
        reply = "Fixed the first concern."
        thread = self._thread("thread-1", 3, "first")
        receipt = {
            **thread,
            "comments": [
                *thread["comments"],
                {"id": "reply-1", "body": reply, "author": "hephaestus[bot]"},
            ],
            "implementation_reply_id": "reply-1",
            "implementation_reply_body": reply,
            "implementation_head_sha": "a" * 40,
        }

        fingerprints = _validation_receipt_fingerprints([receipt])
        assert fingerprints is not None
        assert set(fingerprints) == {"thread-1"}
        assert len(fingerprints["thread-1"]) == 64
        assert _validation_receipt_fingerprints([receipt, receipt]) is None
        assert _validation_receipt_fingerprints([{**receipt, "comments": []}]) is None
        assert (
            _validation_receipt_fingerprints(
                [{**receipt, "comments": [{"id": "reply-1", "body": 7}]}]
            )
            is None
        )
        assert (
            _validation_receipt_fingerprints(
                [{**receipt, "comments": [*receipt["comments"], dict(receipt["comments"][-1])]}]
            )
            is None
        )

    def test_validation_snapshot_and_head_guard_fail_closed_on_drift(self) -> None:
        """Only an open, unarmed exact head and matching receipt enter validation."""
        receipt = {
            "id": "thread-1",
            "implementation_reply_body": "fixed",
            "implementation_reply_id": "reply-1",
        }
        assert _validation_thread_snapshots([{"id": "thread-1"}], [receipt]) == [
            {
                "id": "thread-1",
                "implementation_reply_body": "fixed",
                "implementation_reply_submitted": True,
            }
        ]
        assert _validation_thread_snapshots([{"id": "thread-1"}], [receipt, receipt]) is None
        assert _validation_thread_snapshots([{"id": "thread-1"}, {"id": "thread-1"}], []) is None
        assert _validation_thread_snapshots([{"id": "thread-1"}], [{"id": "thread-2"}]) is None
        state = {"state": "OPEN", "headRefOid": "a" * 40, "autoMergeRequest": None}
        assert _pr_is_current_open_head(state, "a" * 40)
        assert not _pr_is_current_open_head({**state, "autoMergeRequest": {}}, "a" * 40)
        assert not _pr_is_current_open_head({**state, "headRefOid": "b" * 40}, "a" * 40)

    def test_validation_parser_uses_only_a_complete_final_json_verdict(self) -> None:
        """Prose and malformed JSON cannot become reviewer authorization."""
        assert _parse_validation_result(None) is None
        assert _parse_validation_result("not a verdict") is None
        assert _parse_validation_result("```json\n[]\n```") is None
        assert _parse_validation_result(
            'earlier ```json\n{"resolved": ["old"], "unaddressed": []}\n```\n'
            'final ```json\n{"resolved": ["thread-1"], "unaddressed": []}\n```'
        ) == {"resolved": ["thread-1"], "unaddressed": []}
        assert (
            _parse_validation_result(
                'earlier ```json\n{"resolved": ["old"], "unaddressed": []}\n```\n'
                "final ```json\n{not valid json}\n```"
            )
            is None
        )
        assert (
            _parse_validation_result(
                '```json\n{"resolved": ["thread-1"], "unaddressed": []}\n```\n'
                "untrusted trailing text"
            )
            is None
        )

    @pytest.mark.parametrize(
        ("receipts", "verdict"),
        [
            ([{"id": "thread-1"}], None),
            ([{"id": "thread-1"}], {"resolved": "thread-1", "unaddressed": []}),
            ([{}], {"resolved": [], "unaddressed": []}),
            (
                [{"id": "thread-1"}, {"id": "thread-1"}],
                {"resolved": [], "unaddressed": []},
            ),
            ([{"id": "thread-1"}], {"resolved": [7], "unaddressed": []}),
            (
                [{"id": "thread-1"}],
                {"resolved": ["thread-1", "thread-1"], "unaddressed": []},
            ),
            ([{"id": "thread-1"}], {"resolved": [], "unaddressed": ["not-an-object"]}),
            (
                [{"id": "thread-1"}],
                {"resolved": [], "unaddressed": [{"thread_id": "thread-1", "detail": ""}]},
            ),
        ],
    )
    def test_reviewer_decisions_fail_closed_for_malformed_or_ambiguous_verdicts(
        self, receipts: list[dict[str, object]], verdict: object
    ) -> None:
        """Every receipt must have one well-formed, non-overlapping decision."""
        assert _reviewer_thread_decisions(receipts, verdict) is None

    def test_postable_audit_finding_requires_a_real_inline_location_and_body(self) -> None:
        """Only structurally valid new audit findings can be published."""
        valid = {
            "path": "hephaestus/example.py",
            "line": 3,
            "side": "RIGHT",
            "severity": "major",
            "body": "Handle the null value before reading it.",
        }
        assert _is_postable_finding(valid)
        assert not _is_postable_finding({**valid, "path": ""})
        assert not _is_postable_finding({**valid, "line": True})
        assert not _is_postable_finding({**valid, "side": "LEFT"})
        assert not _is_postable_finding({**valid, "severity": "informational"})
        assert not _is_postable_finding({**valid, "body": "<!-- hephaestus-severity: major -->"})

    def test_visible_reviewer_prefix_does_not_repost_a_live_finding(self) -> None:
        """The human-readable role prefix is not part of finding identity."""
        finding = {
            "path": "hephaestus/example.py",
            "line": 3,
            "side": "RIGHT",
            "body": "Handle the null value before reading it.",
        }
        live = {
            "thread-1": {
                **finding,
                "body": (
                    "[Review] Handle the null value before reading it.\n"
                    "<!-- hephaestus-severity: major -->"
                ),
            }
        }

        assert _without_duplicate_live_findings([finding], live) == []

    def test_reviewer_feedback_is_preserved_for_the_next_implementer(self) -> None:
        """A rejection reply is present in the next remediation prompt payload."""
        thread = self._thread("thread-1", 3, "fix this")
        thread["comments"].append(
            {
                "id": "reviewer-follow-up",
                "author": "reviewer",
                "body": "Reviewer validation found this still unresolved: guard None first.",
            }
        )

        normalized = _normalize_remediation_threads([thread])

        assert len(normalized) == 1
        assert "guard None first" in normalized[0]["body"]
        assert "Thread conversation:" in normalized[0]["body"]

    def test_remediation_conversation_does_not_duplicate_scope_manifest(self) -> None:
        """A submitted reply keeps one parseable scope-retraction manifest."""
        thread = self._thread(
            "thread-1",
            3,
            (
                "Remove this unrelated file.\n"
                '<!-- hephaestus-scope-retraction-paths: ["out-of-scope.py"] -->'
            ),
        )
        thread["path"] = "out-of-scope.py"
        thread["comments"].append(
            {
                "id": "implementation-reply",
                "author": "implementer",
                "body": "[Response] Removed the unrelated file.",
            }
        )

        normalized = _normalize_remediation_threads([thread])

        assert _scope_retraction_paths(normalized) == ("out-of-scope.py",)
        assert normalized[0]["body"].count("hephaestus-scope-retraction-paths:") == 1

    def test_partial_reconciliation_restarts_fresh_review_without_stale_receipts(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A partial race discards the validator decision for a fresh review."""
        first = self._thread("live-thread-1001-0", 3, "first")
        second = self._thread("live-thread-1001-1", 4, "second")
        receipts = [
            {
                **thread,
                "implementation_reply_id": f"reply-{thread['id']}",
                "implementation_reply_body": "fixed",
                "comments": [
                    *thread["comments"],
                    {
                        "id": f"implementation-reply-{thread['id']}",
                        "author": "hephaestus[bot]",
                        "body": "fixed",
                    },
                ],
            }
            for thread in (first, second)
        ]

        class PartialGitHub(FakeStageGitHub):
            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(thread) for thread in receipts]

            def reconcile_reviewer_validated_threads(self, *args: Any, **kwargs: Any) -> Any:
                del args, kwargs
                from hephaestus.automation.pipeline.stages.base import (
                    ReviewerThreadReconciliationResult,
                )

                return ReviewerThreadReconciliationResult(
                    resolved_thread_ids=("live-thread-1001-0",),
                    blocked_thread_ids=("live-thread-1001-1",),
                )

        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "validation_result": {
                    "resolved": ["live-thread-1001-0", "live-thread-1001-1"],
                    "unaddressed": [],
                },
                "review_audit": _valid_audit(),
                "review_threads": [],
            }
        )

        result = _complete_github_job(
            PrReviewStage(), item, make_ctx(github=PartialGitHub(unresolved=[(2, 0)]))
        )

        assert result == Continue(next_state="REVIEW_WAIT")
        assert item.payload.get("review_audit_failure") is not True

    def test_reconciliation_blocked_receipt_restarts_fresh_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An unproven resolution discards the validator decision for a fresh review."""
        thread = self._thread("live-thread-restore-failed", 3, "first")
        thread["comments"].append(
            {
                "id": "implementation-reply-live-thread-restore-failed",
                "author": "hephaestus[bot]",
                "body": "Fixed the first concern.",
            }
        )

        class BlockedReconciliationGitHub(FakeStageGitHub):
            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(thread)]

            def reconcile_reviewer_validated_threads(self, *args: Any, **kwargs: Any) -> Any:
                del args, kwargs
                from hephaestus.automation.pipeline.stages.base import (
                    ReviewerThreadReconciliationResult,
                )

                return ReviewerThreadReconciliationResult(
                    blocked_thread_ids=("live-thread-restore-failed",)
                )

        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "validation_result": {
                    "resolved": ["live-thread-restore-failed"],
                    "unaddressed": [],
                },
                "review_audit": _valid_audit(),
                "review_threads": [],
            }
        )

        assert _complete_github_job(
            PrReviewStage(),
            item,
            make_ctx(github=BlockedReconciliationGitHub(unresolved=[(2, 0)])),
        ) == Continue(next_state="REVIEW_WAIT")

    def test_unaddressed_external_bot_thread_routes_to_remediation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An exact bot finding is routed to the implementation/reviewer cycle."""
        bot = self._thread("bot-1", 3, "fix this")
        bot.update(
            {
                "external_bot": True,
                "author_type": "Bot",
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
                "validation_result": {"unaddressed": [{"thread_id": "bot-1"}], "wont_fix": []},
                "review_audit": ReviewAudit("A", "clean", (), "", valid=True),
                "review_threads": [],
            }
        )

        stage = PrReviewStage()
        assert _complete_github_job(stage, item, make_ctx(github=BotGitHub())) == Continue(
            next_state="ADDRESS_WAIT"
        )
        assert item.payload["remediation_threads"] == [
            {
                "thread_id": "bot-1",
                "path": "a.py",
                "line": 3,
                "body": "<!-- hephaestus-severity: major -->\nfix this",
            }
        ]

    def test_validation_reads_all_open_threads_after_restart(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A restarted reviewer reads the live thread rather than stale state."""
        live = self._thread("thread-1", 3, "fix this")

        class RestartGitHub(FakeStageGitHub):
            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(live)]

        item = make_work_item(issue=1, pr=1001, state="VALIDATE_WAIT")
        item.payload["reviewed_pr_head_sha"] = "a" * 40
        stage = PrReviewStage()
        result = stage.step(item, make_ctx(github=RestartGitHub()))

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert json.loads(result.job.prompt_kwargs["prior_comments_json"]) == [
            {
                **live,
                "implementation_reply_body": None,
                "implementation_reply_submitted": False,
            }
        ]
        item.state = "POST"
        item.payload.update(
            {
                "validation_result": {
                    "unaddressed": [{"thread_id": "thread-1"}],
                    "wont_fix": [],
                },
                "review_audit": ReviewAudit("A", "clean", (), "", valid=True),
                "review_threads": [],
            }
        )

        assert _complete_github_job(stage, item, make_ctx(github=RestartGitHub())) == Continue(
            next_state="ADDRESS_WAIT"
        )
        assert item.payload["remediation_threads"] == [
            {
                "thread_id": "thread-1",
                "path": "a.py",
                "line": 3,
                "body": "<!-- hephaestus-severity: major -->\nfix this",
            }
        ]

    def test_live_threads_are_remediated_without_duplicate_findings(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Every live thread enters remediation while matching findings are not reposted."""

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
                self,
                pr_number: int,
                threads: list[dict[str, Any]],
                *,
                expected_head_sha: str,
                review_diff: str | None = None,
            ) -> list[dict[str, Any]]:
                del expected_head_sha, review_diff
                self.posted_batches.append([dict(thread) for thread in threads])
                thread_ids: list[str] = []
                for thread in threads:
                    thread_id = f"new-{self.next_id}"
                    self.next_id += 1
                    thread_ids.append(thread_id)
                    self.live.append(
                        TestReviewThreadLifecycle._thread(
                            thread_id, int(thread["line"]), str(thread["body"])
                        )
                    )
                self._log("gh_pr_review_post", pr_number, "COMMENT")
                new_ids = set(thread_ids)
                return [dict(thread) for thread in self.live if str(thread.get("id")) in new_ids]

        inherited = [
            self._thread(f"inherited-{index}", index + 1, f"old {index}") for index in range(10)
        ]
        prior_threads = [
            self._thread(f"thread-{index}", index + 20, f"duplicate {index}") for index in range(12)
        ]
        github = LiveThreadGitHub(inherited + prior_threads)
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
                "validation_result": {
                    "unaddressed": [
                        {"thread_id": "thread-0"},
                        {"thread_id": "thread-1"},
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

        result = _complete_github_job(stage, item, ctx)

        assert result == Continue(next_state="ADDRESS_WAIT")
        assert len(github.posted_batches) == 1
        assert len(github.posted_batches[0]) == 7
        assert len(github.live) == 29

    def test_preexisting_thread_continues_to_remediation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An inherited thread is addressed instead of becoming a terminal handoff."""

        class ResolverForbiddenGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.live = [TestReviewThreadLifecycle._thread("thread-1", 3, "fix this")]

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(thread) for thread in self.live]

        github = ResolverForbiddenGitHub()
        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "validation_result": {"unaddressed": [], "wont_fix": []},
                "review_audit": ReviewAudit("A", "clean", (), "", valid=True),
                "review_threads": [],
            }
        )

        result = _complete_github_job(PrReviewStage(), item, make_ctx(github=github))

        assert result == Continue(next_state="ADDRESS_WAIT")
        assert not any(name == "mark_pr_implementation_go" for name, _ in github.mutation_log)

    def test_preexisting_thread_is_not_resolved_before_fresh_validation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Only a post-fix reviewer decision may resolve an inherited thread."""

        class ResolvingGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.live = [TestReviewThreadLifecycle._thread("thread-1", 3, "fix this")]
                self.calls: list[tuple[int, str, list[dict[str, Any]], dict[str, str]]] = []

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(thread) for thread in self.live]

        github = ResolvingGitHub()
        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "validation_result": {"unaddressed": [], "wont_fix": []},
                "review_audit": ReviewAudit("A", "clean", (), "", valid=True),
                "review_threads": [],
            }
        )

        result = _complete_github_job(PrReviewStage(), item, make_ctx(github=github))

        assert result == Continue(next_state="ADDRESS_WAIT")
        assert github.calls == []

    def test_minor_finding_is_audit_only_not_an_inline_merge_blocker(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Minor/nitpick findings are never published as unresolved inline threads."""

        class CapturePostsGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.posted_batches: list[list[dict[str, Any]]] = []

            def post_review_threads(
                self,
                pr_number: int,
                threads: list[dict[str, Any]],
                *,
                expected_head_sha: str,
                review_diff: str | None = None,
            ) -> list[dict[str, Any]]:
                self.posted_batches.append([dict(thread) for thread in threads])
                return super().post_review_threads(
                    pr_number,
                    threads,
                    expected_head_sha=expected_head_sha,
                    review_diff=review_diff,
                )

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

        result = _complete_github_job(PrReviewStage(), item, make_ctx(github=github))

        assert result == Continue(next_state="EVAL")
        assert github.posted_batches == []

    def test_validation_receives_all_live_thread_facts(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The reviewer validates every open host-read thread, not only reply receipts."""
        stage = PrReviewStage()
        item = make_work_item(issue=1, pr=1001, state="VALIDATE_WAIT")
        item.payload["reviewed_pr_head_sha"] = "a" * 40
        first_thread = self._thread("thread-1", 3, "fix this")
        inherited = self._thread("inherited", 8, "manual review")

        class ProcessOnlyGitHub(FakeStageGitHub):
            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(first_thread), dict(inherited)]

        ctx = make_ctx(github=ProcessOnlyGitHub())

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert json.loads(result.job.prompt_kwargs["prior_comments_json"]) == [
            {
                **first_thread,
                "implementation_reply_body": None,
                "implementation_reply_submitted": False,
            },
            {
                **inherited,
                "implementation_reply_body": None,
                "implementation_reply_submitted": False,
            },
        ]

    def test_externally_resolved_thread_is_not_recreated_from_validator_output(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A vanished thread stays closed; validation output never recreates it."""

        class ExternallyResolvedGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.live: list[dict[str, Any]] = []
                self.posted: list[dict[str, Any]] = []

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(thread) for thread in self.live]

            def post_review_threads(
                self,
                pr_number: int,
                threads: list[dict[str, Any]],
                *,
                expected_head_sha: str,
                review_diff: str | None = None,
            ) -> list[dict[str, Any]]:
                del expected_head_sha, review_diff
                self.posted = [dict(thread) for thread in threads]
                posted_ids = [f"reopened-{index}" for index, _ in enumerate(threads)]
                for thread_id, thread in zip(posted_ids, threads, strict=True):
                    self.live.append(
                        TestReviewThreadLifecycle._thread(
                            thread_id,
                            int(thread["line"]),
                            str(thread["body"]),
                        )
                    )
                self._log("gh_pr_review_post", pr_number, "COMMENT")
                return [dict(thread) for thread in self.live if thread["id"] in posted_ids]

        github = ExternallyResolvedGitHub()
        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "validation_result": {
                    "unaddressed": [
                        {
                            "thread_id": "thread-1",
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

        result = _complete_github_job(PrReviewStage(), item, make_ctx(github=github))

        assert result == Continue(next_state="EVAL")
        assert github.posted == []

    def test_changed_open_thread_is_sent_back_to_implementation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A changed thread remains open for a fresh implementation reply."""

        class ChangedReceiptGitHub(FakeStageGitHub):
            def __init__(self, live: list[dict[str, Any]]) -> None:
                super().__init__()
                self.live = live
                self.posted: list[dict[str, Any]] = []

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(thread) for thread in self.live]

            def post_review_threads(
                self,
                pr_number: int,
                threads: list[dict[str, Any]],
                *,
                expected_head_sha: str,
                review_diff: str | None = None,
            ) -> list[dict[str, Any]]:
                del expected_head_sha, review_diff
                self.posted = [dict(thread) for thread in threads]
                posted_ids = [f"reopened-{index}" for index, _ in enumerate(threads)]
                for thread_id, thread in zip(posted_ids, threads, strict=True):
                    self.live.append(
                        TestReviewThreadLifecycle._thread(
                            thread_id,
                            int(thread["line"]),
                            str(thread["body"]),
                        )
                    )
                self._log("gh_pr_review_post", pr_number, "COMMENT")
                return [dict(thread) for thread in self.live if thread["id"] in posted_ids]

        prior = self._thread("thread-1", 3, "fix this")
        changed = dict(prior)
        changed["comments"] = [
            {"author": "hephaestus[bot]", "body": "fix this"},
            {"author": "hephaestus[bot]", "body": "reviewer follow-up"},
        ]
        github = ChangedReceiptGitHub([changed])
        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "validation_result": {
                    "unaddressed": [
                        {
                            "thread_id": "thread-1",
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

        result = _complete_github_job(PrReviewStage(), item, make_ctx(github=github))

        assert result == Continue(next_state="ADDRESS_WAIT")
        assert github.posted == []
        assert item.payload["remediation_threads"][0]["thread_id"] == "thread-1"

    def test_replaced_validation_receipt_restarts_validation_without_reconciliation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A reviewer decision for reply A cannot act on a later reply B."""
        reply_a = self._thread("thread-1", 3, "fix this")
        reply_a["comments"].append(
            {
                "id": "implementation-reply-a",
                "author": "hephaestus[bot]",
                "body": "Fixed it with receipt A.",
            }
        )
        reply_b = {
            **reply_a,
            "comments": [
                *reply_a["comments"],
                {
                    "id": "implementation-reply-b",
                    "author": "hephaestus[bot]",
                    "body": "Fixed it differently with receipt B.",
                },
            ],
        }

        class ReceiptRaceGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.live = [reply_a]
                self.reconciliation_calls: list[dict[str, Any]] = []

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(thread) for thread in self.live]

            def reconcile_reviewer_validated_threads(self, *args: Any, **kwargs: Any) -> Any:
                self.reconciliation_calls.append(dict(kwargs))
                return super().reconcile_reviewer_validated_threads(*args, **kwargs)

        github = ReceiptRaceGitHub()
        ctx = make_ctx(github=github)
        stage = PrReviewStage()
        item = make_work_item(issue=1, pr=1001, state="VALIDATE_WAIT")
        item.payload["reviewed_pr_head_sha"] = "a" * 40

        validation = stage.step(item, ctx)
        assert isinstance(validation, JobRequest)
        assert item.payload["validation_threads"][-1]["comments"][-1]["id"] == (
            "implementation-reply-a"
        )

        github.live = [reply_b]
        item.state = "POST"
        item.payload.update(
            {
                "validation_result": {"resolved": ["thread-1"], "unaddressed": []},
                "review_audit": _valid_audit(),
                "review_threads": [],
            }
        )

        result = _complete_github_job(stage, item, ctx)

        assert result == Continue(next_state="VALIDATE_WAIT")
        assert github.reconciliation_calls == []

    def test_same_head_pr_metadata_mutation_restarts_validation_before_reconciliation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A validator decision for one title/body cannot act after metadata changes."""
        thread = self._thread("thread-1", 3, "fix this")
        thread["comments"].append(
            {
                "id": "implementation-reply",
                "author": "hephaestus[bot]",
                "body": "Updated the PR metadata only.",
            }
        )

        class MetadataRaceGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__(
                    pr_review_context={
                        "pr_title": "docs(policy): corrected",
                        "pr_description": "Current factual summary.\n\nCloses #1",
                        "pr_head_sha": "a" * 40,
                        "pr_base_branch": "main",
                    }
                )
                self.reconciliation_calls: list[dict[str, Any]] = []

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(thread)]

            def reconcile_reviewer_validated_threads(self, *args: Any, **kwargs: Any) -> Any:
                self.reconciliation_calls.append(dict(kwargs))
                return super().reconcile_reviewer_validated_threads(*args, **kwargs)

        github = MetadataRaceGitHub()
        ctx = make_ctx(github=github)
        stage = PrReviewStage()
        item = make_work_item(issue=1, pr=1001, state="VALIDATE_WAIT")
        item.payload["reviewed_pr_head_sha"] = "a" * 40

        validation = stage.step(item, ctx)
        assert isinstance(validation, JobRequest)
        original_fingerprint = item.payload["validation_pr_metadata_fingerprint"]

        github._pr_review_context = {
            "pr_title": "docs(policy): stale claim restored",
            "pr_description": "Stale factual summary.\n\nCloses #1",
            "pr_head_sha": "a" * 40,
            "pr_base_branch": "main",
        }
        item.state = "POST"
        item.payload.update(
            {
                "validation_result": {"resolved": ["thread-1"], "unaddressed": []},
                "review_audit": _valid_audit(),
                "review_threads": [],
            }
        )

        result = _complete_github_job(stage, item, ctx)

        assert result == Continue(next_state="VALIDATE_WAIT")
        assert github.reconciliation_calls == []
        assert item.payload.get("validation_pr_metadata_fingerprint") is None
        assert item.payload.get("validation_result") is None
        assert original_fingerprint

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
                id="multiple-participants",
            ),
        ],
    )
    def test_unproven_prior_state_never_resolves_a_thread(
        self,
        make_ctx: Any,
        make_work_item: Any,
        pr_state: dict[str, Any],
        validation_result: dict[str, Any],
        authors: list[str],
    ) -> None:
        """Only a current implementation reply may authorize reviewer resolution."""

        class ResolutionRecordingGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__(pr_state=pr_state)
                self.live = [
                    TestReviewThreadLifecycle._thread("thread-1", 3, "fix this", authors=authors)
                ]

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(thread) for thread in self.live]

            def post_review_threads(
                self,
                pr_number: int,
                threads: list[dict[str, Any]],
                *,
                expected_head_sha: str,
                review_diff: str | None = None,
            ) -> list[dict[str, Any]]:
                del expected_head_sha, review_diff
                self._log("gh_pr_review_post", pr_number, "COMMENT")
                return []

        github = ResolutionRecordingGitHub()
        ctx = make_ctx(github=github)
        stage = PrReviewStage()
        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "validation_result": validation_result,
                "review_audit": ReviewAudit("A", "clean", (), "", valid=True),
                "review_threads": [],
            }
        )

        result = _complete_github_job(stage, item, ctx)

        if pr_state["headRefOid"] != "a" * 40:
            assert result == Continue(next_state="ADDRESS_WAIT")
            assert not any("reconcile" in name for name, _ in github.mutation_log)

    def test_unaddressed_thread_routes_to_remediation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A verified prior finding is sent to the address leg, not handed off."""

        class GuardRaceGitHub(FakeStageGitHub):
            def __init__(self, live: dict[str, Any]) -> None:
                super().__init__()
                self.live = live

            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                return [dict(self.live)]

        review_thread = self._thread("thread-1", 3, "fix this")
        github = GuardRaceGitHub(review_thread)
        item = make_work_item(issue=1, pr=1001, state="POST")
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "validation_result": {
                    "unaddressed": [{"thread_id": "thread-1"}],
                    "wont_fix": [],
                },
                "review_audit": ReviewAudit("A", "clean", (), "", valid=True),
                "review_threads": [],
            }
        )

        result = _complete_github_job(PrReviewStage(), item, make_ctx(github=github))

        assert result == Continue(next_state="ADDRESS_WAIT")
        assert item.payload["remediation_threads"] == [
            {
                "thread_id": "thread-1",
                "path": "a.py",
                "line": 3,
                "body": "<!-- hephaestus-severity: major -->\nfix this",
            }
        ]
        assert ("mark_pr_implementation_no_go", (1001,)) not in github.mutation_log

    def test_truncated_live_thread_facts_skip_validation_and_all_writes(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A pagination/read failure cannot hide a later review participant."""

        class TruncatedThreadsGitHub(FakeStageGitHub):
            def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
                del pr_number
                raise RuntimeError("could not fetch all PR review threads")

        stage = PrReviewStage()
        github = TruncatedThreadsGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="VALIDATE_WAIT")

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

    def test_incomplete_scope_retraction_finishes_without_a_label_mutation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A locally rejected address commit never becomes an approval or push retry."""
        stage = PrReviewStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=2137, pr=2346, state="EVAL")
        item.payload["scope_retraction_failure"] = True

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "scope_retraction_incomplete")
        assert github.mutation_log == []

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

    def test_go_with_zero_threads_posts_a_public_audit_and_advances_to_merge_wait(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A clean GO has a human-readable, head-bound public audit record."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        ctx.config.enable_follow_up = False
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result == StageOutcome(Disposition.ADVANCE, "review audit; merge wait pending")
        assert github.mutation_log[0][0] == "persist_pending_implementation_go_audit"
        assert github.mutation_log[1] == ("mark_pr_implementation_go", (1001,))
        assert github.mutation_log[2][0] == "gh_issue_upsert_comment"
        assert github.mutation_log[3][0] == "publish_implementation_go_audit"
        public_comments = github.comments.get(1001, [])
        assert any(
            isinstance(comment, str)
            and comment.startswith(
                "<!-- hephaestus-implementation-go-audit:"
                "pr=1001:head=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->"
            )
            and "## Automated PR review" in comment
            and "Review summary: fixture audit" in comment
            for comment in public_comments
        )
        assert ("arm_auto_merge", (1001,)) not in github.mutation_log
        assert item.attempts["pr_review_iter"] == 1  # real verdict counted

    def test_go_removes_only_the_matching_recovery_handoff_after_public_audit(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A published audit makes its exact-head recovery receipt disposable."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()
        matching_handoff = (
            "<!-- hephaestus-implementation-reply-handoff:"
            "pr=1001:head=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:"
            "batch=0123456789abcdef0123456789abcdef -->\n"
            "<!-- {} -->"
        )
        other_handoff = (
            "<!-- hephaestus-implementation-reply-handoff:"
            "pr=1001:head=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:"
            "batch=fedcba9876543210fedcba9876543210 -->\n"
            "<!-- {} -->"
        )
        github.comments[1001] = [matching_handoff, other_handoff]

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.ADVANCE, "review audit; merge wait pending"
        )
        assert not any(
            isinstance(comment, str) and "head=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:" in comment
            for comment in github.comments[1001]
        )
        assert any(
            isinstance(comment, str) and "head=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:" in comment
            for comment in github.comments[1001]
        )

    def test_go_retries_when_the_public_audit_is_not_durable(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """GO remains retryable when its required public audit write fails."""

        class AuditWriteFailsGitHub(FakeStageGitHub):
            def publish_implementation_go_audit(
                self, pr_number: int, head_sha: str, audit: ReviewAudit
            ) -> None:
                del pr_number, head_sha, audit
                raise RuntimeError("comment unavailable")

        stage = PrReviewStage()
        github = AuditWriteFailsGitHub(unresolved=[(0, 0)])
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        assert stage.step(item, make_ctx(github=github)) == StageOutcome(
            Disposition.RETRY, "implementation_go_audit_retry"
        )
        assert [name for name, _args in github.mutation_log] == [
            "persist_pending_implementation_go_audit",
            "mark_pr_implementation_go",
        ]
        assert 1001 in github.pending_go_audits

    def test_go_audit_publication_retries_are_bounded_backed_off_and_do_not_rereview(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Repeated publication failures stay in the receipt-backed publish state."""

        class AuditWriteFailsGitHub(FakeStageGitHub):
            def publish_implementation_go_audit(
                self, pr_number: int, head_sha: str, audit: ReviewAudit
            ) -> None:
                del pr_number, head_sha, audit
                raise RuntimeError("comment unavailable")

        stage = PrReviewStage()
        github = AuditWriteFailsGitHub(unresolved=[(0, 0)])
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()
        ctx = make_ctx(github=github)

        for expected_delay in (1.0, 2.0, 4.0):
            assert stage.step(item, ctx) == StageOutcome(
                Disposition.RETRY, "implementation_go_audit_retry"
            )
            assert item.state == "GO_AUDIT_PUBLISH"
            assert item.payload["retry_delay_s"] == expected_delay

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.FINISH_FAIL, "implementation_go_audit_failed"
        )
        names = [name for name, _args in github.mutation_log]
        assert names.count("persist_pending_implementation_go_audit") == 1
        assert names.count("mark_pr_implementation_go") == 1
        assert item.attempts["pr_review_iter"] == 1
        assert 1001 in github.pending_go_audits

    def test_restart_resumes_pending_audit_without_reviewer_or_label_rewrite(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A reseeded exact-head receipt resumes directly at publication recovery."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 0)], pr_impl_state=(True, False))
        item = make_work_item(issue=1, pr=1001, state="ENTER")
        item.payload.update(
            {
                "pending_implementation_go_audit": _valid_audit(),
                "pending_implementation_go_audit_head": "a" * 40,
                "pending_implementation_go_label_confirmed": True,
            }
        )
        ctx = make_ctx(github=github)

        assert stage.step(item, ctx) == Continue(next_state="GO_AUDIT_RECEIPT")
        item.state = "GO_AUDIT_RECEIPT"
        assert stage.step(item, ctx) == StageOutcome(
            Disposition.ADVANCE, "review audit; merge wait pending"
        )
        names = [name for name, _args in github.mutation_log]
        assert "mark_pr_implementation_go" not in names
        assert "publish_implementation_go_audit" in names
        assert item.attempts["pr_review_iter"] == 0

    def test_thread_added_during_go_write_preserves_external_labels_and_restarts_review(
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

        assert result == StageOutcome(Disposition.FINISH_FAIL, "review_activity_changed")
        assert ("mark_pr_implementation_go", (1001,)) in github.mutation_log
        assert not any(
            name in {"mark_pr_implementation_no_go", "gh_issue_remove_labels"}
            for name, _args in github.mutation_log
        )
        assert github.pr_has_implementation_state_label(1001) == (False, True)
        assert github.comments.get(1001, []) == []
        assert not any(name == "gh_issue_comment" for name, _args in github.mutation_log)

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
        assert not any(name == "arm_auto_merge" for name, _args in github.mutation_log)

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
        assert ("mark_pr_implementation_go", (1001,)) in github.mutation_log
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
        assert ("mark_pr_implementation_go", (1001,)) in github.mutation_log

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
        assert ("mark_pr_implementation_go", (1001,)) in github.mutation_log

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

    def test_go_rechecks_preexisting_threads_and_reenters_remediation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A late thread is requeued for the implementation/reviewer cycle."""
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(0, 0, 0), (0, 0, 1)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert result == Continue(next_state="REVIEW_WAIT")
        assert not any(name == "mark_pr_implementation_no_go" for name, _ in github.mutation_log)
        assert ("mark_pr_implementation_go", (1001,)) not in github.mutation_log
        assert ("arm_auto_merge", (1001,)) not in github.mutation_log

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
        assert ("mark_pr_implementation_go", (1001,)) in github.mutation_log

    def test_go_with_preexisting_thread_reenters_remediation_cycle(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """GO + an open thread remains actionable rather than terminal."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 1)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert result == Continue(next_state="REVIEW_WAIT")
        assert ("mark_pr_implementation_no_go", (1001,)) in github.mutation_log

    def test_failed_audit_with_external_thread_reenters_remediation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An externally authored thread is retried through implementation and review."""
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

        assert result == Continue(next_state="REVIEW_WAIT")
        assert ("mark_pr_implementation_no_go", (1001,)) in github.mutation_log

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

    def test_detached_push_with_unchanged_remote_retries_without_reinvoking_agent(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A pre-push failure retries the existing detached commit exactly once."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="PUSH_WAIT")

        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                value={
                    "detached_push_failure": "remote_unchanged",
                    "detached_push_head_sha": "b" * 40,
                },
                error="detached review push failed while remote head was unchanged",
            ),
            ctx,
        )
        item.state = "EVAL"

        result = stage.step(item, ctx)

        assert result == Continue(next_state="PUSH_WAIT")
        assert item.payload["direct_push_retries"] == 1
        assert item.payload["detached_push_retry_head_sha"] == "b" * 40
        assert "address_error" not in item.payload

    def test_detached_push_retry_cap_preserves_the_checkout(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Repeated local push failures terminate safely instead of failing back."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="PUSH_WAIT")
        item.worktree = "/tmp/review-pr-1001"
        item.payload["direct_push_retries"] = DIRECT_PUSH_RETRY_CAP

        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                value={
                    "detached_push_failure": "remote_unchanged",
                    "detached_push_head_sha": "b" * 40,
                },
                error="detached review push failed while remote head was unchanged",
            ),
            ctx,
        )
        item.state = "EVAL"

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "detached_push_failed")
        assert item.payload["detached_push_failure"] == "remote_unchanged"

    def test_successful_detached_push_resets_the_retry_budget_for_the_next_round(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A later independent failure receives its own bounded retry."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="PUSH_WAIT")
        item.payload["direct_push_retries"] = DIRECT_PUSH_RETRY_CAP

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"pushed": True, "head_sha": "a" * 40}),
            ctx,
        )

        assert "direct_push_retries" not in item.payload
        item.state = "PUSH_WAIT"
        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                value={
                    "detached_push_failure": "remote_unchanged",
                    "detached_push_head_sha": "b" * 40,
                },
                error="detached review push failed while remote head was unchanged",
            ),
            ctx,
        )
        item.state = "EVAL"

        assert stage.step(item, ctx) == Continue(next_state="PUSH_WAIT")

    def test_detached_push_with_advanced_remote_preserves_the_checkout(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A changed remote must not be overwritten or called an agent failure."""
        stage = PrReviewStage()
        ctx = make_ctx(github=FakeStageGitHub(pr_head_branch="1001-auto"))
        item = make_work_item(issue=1, pr=1001, state="PUSH_WAIT")
        item.worktree = "/tmp/review-pr-1001"

        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                value={"detached_push_failure": "remote_changed"},
                error="detached review push observed a different remote head",
            ),
            ctx,
        )
        item.state = "EVAL"

        result = stage.step(item, ctx)

        assert result == Continue(next_state="ENTER")
        assert item.worktree == ""
        assert "preserved_direct_worktrees" not in item.payload
        assert item.payload["direct_pr_worktree_generation"] == 1
        assert "address_error" not in item.payload

        item.state = "ENTER"
        retry = stage.step(item, ctx)

        assert isinstance(retry, JobRequest)
        assert isinstance(retry.job, GitJob)
        assert retry.job.kwargs["isolated_generation"] == 1

    def test_direct_pr_drift_restart_rebinds_the_recreated_checkout(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A fresh direct checkout binds its new snapshot without a rebase."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            pr_review_context={
                "pr_diff": "diff --git a/a.py b/a.py\n+new\n",
                "pr_description": "Closes #1",
                "pr_head_sha": "a" * 40,
                "pr_base_branch": "main",
            }
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, kind=ItemKind.PR, state="PUSH_WAIT")
        item.worktree = "/tmp/review-pr-1001"
        item.branch = "review-branch"
        item.payload.update({})

        assert PrReviewStage._restart_direct_pr_review(item) is None
        item.worktree = "/tmp/review-pr-1001-1"
        item.payload["direct_pr_worktree"] = item.worktree
        item.state = "REVIEW_WAIT"
        restart = stage.step(item, ctx)

        assert isinstance(restart, JobRequest)
        assert isinstance(restart.job, GitJob)
        assert restart.job.op == "verify_pr_review_checkout"
        assert restart.on_done_state == REVIEW_CHECKOUT_WAIT

    def test_direct_pr_drift_restart_clears_pi_bound_review_sessions(
        self, make_work_item: Any
    ) -> None:
        """A recreated checkout cannot resume a binding from the old worktree."""
        item = make_work_item(issue=1, pr=1001, kind=ItemKind.PR, state="PUSH_WAIT")
        item.worktree = "/tmp/review-pr-1001"
        item.session_ids["pr-reviewer"] = "saved-pi-session"
        item.session_bindings["pr-reviewer"] = create_pi_binding(
            session_id="saved-pi-session",
            cwd=Path(item.worktree),
            role=AgentRole.PR_REVIEWER,
            model="pi-reviewer",
        )

        assert PrReviewStage._restart_direct_pr_review(item) is None

        assert "pr-reviewer" not in item.session_ids
        assert "pr-reviewer" not in item.session_bindings

    def test_detached_push_without_a_durable_recovery_receipt_preserves_the_checkout(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The stage must not restart if the worker could not persist provenance."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="PUSH_WAIT")
        item.worktree = "/tmp/review-pr-1001"

        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                value={"detached_push_failure": "remote_changed_unrecorded"},
                error="remote changed and receipt storage failed",
            ),
            ctx,
        )
        item.state = "EVAL"

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.FINISH_FAIL, "detached_push_failed"
        )
        assert item.worktree == "/tmp/review-pr-1001"

    def test_unclassified_direct_push_failure_preserves_the_checkout(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Publication setup uncertainty must not orphan a detached address commit."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="PUSH_WAIT")
        item.worktree = "/tmp/review-pr-1001"
        item.payload["direct_pr_worktree"] = item.worktree

        stage.on_job_done(
            item,
            JobResult(ok=False, error="cannot bind detached review push head"),
            ctx,
        )
        item.state = "EVAL"

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.FINISH_FAIL, "detached_push_failed"
        )
        assert item.payload["detached_push_failure"] == "remote_unconfirmed"
        assert "address_error" not in item.payload

    def test_detached_push_remote_changed_recovery_has_a_bounded_restart_budget(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Repeated concurrent head changes preserve the current checkout and stop."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="PUSH_WAIT")
        item.worktree = "/tmp/review-pr-1001-1"
        item.payload["direct_push_remote_changed_restarts"] = DIRECT_PUSH_REMOTE_CHANGED_RESTART_CAP

        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                value={"detached_push_failure": "remote_changed"},
                error="detached review push observed a different remote head",
            ),
            ctx,
        )
        item.state = "EVAL"

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.FINISH_FAIL, "detached_push_failed"
        )
        assert item.worktree == "/tmp/review-pr-1001-1"
        assert item.payload["detached_push_failure"] == "remote_changed"

    # Severity-aware GO gate tests (#1856)
    def test_same_login_external_reply_remains_actionable(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A reply sharing the host login remains an open remediation item."""

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
                    "author": "mvillmow",
                    "authors": ["mvillmow", "mvillmow"],
                    "review_id": "review-process-advisory",
                    "comments": [
                        {"author": "mvillmow", "body": "original finding"},
                        {"author": "mvillmow", "body": "reviewer reply"},
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
            }
        )

        result = stage.step(item, make_ctx(github=github))

        assert result == Continue(next_state="REVIEW_WAIT")
        assert not any(name == "mark_pr_implementation_go" for name, _ in github.mutation_log)

    def test_go_with_only_minor_thread_reenters_remediation_cycle(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Every open thread is handled by the implementation/reviewer cycle."""
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(0, 2, 0)])  # 0 blocking, 2 minor, 0 external
        ctx = make_ctx(github=github)
        ctx.config.enable_follow_up = False
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert result == Continue(next_state="REVIEW_WAIT")
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

        result = stage.step(item, ctx)

        assert result == Continue(next_state="REVIEW_WAIT")
        assert "reviewed_pr_head_sha" not in item.payload
        assert github.mutation_log == []

    def test_go_with_open_thread_downgrades(self, make_ctx: Any, make_work_item: Any) -> None:
        """Any open thread downgrades a GO result to NOGO."""
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(1, 0, 0)])  # 1 blocking, 0 minor, 0 external
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

    def test_go_with_external_thread_reenters_remediation_cycle(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A pre-existing thread is eligible for implementation remediation."""
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(0, 0, 1)])  # 0 blocking, 0 minor, 1 external
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert result == Continue(next_state="REVIEW_WAIT")
        assert ("mark_pr_implementation_no_go", (1001,)) in github.mutation_log

    def test_go_zero_threads_advances(self, make_ctx: Any, make_work_item: Any) -> None:
        """A clean fresh read is the only route that can advance to merge wait."""
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(0, 0, 0)])  # 0 blocking, 0 minor, 0 external
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
        ``payload["unresolved_threads_before_address"]`` THIS round (the pre-address
        snapshot) — EVAL's progress gate now compares it against this
        same round's post-address count instead of a value carried from
        the previous round (#1863).
        """
        item.payload["review_audit"] = _valid_audit()
        item.payload["reviewed_pr_head_sha"] = "a" * 40
        if pre_address is not None:
            item.payload["unresolved_threads_before_address"] = pre_address
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
        ]
        assert item.payload["pr_review_round"] == 3

    def test_decreasing_threads_earn_extension_rounds(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Strictly decreasing threads admit rounds 4+ until the plateau."""
        stage = PrReviewStage()
        # by_severity/unresolved FIFO supplies each round's POST-ADDRESS count
        # (EVAL's open-thread count): r1=5, r2=3, r3=2, r4=1, r5=1(repeats).
        # pre_address seeds each round's PRE-ADDRESS count (POST's
        # unresolved_threads_before_address) so soft_cap+ rounds prove progress WITHIN
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
        assert github.mutation_log[-1:] == [
            ("gh_issue_add_labels", (13, (STATE_SKIP,))),
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
        unresolved_threads_before_address) and the address leg drives it down to 3
        post-address (EVAL's open-thread count) -- progress made
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
        item.payload["unresolved_threads_before_address"] = 3
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
        item.payload["unresolved_threads_before_address"] = 3
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

    def test_validation_result_is_stored(self, make_ctx: Any, make_work_item: Any) -> None:
        """The reviewer validation output lands on the payload."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="VALIDATE_WAIT")

        stage.on_job_done(item, JobResult(ok=True, value='{"unaddressed": []}'), ctx)
        assert item.payload["validation_result"] == '{"unaddressed": []}'

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

    def test_scope_retraction_publish_failure_is_not_an_address_agent_failure(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The terminal safety outcome must not re-adopt and retry an unsafe draft."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=2137, pr=2346, state="PUSH_WAIT")

        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                value={"scope_retraction_failure": True},
                error="scope retraction incomplete",
            ),
            ctx,
        )

        assert item.payload["scope_retraction_failure"] is True
        assert "address_error" not in item.payload


class TestFullWalks:
    """Full pool-driven walks of the whole stage (canonical FakeWorkerPool)."""

    def test_nogo_round_then_clean_go_walk(self, make_ctx: Any, make_work_item: Any) -> None:
        """An existing open thread exits directly to implementation remediation."""
        stage = PrReviewStage()
        # Existing open threads must be handled by the writer before any new
        # broad review batch is scheduled.
        github = FakeStageGitHub(
            by_severity=[
                (2, 0, 0),
                (2, 0, 0),
                (2, 0, 0),
                (2, 0, 0),
                (2, 0, 0),
                (0, 0, 0),
                (0, 0, 0),
                (0, 0, 0),
            ],
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=21, pr=1001, state="ENTER")
        item.branch = "21-auto-impl"
        item.worktree = "/tmp/wt21"

        pool = FakeWorkerPool()
        outcome = _drive(stage, item, ctx, pool)

        assert isinstance(outcome, StageOutcome)
        assert outcome == StageOutcome(Disposition.FAIL_BACK, "implementation_remediation")
        assert pool.submitted == []
        assert ("mark_pr_implementation_no_go", (1001,)) in github.mutation_log

    def test_unresolved_thread_walk_exhausts_without_terminal_handoff(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An unresolved thread is handed to implementation instead of self-writing in review."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            unresolved=[(3, 0)],
            pr_head_branch="22-auto-impl",
        )  # plateau forever
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
        assert outcome == StageOutcome(Disposition.FAIL_BACK, "implementation_remediation")
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


class TestPreexistingThreadRemediation:
    """Pre-existing threads re-enter the implementation/reviewer workflow."""

    @pytest.mark.parametrize(
        "starting_state",
        [
            pytest.param((True, False), id="implementation-go"),
            pytest.param((False, True), id="implementation-no-go"),
        ],
    )
    def test_replaces_stale_implementation_state_with_no_go_for_remediation(
        self,
        make_ctx: Any,
        make_work_item: Any,
        starting_state: tuple[bool, bool],
    ) -> None:
        """An open thread gets the same recoverable no-go route as any thread."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            unresolved=[(0, 1)],
            pr_impl_state=starting_state,
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=33, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert result == Continue(next_state="REVIEW_WAIT")
        assert github.pr_has_implementation_state_label(1001) == (False, True)
        assert ("mark_pr_implementation_no_go", (1001,)) in github.mutation_log

    def test_open_thread_does_not_emit_a_handoff_comment(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The loop owns the next implementation/reviewer pass for open threads."""
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 2)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=33, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)

        assert result == Continue(next_state="REVIEW_WAIT")
        assert ("mark_pr_implementation_no_go", (1001,)) in github.mutation_log
        assert github.comments.get(1001, []) == []

    def test_unused_handoff_comment_writer_does_not_affect_recovery(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """No terminal handoff comment is written while the thread remains actionable."""

        class CommentFailsGitHub(FakeStageGitHub):
            def post_pr_comment(self, pr_number: int, body: str) -> None:
                raise RuntimeError("gh comment failed")

        stage = PrReviewStage()
        ctx = make_ctx(github=CommentFailsGitHub(unresolved=[(0, 1)]))
        item = make_work_item(issue=34, pr=1001, state="EVAL")
        item.payload["review_audit"] = _valid_audit()

        result = stage.step(item, ctx)  # must not raise

        assert result == Continue(next_state="REVIEW_WAIT")


class TestRealCommitGate:
    """M4 (#1575): a no-commit address turn is never treated as addressed."""

    def test_push_result_records_the_no_commit_flag(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """commit_push requires a matching immutable publication receipt."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=40, pr=1001, state="PUSH_WAIT")

        stage.on_job_done(item, JobResult(ok=True, value=False), ctx)
        assert item.payload["push_no_commit"] is True

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"pushed": True, "head_sha": "a" * 40}),
            ctx,
        )
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
                "host_verification_receipts": [{"head_sha": "a" * 40}],
            }
        )

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"pushed": True, "head_sha": "a" * 40}),
            ctx,
        )

        for key in (
            "review_audit",
            "review_feedback",
            "review_threads",
            "validation_result",
            "reviewed_pr_head_sha",
            "pr_diff",
            "host_verification_receipts",
        ):
            assert key not in item.payload

        item.state = "EVAL"
        result = stage.step(item, ctx)

        assert result == Continue(next_state="COMPACT_REVIEWER_WAIT")
        assert item.attempts["pr_review_iter"] == 0
        assert github.mutation_log == []

    def test_head_drift_after_push_cannot_post_an_implementation_reply(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A later push cannot inherit a reply from the implementation commit."""
        stage = PrReviewStage()
        github = FakeStageGitHub(
            pr_state={"state": "OPEN", "headRefOid": "b" * 40, "autoMergeRequest": None}
        )
        item = make_work_item(issue=40, pr=1001, state="PUSH_WAIT")
        thread = {
            "id": "thread-1",
            "path": "a.py",
            "line": 3,
            "side": "RIGHT",
            "body": "fix this",
            "comments": [{"id": "comment-1", "author": "reviewer", "body": "fix this"}],
        }
        item.payload.update(
            {
                "remediation_threads": [{"thread_id": "thread-1", "body": "fix this"}],
                "remediation_thread_snapshots": [thread],
                "address_output": {
                    "addressed": ["thread-1"],
                    "replies": {"thread-1": "Fixed the guard."},
                },
            }
        )

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"pushed": True, "head_sha": "a" * 40}),
            make_ctx(github=github),
        )

        assert not any(
            name == "post_implementation_thread_replies" for name, _ in github.mutation_log
        )
        assert "pending_thread_reply_receipts" not in item.payload

    def test_reply_handoff_retries_the_pushed_fix_without_a_second_commit(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A transient reply-post failure must not turn a real fix into a no-op loop."""

        class ReplyFailsOnceGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.reply_attempts = 0

            def post_implementation_thread_replies(
                self,
                pr_number: int,
                *,
                expected_head_sha: str,
                threads: list[dict[str, Any]],
                replies: dict[str, str],
                batch_nonce: str,
            ) -> Any:
                self.reply_attempts += 1
                if self.reply_attempts == 1:
                    raise OSError("temporary GitHub transport failure")
                return super().post_implementation_thread_replies(
                    pr_number,
                    expected_head_sha=expected_head_sha,
                    threads=threads,
                    replies=replies,
                    batch_nonce=batch_nonce,
                )

        stage = PrReviewStage()
        github = ReplyFailsOnceGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=40, pr=1001, state="PUSH_WAIT")
        snapshot = {
            "id": "thread-1",
            "path": "a.py",
            "line": 3,
            "side": "RIGHT",
            "body": "fix this",
            "comments": [{"id": "comment-1", "author": "reviewer", "body": "fix this"}],
        }
        item.payload.update(
            {
                "remediation_threads": [{"thread_id": "thread-1", "body": "fix this"}],
                "remediation_thread_snapshots": [snapshot],
                "address_output": {
                    "addressed": ["thread-1"],
                    "replies": {"thread-1": "Fixed the guard."},
                },
            }
        )

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"pushed": True, "head_sha": "a" * 40}),
            ctx,
        )

        assert "pending_implementation_reply_handoff" in item.payload
        assert github.reply_attempts == 0

        item.state = "EVAL"
        assert _complete_github_job(stage, item, ctx) == StageOutcome(
            Disposition.RETRY, "implementation_reply_handoff_retry"
        )
        assert github.reply_attempts == 1
        assert _complete_github_job(stage, item, ctx) == Continue(next_state="REVIEW_WAIT")
        assert github.reply_attempts == 2
        assert "pending_implementation_reply_handoff" not in item.payload
        assert item.payload["push_no_commit"] is False

    def test_retry_rejects_handoff_without_a_persisted_batch_nonce(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A recovered handoff cannot mint a replacement ownership nonce."""
        stage = PrReviewStage()
        github = FakeStageGitHub()
        item = make_work_item(issue=40, pr=1001, state="EVAL")
        item.payload["pending_implementation_reply_handoff"] = {
            "head_sha": "a" * 40,
            "threads": [
                {
                    "id": "thread-1",
                    "path": "a.py",
                    "line": 3,
                    "side": "RIGHT",
                    "body": "fix this",
                    "comments": [{"id": "comment-1", "author": "reviewer", "body": "fix this"}],
                }
            ],
            "replies": {"thread-1": "Fixed the guard."},
        }

        assert _complete_github_job(stage, item, make_ctx(github=github)) == StageOutcome(
            Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid"
        )
        assert github.mutation_log == []

    def test_reply_handoff_waits_for_post_push_head_visibility(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A stale read immediately after push earns a delayed host-only retry."""

        class HeadVisibilityLagGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self._states = deque(
                    [
                        {"state": "OPEN", "headRefOid": "b" * 40, "autoMergeRequest": None},
                        {"state": "OPEN", "headRefOid": "a" * 40, "autoMergeRequest": None},
                    ]
                )
                self.reply_attempts = 0

            def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
                del pr_number
                return self._states.popleft() if self._states else None

            def post_implementation_thread_replies(
                self,
                pr_number: int,
                *,
                expected_head_sha: str,
                threads: list[dict[str, Any]],
                replies: dict[str, str],
                batch_nonce: str,
            ) -> ImplementationThreadReplyResult:
                self.reply_attempts += 1
                return super().post_implementation_thread_replies(
                    pr_number,
                    expected_head_sha=expected_head_sha,
                    threads=threads,
                    replies=replies,
                    batch_nonce=batch_nonce,
                )

        stage = PrReviewStage()
        github = HeadVisibilityLagGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=40, pr=1001, state="PUSH_WAIT")
        snapshot = {
            "id": "thread-1",
            "path": "a.py",
            "line": 3,
            "side": "RIGHT",
            "body": "fix this",
            "comments": [{"id": "comment-1", "author": "reviewer", "body": "fix this"}],
        }
        item.payload.update(
            {
                "remediation_threads": [{"thread_id": "thread-1", "body": "fix this"}],
                "remediation_thread_snapshots": [snapshot],
                "address_output": {
                    "addressed": ["thread-1"],
                    "replies": {"thread-1": "Fixed the guard."},
                },
            }
        )

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"pushed": True, "head_sha": "a" * 40}),
            ctx,
        )

        item.state = "EVAL"
        assert _complete_github_job(stage, item, ctx) == StageOutcome(
            Disposition.RETRY, "implementation_reply_handoff_visibility_wait"
        )
        assert item.payload["retry_delay_s"] == 1.0
        assert github.reply_attempts == 0

        assert _complete_github_job(stage, item, ctx) == Continue(next_state="REVIEW_WAIT")
        assert github.reply_attempts == 1
        assert "pending_implementation_reply_handoff" not in item.payload

    def test_reply_handoff_stops_waiting_when_the_head_stays_drifted(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A persistent different open head drops its stale direct-reply handoff."""

        class HeadStaysDriftedGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.reply_attempts = 0

            def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
                del pr_number
                return {"state": "OPEN", "headRefOid": "b" * 40, "autoMergeRequest": None}

            def post_implementation_thread_replies(
                self,
                pr_number: int,
                *,
                expected_head_sha: str,
                threads: list[dict[str, Any]],
                replies: dict[str, str],
                batch_nonce: str,
            ) -> ImplementationThreadReplyResult:
                self.reply_attempts += 1
                return super().post_implementation_thread_replies(
                    pr_number,
                    expected_head_sha=expected_head_sha,
                    threads=threads,
                    replies=replies,
                    batch_nonce=batch_nonce,
                )

        stage = PrReviewStage()
        github = HeadStaysDriftedGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=40, pr=1001, state="PUSH_WAIT")
        snapshot = {
            "id": "thread-1",
            "path": "a.py",
            "line": 3,
            "side": "RIGHT",
            "body": "fix this",
            "comments": [{"id": "comment-1", "author": "reviewer", "body": "fix this"}],
        }
        item.payload.update(
            {
                "remediation_threads": [{"thread_id": "thread-1", "body": "fix this"}],
                "remediation_thread_snapshots": [snapshot],
                "address_output": {
                    "addressed": ["thread-1"],
                    "replies": {"thread-1": "Fixed the guard."},
                },
            }
        )

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"pushed": True, "head_sha": "a" * 40}),
            ctx,
        )

        item.state = "EVAL"
        for _ in range(2):
            assert _complete_github_job(stage, item, ctx) == StageOutcome(
                Disposition.RETRY, "implementation_reply_handoff_visibility_wait"
            )
        assert _complete_github_job(stage, item, ctx) == Continue(next_state="REVIEW_WAIT")
        assert github.reply_attempts == 0
        assert "pending_implementation_reply_handoff" not in item.payload
        assert github.mutation_log == []

    def test_stale_reply_handoff_restarts_fresh_review_without_retrying(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A post/read-back race must not exhaust retries on a stale snapshot."""

        class ReplyRacedGitHub(FakeStageGitHub):
            def post_implementation_thread_replies(
                self,
                pr_number: int,
                *,
                expected_head_sha: str,
                threads: list[dict[str, Any]],
                replies: dict[str, str],
                batch_nonce: str,
            ) -> ImplementationThreadReplyResult:
                del pr_number, expected_head_sha, threads, replies, batch_nonce
                # The reply may already be visible, but a reviewer comment
                # raced the post-read.  This is a factual stale handoff, not
                # a transport ambiguity that can be replayed.
                return ImplementationThreadReplyResult(blocked_thread_ids=("thread-1",))

        stage = PrReviewStage()
        ctx = make_ctx(github=ReplyRacedGitHub())
        item = make_work_item(issue=40, pr=1001, state="PUSH_WAIT")
        snapshot = {
            "id": "thread-1",
            "path": "a.py",
            "line": 3,
            "side": "RIGHT",
            "body": "fix this",
            "comments": [{"id": "comment-1", "author": "reviewer", "body": "fix this"}],
        }
        item.payload.update(
            {
                "remediation_threads": [{"thread_id": "thread-1", "body": "fix this"}],
                "remediation_thread_snapshots": [snapshot],
                "address_output": {
                    "addressed": ["thread-1"],
                    "replies": {"thread-1": "Fixed the guard."},
                },
            }
        )

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"pushed": True, "head_sha": "a" * 40}),
            ctx,
        )

        item.state = "EVAL"
        assert _complete_github_job(stage, item, ctx) == Continue(next_state="REVIEW_WAIT")
        assert "pending_implementation_reply_handoff" not in item.payload

    def test_mixed_stale_and_transport_reply_batch_retries_only_ambiguous_thread(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A known-stale thread cannot remain in a later transport retry batch."""

        class MixedReplyOutcomeGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.reply_batches: list[tuple[str, ...]] = []

            def post_implementation_thread_replies(
                self,
                pr_number: int,
                *,
                expected_head_sha: str,
                threads: list[dict[str, Any]],
                replies: dict[str, str],
                batch_nonce: str,
            ) -> ImplementationThreadReplyResult:
                del pr_number, expected_head_sha, threads, batch_nonce
                self.reply_batches.append(tuple(sorted(replies)))
                return ImplementationThreadReplyResult(
                    blocked_thread_ids=("stale-thread",),
                    retryable_thread_ids=("ambiguous-thread",),
                    retryable=True,
                )

        stage = PrReviewStage()
        ctx = make_ctx(github=MixedReplyOutcomeGitHub())
        item = make_work_item(issue=40, pr=1001, state="PUSH_WAIT")
        snapshots = [
            {
                "id": thread_id,
                "path": "a.py",
                "line": 3,
                "side": "RIGHT",
                "body": "fix this",
                "comments": [
                    {"id": f"comment-{thread_id}", "author": "reviewer", "body": "fix this"}
                ],
            }
            for thread_id in ("stale-thread", "ambiguous-thread")
        ]
        item.payload.update(
            {
                "remediation_threads": [
                    {"thread_id": snapshot["id"], "body": "fix this"} for snapshot in snapshots
                ],
                "remediation_thread_snapshots": snapshots,
                "address_output": {
                    "addressed": ["stale-thread", "ambiguous-thread"],
                    "replies": {
                        "stale-thread": "Fixed stale thread.",
                        "ambiguous-thread": "Fixed ambiguous thread.",
                    },
                },
            }
        )

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"pushed": True, "head_sha": "a" * 40}),
            ctx,
        )

        item.state = "EVAL"
        assert _complete_github_job(stage, item, ctx) == StageOutcome(
            Disposition.RETRY, "implementation_reply_handoff_retry"
        )
        handoff = item.payload["pending_implementation_reply_handoff"]
        assert [snapshot["id"] for snapshot in handoff["threads"]] == ["ambiguous-thread"]
        assert handoff["replies"] == {"ambiguous-thread": "Fixed ambiguous thread."}

        assert ctx.github.reply_batches == [("ambiguous-thread", "stale-thread")]

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
        """A legacy retry state hands its threads to implementation without a write job."""
        stage = PrReviewStage()
        ctx = make_ctx()
        item = make_work_item(issue=42, pr=1001, state="ADDRESS_WAIT")
        item.worktree = "/tmp/wt"
        item.payload["existing_pr"] = True
        threads = [{"thread_id": "t1", "path": "x.py", "line": 3, "body": "fix the bug"}]
        item.payload["remediation_threads"] = threads
        item.payload["unaddressed_findings"] = threads

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FAIL_BACK, "implementation_remediation")
        assert item.payload["implementation_remediation"] is True

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


class TestAuditPublication:
    """Fresh audit findings and validation reconciliation have separate authority."""

    def test_post_keeps_fresh_audit_findings_independent_of_validation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Validation output cannot suppress or recreate a fresh audit finding."""
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
        item.payload["reviewed_pr_head_sha"] = "a" * 40

        result = _complete_github_job(stage, item, ctx)

        assert isinstance(result, Continue)
        assert github.mutation_log == [("gh_pr_review_post", (1001, "COMMENT"))]
        posted = github.reviews[1001][0]["comments"]
        assert [t["thread_id"] for t in item.payload["raw_review_threads"]] == ["t1", "t2"]
        assert [t.get("thread_id") for t in item.payload["review_threads"]] == ["t1"]
        assert [t.get("thread_id") for t in posted] == ["t1"]

    def test_fresh_audit_publication_uses_the_reviewed_snapshot_after_head_drift(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A later push does not discard the completed snapshot review."""

        class SnapshotPublishingGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__(
                    pr_state={"state": "OPEN", "headRefOid": "b" * 40, "autoMergeRequest": None}
                )
                self.received_diff: str | None = None

            def post_review_threads(
                self,
                pr_number: int,
                threads: list[dict[str, Any]],
                *,
                expected_head_sha: str,
                review_diff: str | None = None,
            ) -> list[dict[str, Any]]:
                assert pr_number == 1001
                assert expected_head_sha == "a" * 40
                self.received_diff = review_diff
                self._log("gh_pr_review_post", pr_number, "COMMENT")
                return [
                    {
                        "id": "review-thread-1",
                        "path": threads[0]["path"],
                        "line": threads[0]["line"],
                        "side": threads[0]["side"],
                        "body": threads[0]["body"],
                    }
                ]

        finding = {
            "path": "x.py",
            "line": 3,
            "side": "RIGHT",
            "severity": "major",
            "body": "Current code loses the review receipt.",
        }
        github = SnapshotPublishingGitHub()
        item = make_work_item(issue=50, pr=1001, state="POST")
        item.payload.update(
            {
                "reviewed_pr_head_sha": "a" * 40,
                "pr_diff": "diff --git a/x.py b/x.py\n@@ -1 +1 @@\n-old\n+new\n",
                "review_threads": [finding],
                "review_audit": ReviewAudit(
                    grade="F",
                    summary="Needs review",
                    findings=(finding,),
                    raw_feedback="review feedback",
                    valid=True,
                ),
            }
        )

        result = _complete_github_job(PrReviewStage(), item, make_ctx(github=github))

        assert result == Continue(next_state="EVAL")
        assert github.received_diff == item.payload["pr_diff"]


class TestProgressCounts:
    """Extension rounds are based on the total open-thread count."""

    def _eval_round(
        self, stage: Any, item: Any, ctx: Any, *, pre_address: int | None = None
    ) -> Any:
        item.payload["review_audit"] = _valid_audit()
        item.payload["reviewed_pr_head_sha"] = "a" * 40
        if pre_address is not None:
            item.payload["unresolved_threads_before_address"] = pre_address
        item.state = "EVAL"
        return stage.step(item, ctx)

    def test_no_open_thread_progress_earns_no_extension(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A plateau in total unresolved threads does not earn round 4.

        5->4->3 total would have earned extensions under a total-count
        metric, but the total count plateaus at 3 — the extension gate
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
        )  # r3 == soft cap: 3 == 3 plateau

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.SKIP
        assert outcome.note == "exhaustion"

    def test_open_thread_decrease_still_earns_extension(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Control: the same walk with a decreasing open-thread count extends."""
        stage = PrReviewStage()
        github = FakeStageGitHub(by_severity=[(5, 0, 0), (4, 0, 0), (3, 0, 0), (3, 0, 0)])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=52, pr=1001, state="EVAL")
        assert stage.on_enter(item, ctx) is None

        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r1: 5 (< soft cap)
        assert isinstance(self._eval_round(stage, item, ctx), Continue)  # r2: 4 (< soft cap)
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
        assert outcome.note == "implementation_remediation"
        assert item.payload["implementation_remediation"] is True

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
