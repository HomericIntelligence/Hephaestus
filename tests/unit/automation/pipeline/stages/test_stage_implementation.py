"""Tests for the implementation stage (doc section "4. implementation")."""

from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from hephaestus.automation.address_review_core import _parse_addressed_block
from hephaestus.automation.pipeline.github_jobs import (
    AppendReplyJournalRequest,
    DeliverReplyHandoffRequest,
    FrozenJson,
    GitHubJob,
    RecoverReplyJournalRequest,
    ReplyHandoffAttempted,
    ReplyJournalAppended,
    ReplyJournalRecovered,
)
from hephaestus.automation.pipeline.jobs import AgentJob, BuildTestJob, GitJob, JobResult
from hephaestus.automation.pipeline.reply_handoff import (
    attempt_reply_handoff,
    implementation_reply_handoff,
    implementation_reply_handoff_journal_entry,
    journaled_implementation_reply_handoff,
)
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import (
    Continue,
    ImplementationThreadReplyResult,
    JobRequest,
    StageOutcome,
    implementation as implementation_module,
)
from hephaestus.automation.pipeline.stages.implementation import (
    BRANCH_WORKTREE_OWNER_PENDING_DELAY_S,
    GIT_ERROR_RETRY_CAP,
    PRE_PR_TEST_ARGV,
    ImplementationStage,
    build_implementation_prompt,
    build_test_fix_prompt,
)
from hephaestus.automation.prompts.address_review import get_address_review_prompt
from hephaestus.automation.state_labels import (
    STATE_NEEDS_PLAN,
    STATE_PLAN_BLOCKED,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
    STATE_SKIP,
)
from hephaestus.automation.worktree_manager import BRANCH_WORKTREE_OWNED
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _drive(stage: Any, item: Any, ctx: Any, pool: FakeWorkerPool, max_steps: int = 60) -> Any:
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
            pool.submit(result.job, result.on_done_state)
            _handle, job_result = pool.completion_q.get_nowait()
            assert not job_result.interrupted  # on_job_done contract precondition
            stage.on_job_done(item, job_result, ctx)
            item.state = result.on_done_state
            continue
        return result
    raise AssertionError("stage driver did not terminate")


def _drive_github_jobs(
    stage: ImplementationStage,
    item: Any,
    ctx: Any,
    *,
    max_steps: int = 10,
) -> Any:
    """Execute typed GitHub jobs only after a stage has dispatched them."""
    for _ in range(max_steps):
        result = stage.step(item, ctx)
        if isinstance(result, Continue):
            item.state = result.next_state
            continue
        if not isinstance(result, JobRequest) or not isinstance(result.job, GitHubJob):
            return result
        request = result.job.request
        try:
            if isinstance(request, RecoverReplyJournalRequest):
                threads = request.threads.thaw()
                assert isinstance(threads, list)
                handoff = journaled_implementation_reply_handoff(
                    ctx.github.issue_comments(request.issue_number),
                    pr_number=request.pr_number,
                    threads=threads,
                )
                receipt: object = ReplyJournalRecovered(
                    request=request,
                    handoff=FrozenJson.snapshot(handoff) if handoff is not None else None,
                )
            elif isinstance(request, AppendReplyJournalRequest):
                ctx.github.append_issue_comment(
                    request.issue_number,
                    request.marker,
                    request.body,
                )
                receipt = ReplyJournalAppended(request=request)
            elif isinstance(request, DeliverReplyHandoffRequest):
                receipt = attempt_reply_handoff(request, ctx.github)
                assert isinstance(receipt, ReplyHandoffAttempted)
            else:  # pragma: no cover - the implementation stage has exactly three operations
                raise AssertionError(f"unexpected request: {request!r}")
            job_result = JobResult(ok=True, value=receipt)
        except Exception as error:
            job_result = JobResult(ok=False, error=f"{type(error).__name__}: {error}")
        stage.on_job_done(item, job_result, ctx)
        item.state = result.on_done_state
    raise AssertionError("typed GitHub stage driver did not terminate")


class TestComposedPromptBuilders:
    """Composed top-level builders reuse the base prompts verbatim."""

    def test_implementation_prompt_without_findings_has_no_learnings_block(self) -> None:
        """No advise findings means no team-KB block is appended.

        The base template is reused verbatim via get_implementation_prompt;
        its untrusted-content fence nonce is random per call, so structure
        (not string equality) is asserted.
        """
        prompt = build_implementation_prompt(42, branch_name="42-auto-impl")

        assert "42-auto-impl" in prompt  # base template rendered our kwargs
        assert "#42" in prompt
        assert "## Prior Learnings from Team Knowledge Base" not in prompt

    def test_implementation_prompt_appends_findings_block(self) -> None:
        """Advise findings are appended as the team-KB block."""
        prompt = build_implementation_prompt(42, advise_findings="Use the retry helper.")

        assert "## Prior Learnings from Team Knowledge Base" in prompt
        assert prompt.endswith("Use the retry helper.")

    def test_stage_contract_does_not_make_implementation_go_a_merge_boundary(self) -> None:
        """The module contract must describe the bootstrap containment semantics."""
        contract = implementation_module.__doc__ or ""

        assert "until ``state:implementation-go``" not in contract
        assert "does not create merge eligibility" in contract

    def test_test_fix_prompt_carries_failure_output(self) -> None:
        """The test-fix resume prompt embeds the failing pytest output."""
        prompt = build_test_fix_prompt(42, 0, "FAILED tests/unit/test_x.py::test_y")

        assert "FAILED tests/unit/test_x.py::test_y" in prompt
        assert "Address every concrete finding above" in prompt


class TestImplementationStageOnEnter:
    """on_enter is idempotent and performs no durable writes."""

    def test_on_enter_writes_nothing(self, make_ctx: Any, make_work_item: Any) -> None:
        """on_enter performs no durable writes and always proceeds."""
        stage = ImplementationStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        assert github.mutation_log == []

    def test_on_enter_double_call_is_idempotent(self, make_ctx: Any, make_work_item: Any) -> None:
        """A literal double on_enter changes nothing the second time."""
        stage = ImplementationStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=2, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        snapshot = dict(item.payload)
        assert stage.on_enter(item, ctx) is None

        assert item.payload == snapshot
        assert github.mutation_log == []

    def test_on_enter_without_issue_fails(self, make_ctx: Any, make_work_item: Any) -> None:
        """A work item without an issue number finishes failed on entry."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=None, state="ENTER")

        result = stage.on_enter(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL


class TestGate:
    """GATE: existing-PR fast path + the plan-review verdict gate."""

    def test_enter_advances_to_gate(self, make_ctx: Any, make_work_item: Any) -> None:
        """ENTER advances to GATE."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="ENTER")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "GATE"

    def test_gate_plan_not_go_fails_back(self, make_ctx: Any, make_work_item: Any) -> None:
        """No plan-go label and no PR fails back plan_not_go (-> plan_review)."""
        stage = ImplementationStage()
        github = FakeStageGitHub()  # no labels, no PR
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="GATE")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FAIL_BACK
        assert result.note == "plan_not_go"
        assert github.mutation_log == []  # gate reads only

    def test_gate_plan_go_proceeds_to_worktree(self, make_ctx: Any, make_work_item: Any) -> None:
        """state:plan-go admits the item and defaults the branch name."""
        stage = ImplementationStage()
        github = FakeStageGitHub(labels=["state:plan-go"])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=7, state="GATE")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "WORKTREE_WAIT"
        assert item.branch == "7-auto-impl"

    def test_gate_preserves_preallocated_direct_restart_branch(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Direct-source branch allocation must survive the implementation gate unchanged."""
        stage = ImplementationStage()
        ctx = make_ctx(github=FakeStageGitHub(labels=["state:plan-go"]))
        item = make_work_item(issue=7, state="GATE")
        item.branch = "7-auto-impl-direct-abc123"

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "WORKTREE_WAIT"
        assert item.branch == "7-auto-impl-direct-abc123"

    def test_gate_live_label_failure_cannot_authorize_from_cached_go(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A stale cached GO is diagnostic only when the authoritative read fails."""

        class LabelReadFailsGitHub(FakeStageGitHub):
            def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
                raise RuntimeError("labels unavailable")

        stage = ImplementationStage()
        ctx = make_ctx(github=LabelReadFailsGitHub())
        item = make_work_item(issue=7, state="GATE")
        item.labels_cache = {STATE_PLAN_GO: True}

        with pytest.raises(RuntimeError, match="labels unavailable"):
            stage.step(item, ctx)

        assert item.branch == ""

    def test_gate_blocked_stops_before_existing_pr_adoption(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The operator latch wins even when an implementation PR already exists."""
        stage = ImplementationStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_BLOCKED], open_pr=1001)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=7, state="GATE")

        result = stage.step(item, ctx)

        assert result == StageOutcome(
            Disposition.BLOCKED,
            "plan is blocked pending external intervention",
        )
        assert github.mutation_log == []

    @pytest.mark.parametrize(
        "labels",
        [[], [STATE_NEEDS_PLAN], [STATE_PLAN_NO_GO], [STATE_PLAN_GO, STATE_PLAN_NO_GO]],
    )
    def test_gate_existing_pr_requires_exclusive_authorizing_state(
        self,
        make_ctx: Any,
        make_work_item: Any,
        labels: list[str],
    ) -> None:
        """PR existence cannot replace an exclusive plan/implementation label."""
        stage = ImplementationStage()
        github = FakeStageGitHub(labels=labels, open_pr=1001, pr_head_branch="1-real")
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="GATE")

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FAIL_BACK, "plan_not_go")
        assert item.pr is None
        assert github.mutation_log == []

    def test_gate_existing_pr_rejects_contradictory_pr_state(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Conflicting PR review labels fail closed before adoption writes."""
        stage = ImplementationStage()
        github = FakeStageGitHub(
            open_pr=1001,
            pr_impl_state=(True, True),
            pr_head_branch="1-real",
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="GATE")

        result = stage.step(item, ctx)

        assert result == StageOutcome(
            Disposition.FINISH_FAIL,
            "contradictory_implementation_state",
        )
        assert item.pr is None
        assert github.mutation_log == []

    def test_gate_is_at_or_past_not_equality(self, make_ctx: Any, make_work_item: Any) -> None:
        """Already implementation-go (past plan-go) also satisfies the gate."""
        stage = ImplementationStage()
        github = FakeStageGitHub(labels=["state:implementation-go"])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=7, state="GATE")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "WORKTREE_WAIT"

    def test_gate_existing_pr_with_impl_go_routes_to_merge_wait(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An implementation-go PR with a worktree routes to merge-wait."""
        stage = ImplementationStage()
        github = FakeStageGitHub(
            open_pr=1001, pr_impl_state=(True, False), pr_head_branch="1-real-branch"
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="GATE")
        item.worktree = "/tmp/wt/issue-1"

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FAIL_BACK
        assert result.note == "already_implementation_go_pr"
        assert item.pr == 1001  # set before the fail-back (m7)
        assert item.branch == "1-real-branch"

    def test_gate_existing_pr_with_impl_go_without_worktree_routes_to_merge_wait(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An implementation-go PR does not need adoption before merge-wait."""
        stage = ImplementationStage()
        github = FakeStageGitHub(
            open_pr=1001, pr_impl_state=(True, False), pr_head_branch="1-real-branch"
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="GATE")

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FAIL_BACK, "already_implementation_go_pr")
        assert item.pr == 1001
        assert item.branch == "1-real-branch"

    def test_gate_existing_fork_with_impl_go_routes_to_merge_wait(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A reviewed fork needs no writable head before merge-wait."""
        stage = ImplementationStage()
        github = FakeStageGitHub(
            open_pr=1001,
            pr_impl_state=(True, False),
            pr_head_branch="fork-feature",
            pr_head_writable=False,
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="GATE")

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FAIL_BACK, "already_implementation_go_pr")
        assert item.pr == 1001
        assert item.branch == "fork-feature"

    def test_gate_existing_item_pr_merged_finishes_before_adoption(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A late fail-back must terminalize a PR that merged before adoption."""

        class MergedGitHub(FakeStageGitHub):
            def get_pr_head_branch(self, pr_number: int) -> str | None:
                raise AssertionError("merged PRs should finish before branch adoption")

            def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
                raise AssertionError("merged PRs should finish before label routing")

        stage = ImplementationStage()
        ctx = make_ctx(github=MergedGitHub(pr_state={"state": "MERGED"}))
        item = make_work_item(issue=1, pr=1001, state="GATE")

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_PASS, "merged")

    def test_gate_existing_item_pr_with_merged_at_finishes_before_adoption(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A truthy mergedAt terminalizes even when state is not MERGED."""

        class MergedGitHub(FakeStageGitHub):
            def get_pr_head_branch(self, pr_number: int) -> str | None:
                raise AssertionError("merged PRs should finish before branch adoption")

            def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
                raise AssertionError("merged PRs should finish before label routing")

        stage = ImplementationStage()
        ctx = make_ctx(github=MergedGitHub(pr_state={"state": "OPEN", "mergedAt": "2026-07-10"}))
        item = make_work_item(issue=1, pr=1001, state="GATE")

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_PASS, "merged")

    def test_gate_existing_item_pr_closed_finishes_before_adoption(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A late fail-back must terminalize a PR that closed before adoption."""

        class ClosedGitHub(FakeStageGitHub):
            def get_pr_head_branch(self, pr_number: int) -> str | None:
                raise AssertionError("closed PRs should finish before branch adoption")

            def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
                raise AssertionError("closed PRs should finish before label routing")

        stage = ImplementationStage()
        ctx = make_ctx(github=ClosedGitHub(pr_state={"state": "CLOSED"}))
        item = make_work_item(issue=1, pr=1001, state="GATE")

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "closed")

    def test_gate_existing_pr_without_impl_go_adopts_via_worktree(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An existing non-GO unarmed PR is adopted without mutation.

        Adoption routes through WORKTREE_WAIT so pr_review's address leg gets
        an isolated worktree on the ADOPTED branch (never the shared checkout).
        """
        stage = ImplementationStage()
        github = FakeStageGitHub(
            labels=[STATE_PLAN_GO],
            open_pr=1001,
            pr_head_branch="1-some-real-branch",
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="GATE")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "WORKTREE_WAIT"
        assert item.pr == 1001
        assert item.branch == "1-some-real-branch"  # never assumed {issue}-auto-impl
        assert item.payload["existing_pr"] is True
        assert github.mutation_log == []

    def test_gate_existing_pr_stands_down_when_auto_merge_is_externally_armed(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An external arm blocks adoption and receives zero pipeline mutation."""
        stage = ImplementationStage()
        github = FakeStageGitHub(
            labels=[STATE_PLAN_GO],
            open_pr=1001,
            pr_state={"state": "OPEN", "headRefOid": "a" * 40, "autoMergeRequest": {}},
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="GATE")

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.BLOCKED, "auto_merge_already_armed"
        )
        assert github.mutation_log == []

    def test_gate_existing_pr_rejects_a_partial_state_before_worktree_creation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A missing null auto-merge field cannot authorize existing-PR adoption."""
        stage = ImplementationStage()
        github = FakeStageGitHub(
            labels=[STATE_PLAN_GO],
            open_pr=1001,
            pr_state={"state": "OPEN", "headRefOid": "a" * 40},
        )
        item = make_work_item(issue=1, state="GATE")

        result = stage.step(item, make_ctx(github=github))

        assert result == StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified")
        assert item.worktree == ""
        assert github.mutation_log == []

    def test_gate_existing_fork_pr_fails_closed_before_worktree_or_agent(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Fork heads cannot be addressed by pushing a base-origin branch."""
        stage = ImplementationStage()
        github = FakeStageGitHub(
            labels=[STATE_PLAN_GO],
            open_pr=1001,
            pr_head_branch="fork-feature",
            pr_head_writable=False,
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="GATE")

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "pr_head_not_writable")
        assert github.mutation_log == []

    def test_adopted_worktree_job_syncs_without_trunk_reset(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The adopted branch's worktree is synced, never reset to trunk.

        Anti-clobber (_prepare_worktree_for_existing_pr): refresh_base must
        be False and sync_to_remote True so pushed commits are never
        discarded.
        """
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="WORKTREE_WAIT")
        item.branch = "1-some-real-branch"
        item.payload["existing_pr"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.kwargs == {
            "issue_number": 1,
            "branch_name": "1-some-real-branch",
            "refresh_base": False,
            "repo_root": "/tmp/repo",
            "sync_to_remote": True,
            "pr_number": 1001,
        }

    def test_adopted_clean_worktree_advances_to_pr_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A clean adopted worktree is rebased before review."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="DIRTY_DECISION_WAIT")
        item.payload["existing_pr"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "REBASE_WAIT"

        item.state = "ADOPTED"
        outcome = stage.step(item, ctx)
        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.ADVANCE

    def test_adopted_dirty_worktree_salvages_then_advances(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A dirty adopted worktree runs the salvage decision, then rebases."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="DIRTY_DECISION_WAIT")
        item.payload["existing_pr"] = True
        item.payload["worktree_dirty"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert result.job.descr == "dirty_decision"
        assert result.on_done_state == "REBASE_WAIT"

    def test_rebase_wait_rebases_and_lease_publishes_the_writer_before_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The implementation stage, not the reviewer, owns branch rebasing."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="REBASE_WAIT")
        item.branch = "1-auto-impl"
        item.worktree = "/tmp/implementation-writer"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "rebase"
        assert result.job.descr == "rebase_writer_before_review"
        assert result.job.kwargs == {
            "cwd": Path("/tmp/implementation-writer"),
            "base_branch": "main",
            "remote": "origin",
            "publish_rebased_head": True,
            "branch": "1-auto-impl",
            "expected_remote_sha": "a" * 40,
        }

        stage.on_job_done(item, JobResult(ok=True, value={"rebased": True}), ctx)
        assert stage.step(item, ctx) == Continue(next_state="ADOPTED")

    def test_rebase_conflict_warning_preserves_actionable_reason(
        self,
        make_ctx: Any,
        make_work_item: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The implementation warning explains that the rebase was aborted."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="REBASE_WAIT")
        result = JobResult(
            ok=False,
            value={"rebased": False},
            error="mechanical rebase hit conflicts; aborted",
        )

        with caplog.at_level("WARNING", logger=implementation_module.__name__):
            stage.on_job_done(item, result, ctx)

        assert item.payload["rebase_error"] is True
        assert caplog.messages == [
            "implementation:1: writer rebase failed: mechanical rebase hit conflicts; aborted"
        ]


class TestImplementationStateSkipGate:
    """GATE checks state:skip before either the existing-PR or plan-go path (#1835)."""

    def test_skip_with_existing_pr_skips_without_adoption(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """state:skip on an issue with an open PR skips before any adoption write."""
        stage = ImplementationStage()
        github = FakeStageGitHub(labels=[STATE_SKIP], open_pr=42)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="GATE")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.SKIP
        assert result.note == "state:skip"
        assert github.mutation_log == []  # no defer_auto_merge call

    def test_skip_with_plan_go_skips_and_warns(
        self, make_ctx: Any, make_work_item: Any, caplog: Any
    ) -> None:
        """state:skip + state:plan-go, no existing PR -> SKIP with a loud WARN."""
        stage = ImplementationStage()
        github = FakeStageGitHub(labels=[STATE_SKIP, STATE_PLAN_GO])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=2, state="GATE")

        with caplog.at_level("WARNING"):
            result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.SKIP
        assert result.note == "state:skip"
        assert github.mutation_log == []
        assert any("state:skip AND state:plan-go" in record.message for record in caplog.records)


class TestAgentErrorPingPongBound:
    """M1: pr_review agent_error fail-backs consume the implement budget."""

    def test_reentry_flag_consumes_budget_at_adoption(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A flagged re-entry that adopts a PR consumes attempts["implement"]."""
        stage = ImplementationStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_GO], open_pr=1001, pr_head_branch="1-real")
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="GATE")
        item.payload["agent_error_failback"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)  # 1 < budget 2: still adopted
        assert result.next_state == "WORKTREE_WAIT"
        assert item.attempts["implement"] == 1  # the bound moved
        assert "agent_error_failback" not in item.payload  # flag consumed

    def test_reentry_exhaustion_finishes_failed_with_plan_go(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """At the implement budget the re-adoption terminates, labels untouched."""
        stage = ImplementationStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_GO], open_pr=1001, pr_head_branch="1-real")
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="GATE")
        item.attempts["implement"] = 1  # one fail-back round trip already
        item.payload["agent_error_failback"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL
        assert result.note == "agent_error_exhausted"
        assert github.mutation_log == []

    def test_flag_never_survives_the_fresh_implement_path(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Without an existing PR the flag is dropped (implement job counts)."""
        stage = ImplementationStage()
        github = FakeStageGitHub(labels=["state:plan-go"])  # no PR
        ctx = make_ctx(github=github)
        item = make_work_item(issue=7, state="GATE")
        item.payload["agent_error_failback"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "WORKTREE_WAIT"
        assert item.attempts["implement"] == 0  # the implement job itself counts
        assert "agent_error_failback" not in item.payload


class TestGitErrorRetryCap:
    """M5: transient git RETRYs are bounded by GIT_ERROR_RETRY_CAP."""

    def test_branch_worktree_owner_supersedes_without_retry(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A second issue finishes without retrying or starting implementation."""
        stage = ImplementationStage()
        item = make_work_item(issue=2269, state="WORKTREE_WAIT")
        item.branch = "shared-head"

        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                error=BRANCH_WORKTREE_OWNED,
                value={
                    "branch": "shared-head",
                    "owner_path": "/repo/build/.worktrees/issue-2268",
                },
            ),
            make_ctx(),
        )
        item.state = "DIRTY_DECISION_WAIT"
        outcome = stage.step(
            item,
            make_ctx(branch_worktree_owner_status=lambda _item, _branch, _path: "verified"),
        )

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition is Disposition.FINISH_PASS
        assert "superseded" in outcome.note
        assert item.worktree == ""
        assert item.attempts["implement"] == 0
        assert "git_error_retries" not in item.payload

    def test_external_branch_worktree_holder_fails_closed(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Git holder data without coordinator ownership proof cannot supersede work."""
        stage = ImplementationStage()
        item = make_work_item(issue=2269, state="WORKTREE_WAIT")
        item.branch = "shared-head"

        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                error=BRANCH_WORKTREE_OWNED,
                value={
                    "branch": "shared-head",
                    "owner_path": "/external/manual-worktree",
                },
            ),
            make_ctx(),
        )
        item.state = "DIRTY_DECISION_WAIT"

        outcome = stage.step(
            item,
            make_ctx(branch_worktree_owner_status=lambda _item, _branch, _path: "unverified"),
        )

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition is Disposition.FINISH_FAIL
        assert outcome.note == "branch_worktree_owner_unverified"
        assert item.worktree == ""
        assert item.attempts["implement"] == 0

    def test_pending_branch_worktree_owner_retries_without_losing_the_receipt(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A collision waits for the candidate owner completion without spending budget."""
        stage = ImplementationStage()
        item = make_work_item(issue=2269, state="WORKTREE_WAIT")
        item.branch = "shared-head"
        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                error=BRANCH_WORKTREE_OWNED,
                value={
                    "branch": "shared-head",
                    "owner_path": "/repo/build/.worktrees/issue-2268",
                },
            ),
            make_ctx(),
        )
        item.state = "DIRTY_DECISION_WAIT"

        outcome = stage.step(
            item,
            make_ctx(branch_worktree_owner_status=lambda _item, _branch, _path: "pending"),
        )

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition is Disposition.RETRY
        assert outcome.note == "branch_worktree_owner_pending"
        assert item.payload["retry_delay_s"] == BRANCH_WORKTREE_OWNER_PENDING_DELAY_S
        assert item.payload["branch_worktree_owner"] == {
            "branch": "shared-head",
            "owner_path": "/repo/build/.worktrees/issue-2268",
        }
        assert item.attempts["implement"] == 0
        assert "git_error_retries" not in item.payload

    def test_worktree_failures_retry_to_the_cap_then_fail(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Consecutive worktree failures RETRY twice, then finish failed."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="WORKTREE_WAIT")

        for expected_retry in range(1, GIT_ERROR_RETRY_CAP + 1):
            stage.on_job_done(item, JobResult(ok=False, error="disk full"), ctx)
            item.state = "DIRTY_DECISION_WAIT"
            outcome = stage.step(item, ctx)
            assert isinstance(outcome, StageOutcome)
            assert outcome.disposition == Disposition.RETRY
            assert item.payload["git_error_retries"] == expected_retry
            item.state = "WORKTREE_WAIT"  # coordinator RETRY re-enters

        stage.on_job_done(item, JobResult(ok=False, error="disk full"), ctx)
        item.state = "DIRTY_DECISION_WAIT"
        outcome = stage.step(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.FINISH_FAIL
        assert outcome.note == "git_error"
        assert item.attempts["implement"] == 0  # git failures never burn implement

    def test_adopted_impl_go_worktree_failure_retries_worktree_not_ci(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A failed adopted worktree sync must not bypass the adopted path."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="WORKTREE_WAIT")
        item.branch = "1-real-branch"
        item.worktree = "/tmp/stale-worktree"
        item.payload["existing_pr"] = True
        item.payload["existing_pr_impl_go"] = True
        item.payload["worktree_dirty"] = False

        stage.on_job_done(item, JobResult(ok=False, error="missing remote ref"), ctx)
        item.state = "DIRTY_DECISION_WAIT"
        outcome = stage.step(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.RETRY
        assert outcome.note == "worktree creation failed"
        assert item.state == "WORKTREE_WAIT"
        assert item.worktree == ""
        assert "worktree_dirty" not in item.payload

        retry = stage.step(item, ctx)

        assert isinstance(retry, JobRequest)
        assert isinstance(retry.job, GitJob)
        assert retry.job.op == "create_worktree"
        assert retry.on_done_state == "DIRTY_DECISION_WAIT"

    def test_push_failures_share_the_same_cap(self, make_ctx: Any, make_work_item: Any) -> None:
        """Consecutive push failures hit the same bounded-RETRY path."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="COMMIT_PUSH_WAIT")
        item.payload["git_error_retries"] = GIT_ERROR_RETRY_CAP  # at the cap

        stage.on_job_done(item, JobResult(ok=False, error="remote hung up"), ctx)
        item.state = "PR_CREATE"
        outcome = stage.step(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.FINISH_FAIL
        assert outcome.note == "git_error"

    def test_worktree_success_resets_the_counter(self, make_ctx: Any, make_work_item: Any) -> None:
        """A successful worktree job ends the consecutive-failure streak."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="WORKTREE_WAIT")
        item.payload["git_error_retries"] = GIT_ERROR_RETRY_CAP

        stage.on_job_done(item, JobResult(ok=True, value="/tmp/wt"), ctx)

        assert "git_error_retries" not in item.payload

    def test_push_success_resets_the_counter(self, make_ctx: Any, make_work_item: Any) -> None:
        """A successful commit+push ends the consecutive-failure streak."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="COMMIT_PUSH_WAIT")
        item.payload["git_error_retries"] = 1

        stage.on_job_done(item, JobResult(ok=True, value=True), ctx)

        assert "git_error_retries" not in item.payload


class TestWorktreeAndAdvise:
    """WORKTREE_WAIT / DIRTY_DECISION_WAIT / ADVISE_WAIT."""

    def test_worktree_wait_dispatches_to_handler(self, make_ctx: Any, make_work_item: Any) -> None:
        """WORKTREE_WAIT routes through the dedicated state handler."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="WORKTREE_WAIT")
        expected = StageOutcome(Disposition.ADVANCE, "dispatched")

        with patch.object(stage, "_worktree_wait", create=True, return_value=expected) as mock:
            result = stage.step(item, ctx)

        assert result == expected
        mock.assert_called_once_with(item, ctx)

    def test_worktree_wait_requests_refreshed_worktree(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """WORKTREE_WAIT submits a create_worktree GitJob with refresh_base."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="WORKTREE_WAIT")
        item.branch = "1-auto-impl"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "create_worktree"
        assert result.job.kwargs == {
            "issue_number": 1,
            "branch_name": "1-auto-impl",
            "refresh_base": True,
            "repo_root": "/tmp/repo",
        }
        assert result.on_done_state == "DIRTY_DECISION_WAIT"

    def test_remediation_reuses_the_writer_stowed_for_read_only_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A review handoff never creates a second worktree for the PR branch."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="WORKTREE_WAIT")
        item.branch = "1-auto-impl"
        item.worktree = "/tmp/implementation-writer"
        item.payload.update(
            {
                "implementation_remediation": True,
                "implementation_writer_restored": True,
            }
        )

        result = stage.step(item, ctx)

        assert result == Continue(next_state="DIRTY_DECISION_WAIT")
        assert item.payload["worktree_dirty"] is False
        assert "implementation_writer_restored" not in item.payload

    def test_direct_scope_worktree_uses_its_bootstrap_pin_without_refresh(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A direct fresh implementation is cut only from the synchronized SHA."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="WORKTREE_WAIT")
        item.branch = "1-auto-impl"
        item.payload["_direct_scope_base_sha"] = "a" * 40

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.kwargs == {
            "issue_number": 1,
            "branch_name": "1-auto-impl",
            "refresh_base": False,
            "repo_root": "/tmp/repo",
            "base_sha": "a" * 40,
        }

    def test_direct_scope_worktree_forwards_the_coordinator_run_nonce(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A restart-specific branch also receives a restart-specific worktree path."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="WORKTREE_WAIT")
        run_nonce = "d" * 32
        item.branch = f"1-auto-impl-direct-{run_nonce}"
        item.payload.update(
            {
                "_direct_scope_base_sha": "a" * 40,
                "_direct_scope_worktree_nonce": run_nonce,
            }
        )

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.kwargs["direct_worktree_nonce"] == run_nonce

    def test_adopted_direct_pr_reuses_the_nonce_encoded_in_its_branch(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An existing direct PR returns to its original managed writer path."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="WORKTREE_WAIT")
        run_nonce = "d" * 32
        item.branch = f"1-auto-impl-direct-{run_nonce}"
        item.payload.update(
            {
                "existing_pr": True,
                # A new direct cursor has a different nonce, so recovery must
                # derive the writer identity from the adopted PR branch.
                "_direct_scope_worktree_nonce": "e" * 32,
            }
        )

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.kwargs == {
            "issue_number": 1,
            "branch_name": f"1-auto-impl-direct-{run_nonce}",
            "refresh_base": False,
            "repo_root": "/tmp/repo",
            "direct_worktree_nonce": run_nonce,
            "sync_to_remote": True,
            "pr_number": 1001,
        }

    def test_adopted_direct_pr_rejects_a_malformed_writer_identity(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An external lookalike branch cannot claim a managed direct path."""
        stage = ImplementationStage()
        item = make_work_item(issue=1, pr=1001, state="WORKTREE_WAIT")
        item.branch = "1-auto-impl-direct-not-a-trusted-nonce"
        item.payload["existing_pr"] = True

        assert stage.step(item, make_ctx()) == StageOutcome(
            Disposition.FINISH_FAIL,
            "direct_scope_worktree_nonce_invalid",
        )

    def test_worktree_result_stores_path_and_dirty_state(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A dict worktree result stores path, dirty flag, status, and diff."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="WORKTREE_WAIT")
        result = JobResult(
            ok=True, value={"path": "/tmp/wt", "dirty": True, "status": "M x.py", "diff": "+x"}
        )

        stage.on_job_done(item, result, ctx)

        assert item.worktree == "/tmp/wt"
        assert item.payload["worktree_dirty"] is True
        assert item.payload["worktree_status"] == "M x.py"
        assert item.payload["worktree_diff"] == "+x"

    def test_direct_worktree_result_stores_remote_reservation_receipt(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Finished can release a failed direct run only from this exact receipt."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="WORKTREE_WAIT")
        item.branch = "1-auto-impl"
        item.payload["_direct_scope_base_sha"] = "a" * 40
        result = JobResult(
            ok=True,
            value={
                "path": "/tmp/wt",
                "direct_scope_reservation": {
                    "branch": "1-auto-impl",
                    "base_sha": "a" * 40,
                },
            },
        )

        stage.on_job_done(item, result, ctx)

        assert item.worktree == "/tmp/wt"
        assert item.payload["_direct_scope_reservation"] == {
            "branch": "1-auto-impl",
            "base_sha": "a" * 40,
        }

    def test_direct_worktree_rejects_missing_remote_reservation_receipt(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A fresh direct agent cannot run without its creation lease receipt."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="WORKTREE_WAIT")
        item.branch = "1-auto-impl"
        item.payload["_direct_scope_base_sha"] = "a" * 40

        stage.on_job_done(item, JobResult(ok=True, value={"path": "/tmp/wt"}), ctx)

        assert item.worktree == ""
        assert item.payload["git_error"] is True

    def test_adopted_direct_worktree_does_not_require_a_fresh_reservation_receipt(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An existing direct PR reuses its writer without creating a new lease."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="WORKTREE_WAIT")
        item.branch = "1-auto-impl-direct-" + "b" * 32
        item.payload["existing_pr"] = True
        # The new direct cursor's bootstrap pin is still present, but it does
        # not authorize or require a replacement reservation for this PR.
        item.payload["_direct_scope_base_sha"] = "a" * 40

        stage.on_job_done(
            item,
            JobResult(
                ok=True,
                value={
                    "path": "/tmp/wt",
                    "dirty": False,
                    "status": "",
                    "diff": "",
                },
            ),
            ctx,
        )

        assert item.worktree == "/tmp/wt"
        assert "git_error" not in item.payload
        assert "_direct_scope_reservation" not in item.payload
        item.state = "DIRTY_DECISION_WAIT"
        assert stage.step(item, ctx) == Continue(next_state="REBASE_WAIT")

    def test_worktree_string_result_stores_path(self, make_ctx: Any, make_work_item: Any) -> None:
        """A plain string worktree result is the worktree path."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="WORKTREE_WAIT")

        stage.on_job_done(item, JobResult(ok=True, value="/tmp/wt2"), ctx)

        assert item.worktree == "/tmp/wt2"

    def test_worktree_failure_retries_without_burning_budget(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A failed worktree job RETRYs; the implement budget is untouched."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="WORKTREE_WAIT")

        stage.on_job_done(item, JobResult(ok=False, error="disk full"), ctx)
        item.state = "DIRTY_DECISION_WAIT"
        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.RETRY
        assert item.attempts["implement"] == 0  # transient: no budget burned

    def test_confirmed_direct_reservation_collision_finishes_without_retry(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A confirmed branch collision stops before an agent or generic retry can run."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="WORKTREE_WAIT")
        item.branch = "1-auto-impl-direct-abc123"
        item.payload["_direct_scope_base_sha"] = "a" * 40

        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                error="direct_scope_reservation_collision",
                value={
                    "direct_scope_reservation_collision": {"branch": "1-auto-impl-direct-abc123"}
                },
            ),
            ctx,
        )
        item.state = "DIRTY_DECISION_WAIT"
        outcome = stage.step(item, ctx)

        assert outcome == StageOutcome(
            Disposition.FINISH_FAIL,
            "direct_scope_reservation_collision",
        )
        assert item.attempts["implement"] == 0
        assert "git_error_retries" not in item.payload

    def test_worktree_rollback_failure_preserves_direct_reservation_receipt(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Finished can clean an early remote lease after the retry budget is exhausted."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="WORKTREE_WAIT")
        item.branch = "1-auto-impl"
        item.payload["_direct_scope_base_sha"] = "a" * 40

        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                value={
                    "direct_scope_reservation": {
                        "branch": "1-auto-impl",
                        "base_sha": "a" * 40,
                    }
                },
                error="worktree creation failed; reservation rollback failed",
            ),
            ctx,
        )

        assert item.payload["_direct_scope_reservation"] == {
            "branch": "1-auto-impl",
            "base_sha": "a" * 40,
        }
        assert item.payload["git_error"] is True

    def test_clean_worktree_skips_dirty_decision(self, make_ctx: Any, make_work_item: Any) -> None:
        """A clean worktree continues straight to ADVISE_WAIT."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="DIRTY_DECISION_WAIT")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "ADVISE_WAIT"

    def test_dirty_worktree_requests_decision_job(self, make_ctx: Any, make_work_item: Any) -> None:
        """A dirty reused worktree submits the COMMIT/STASH decision job."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="DIRTY_DECISION_WAIT")
        item.branch = "1-auto-impl"
        item.payload["worktree_dirty"] = True
        item.payload["worktree_status"] = "M x.py"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.job.descr == "dirty_decision"
        assert result.on_done_state == "ADVISE_WAIT"
        assert result.job.prompt_kwargs["branch_name"] == "1-auto-impl"
        assert result.job.prompt_kwargs["status_text"] == "M x.py"

    def test_dirty_decision_result_stored(self, make_ctx: Any, make_work_item: Any) -> None:
        """The COMMIT/STASH decision lands in the payload."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="DIRTY_DECISION_WAIT")

        stage.on_job_done(item, JobResult(ok=True, value="COMMIT"), ctx)

        assert item.payload["dirty_decision"] == "COMMIT"

    def test_advise_disabled_skips_to_implement(self, make_ctx: Any, make_work_item: Any) -> None:
        """Advise disabled continues straight to IMPLEMENT_WAIT."""
        stage = ImplementationStage()
        ctx = make_ctx(config_overrides={"no_advise": True})
        item = make_work_item(issue=1, state="ADVISE_WAIT")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "IMPLEMENT_WAIT"

    def test_advise_enabled_requests_advise_job(self, make_ctx: Any, make_work_item: Any) -> None:
        """Advise enabled submits the advise job, findings land in payload."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="ADVISE_WAIT")

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert result.job.descr == "advise"
        assert result.on_done_state == "IMPLEMENT_WAIT"

        stage.on_job_done(item, JobResult(ok=True, value="prior learnings"), ctx)
        assert item.payload["advise_findings"] == "prior learnings"


class TestImplementBudget:
    """IMPLEMENT_WAIT budget semantics: agent_error consumes the budget."""

    def test_existing_pr_remediation_uses_the_writer_agent_and_review_threads(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Review findings are fixed by the implementation stage, never pr_review."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="IMPLEMENT_WAIT")
        item.branch = "review-branch"
        item.worktree = "/tmp/implementation-writer"
        item.payload.update(
            {
                "existing_pr": True,
                "implementation_remediation": True,
                "remediation_threads": [
                    {"thread_id": "thread-1", "path": "a.py", "line": 3, "body": "fix it"}
                ],
                "pr_diff": "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
            }
        )

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.descr == "address_review"
        assert result.job.session_agent == "implementer"
        assert result.job.prompt_kwargs["pr_number"] == 1001
        assert result.job.prompt_builder is get_address_review_prompt
        assert result.job.allowed_tools == "Read,Write,Edit,Glob,Grep,Bash,Task,Skill"
        assert result.job.parse is _parse_addressed_block
        assert json.loads(result.job.prompt_kwargs["threads_json"]) == [
            {"thread_id": "thread-1", "path": "a.py", "line": 3, "body": "fix it"}
        ]
        assert result.on_done_state == "TEST_WAIT"

    def test_remediation_preserves_the_scope_retraction_publish_guard(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Scope-control findings are checked by the writer before publishing."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="IMPLEMENT_WAIT")
        item.worktree = "/tmp/implementation-writer"
        item.payload.update(
            {
                "implementation_remediation": True,
                "reviewed_pr_base_sha": "a" * 40,
                "remediation_threads": [
                    {
                        "thread_id": "thread-1",
                        "path": "out-of-scope.py",
                        "line": 3,
                        "body": (
                            "Remove this unrelated change.\n"
                            "<!-- hephaestus-scope-retraction-paths: "
                            '["out-of-scope.py"] -->'
                        ),
                    }
                ],
            }
        )

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.prompt_kwargs["scope_retraction_paths"] == ("out-of-scope.py",)
        item.state = "COMMIT_PUSH_WAIT"
        push = stage.step(item, ctx)
        assert isinstance(push, JobRequest)
        assert isinstance(push.job, GitJob)
        assert push.job.kwargs["scope_retraction_paths"] == ("out-of-scope.py",)
        assert push.job.kwargs["scope_retraction_base_sha"] == "a" * 40

    def test_implement_requests_job_with_advise_findings(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """IMPLEMENT_WAIT submits the composed implement prompt job."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="IMPLEMENT_WAIT")
        item.branch = "1-auto-impl"
        item.payload["advise_findings"] = "use helpers"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.job.descr == "implement"
        assert result.job.prompt_builder is build_implementation_prompt
        assert result.on_done_state == "TEST_WAIT"
        assert result.job.prompt_kwargs["advise_findings"] == "use helpers"
        assert result.job.prompt_kwargs["branch_name"] == "1-auto-impl"
        assert item.attempts["implement"] == 0  # submission burns nothing

    def test_implement_resumes_the_saved_direct_agent_session(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A retried direct implementation keeps its prior working context."""
        stage = ImplementationStage()
        ctx = make_ctx(config_overrides={"agent": "codex"})
        item = make_work_item(issue=1, state="IMPLEMENT_WAIT")
        item.session_ids["implementer"] = "implement-session-id"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.resume_session_id == "implement-session-id"

    def test_implement_submission_clears_stale_results(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Submission clears any stale error/summary from a prior attempt."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="IMPLEMENT_WAIT")
        item.payload["implement_error"] = True  # stale attempt-1 failure
        item.payload["implement_summary"] = "old summary"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert "implement_error" not in item.payload
        assert "implement_summary" not in item.payload

    def test_implement_success_counts_attempt_and_stores_summary(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A completed implement job counts one attempt and stores its output."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="IMPLEMENT_WAIT")

        stage.on_job_done(item, JobResult(ok=True, value="Implemented the helper"), ctx)

        assert item.attempts["implement"] == 1
        assert item.payload["implement_summary"] == "Implemented the helper"

    def test_implement_failure_counts_attempt_and_retries(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """agent_error consumes the implement budget then RETRYs (doc rule)."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="IMPLEMENT_WAIT")

        stage.on_job_done(item, JobResult(ok=False, error="claude crashed"), ctx)
        item.state = "TEST_WAIT"
        result = stage.step(item, ctx)

        assert item.attempts["implement"] == 1  # budget consumed
        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.RETRY
        assert result.note == "agent_error"

    def test_agent_tool_failure_with_no_diff_never_enters_no_commit_cleanup(
        self,
        make_ctx: Any,
        make_work_item: Any,
    ) -> None:
        """A failed tool session cannot reach commit, skip, or branch cleanup."""
        stage = ImplementationStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=2634, state="IMPLEMENT_WAIT")
        reservation = {"branch": "2634-auto-impl", "base_sha": "a" * 40}
        item.payload["_direct_scope_reservation"] = reservation

        stage.on_job_done(
            item,
            JobResult(
                ok=False,
                error=(
                    "agent_error: codex_nested_sandbox_unsupported: "
                    "run the outer loop outside the enclosing API sandbox"
                ),
                stdout_tail="No edits were made; no diff exists.",
            ),
            ctx,
        )
        item.state = "TEST_WAIT"

        outcome = stage.step(item, ctx)

        assert outcome == StageOutcome(Disposition.RETRY, "agent_error")
        assert item.payload["_direct_scope_reservation"] == reservation
        assert "no_commits" not in item.payload
        assert "_direct_scope_local_branch_cleanup" not in item.payload
        assert github.mutation_log == []

    def test_implement_budget_exhaustion_finishes_failed(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """At the ROUTES implement budget (2) the stage finishes failed."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="IMPLEMENT_WAIT")
        item.attempts["implement"] = 2  # ROUTES budget consumed

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL
        assert result.note == "implement_exhausted"

    def test_implement_budget_comes_from_routes(self, make_ctx: Any) -> None:
        """The implement/test_fix budgets are ROUTES data, not stage constants."""
        ctx = make_ctx()

        assert ctx.budget("implement") == 2
        assert ctx.budget("test_fix") == 1

    def test_budget_override_changes_the_cap(self, make_ctx: Any, make_work_item: Any) -> None:
        """An injected budget_fn (ROUTES stand-in) moves the exhaustion point."""
        from dataclasses import replace

        stage = ImplementationStage()
        ctx = replace(make_ctx(), budget_fn=lambda name: 5)
        item = make_work_item(issue=1, state="IMPLEMENT_WAIT")
        item.attempts["implement"] = 2  # would exhaust under the default budget

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)  # 2 < 5: still admitted


class TestTestsAndFix:
    """TEST_WAIT / TESTFIX_WAIT: optional pre-PR tests bounded by test_fix."""

    def test_tests_disabled_skip_to_commit_push(self, make_ctx: Any, make_work_item: Any) -> None:
        """run_pre_pr_tests=False (the default) skips the test leg."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="TEST_WAIT")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "COMMIT_PUSH_WAIT"

    def test_tests_enabled_request_build_test_job(self, make_ctx: Any, make_work_item: Any) -> None:
        """run_pre_pr_tests=True submits the vetted pytest BuildTestJob."""
        stage = ImplementationStage()
        ctx = make_ctx(config_overrides={"run_pre_pr_tests": True})
        item = make_work_item(issue=1, state="TEST_WAIT")
        item.payload["tests_failed"] = True  # stale prior round result

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, BuildTestJob)
        expected_argv = ("uv", "run", "pytest", "tests", "-q", "--tb=short")
        assert expected_argv == PRE_PR_TEST_ARGV
        assert result.job.argv == expected_argv
        assert result.on_done_state == "COMMIT_PUSH_WAIT"
        assert "tests_failed" not in item.payload  # stale result cleared at submit

    def test_tests_enabled_use_configured_argv(self, make_ctx: Any, make_work_item: Any) -> None:
        """The pre-PR test command comes from config when overridden."""
        stage = ImplementationStage()
        ctx = make_ctx(
            config_overrides={
                "run_pre_pr_tests": True,
                "pre_pr_test_argv": ("pytest", "tests/custom", "-q"),
            }
        )
        item = make_work_item(issue=1, state="TEST_WAIT")

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, BuildTestJob)
        assert result.job.argv == ("pytest", "tests/custom", "-q")

    def test_failed_tests_route_to_testfix(self, make_ctx: Any, make_work_item: Any) -> None:
        """A red test run stores the output and routes to TESTFIX_WAIT."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="TEST_WAIT")

        stage.on_job_done(
            item, JobResult(ok=False, value=1, stdout_tail="FAILED test_x", error="exit 1"), ctx
        )
        item.state = "COMMIT_PUSH_WAIT"
        result = stage.step(item, ctx)

        assert item.payload["tests_failed"] is True
        assert "FAILED test_x" in item.payload["test_output"]
        assert isinstance(result, Continue)
        assert result.next_state == "TESTFIX_WAIT"

    def test_green_tests_clear_failure_state(self, make_ctx: Any, make_work_item: Any) -> None:
        """A green run clears any prior failure payload."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="TEST_WAIT")
        item.payload["tests_failed"] = True
        item.payload["test_output"] = "old"

        stage.on_job_done(item, JobResult(ok=True, value=0), ctx)

        assert "tests_failed" not in item.payload
        assert "test_output" not in item.payload

    def test_green_tests_record_command_receipt_for_pr(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A passing host test run leaves its exact command and outcome for the PR."""
        stage = ImplementationStage()
        ctx = make_ctx(config_overrides={"run_pre_pr_tests": True})
        item = make_work_item(issue=1, state="TEST_WAIT")

        stage.step(item, ctx)
        stage.on_job_done(item, JobResult(ok=True, value=0), ctx)

        assert item.payload["test_receipt"] == "`uv run pytest tests -q --tb=short` — passed"

    def test_testfix_requests_resume_job(self, make_ctx: Any, make_work_item: Any) -> None:
        """TESTFIX_WAIT submits the composed test-failure resume job."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="TESTFIX_WAIT")
        item.payload["test_output"] = "FAILED test_y"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.job.descr == "test_fix"
        assert result.job.prompt_builder is build_test_fix_prompt
        assert result.job.prompt_kwargs["test_output"] == "FAILED test_y"
        assert result.on_done_state == "TEST_WAIT"

        stage.on_job_done(item, JobResult(ok=True, value="fixed"), ctx)
        assert item.attempts["test_fix"] == 1

    def test_testfix_resumes_the_saved_direct_implementer_session(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A test repair continues the implementation conversation."""
        stage = ImplementationStage()
        ctx = make_ctx(config_overrides={"agent": "codex"})
        item = make_work_item(issue=1, state="TESTFIX_WAIT")
        item.session_ids["implementer"] = "implement-session-id"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.resume_session_id == "implement-session-id"

    def test_testfix_budget_exhaustion_finishes_failed(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """At the test_fix budget (1) still-red tests finish failed."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="TESTFIX_WAIT")
        item.attempts["test_fix"] = 1

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL
        assert result.note == "tests_red"


class TestCommitPushAndPrCreate:
    """COMMIT_PUSH_WAIT / PR_CREATE: durable journal entry + deferral order."""

    def test_pushed_remediation_with_malformed_reply_mapping_returns_to_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A valid pushed head survives a malformed exact-thread reply mapping."""
        stage = ImplementationStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="COMMIT_PUSH_WAIT")
        item.payload.update(
            {
                "implementation_remediation": True,
                "remediation_thread_snapshots": [
                    {
                        "id": "thread-1",
                        "path": "a.py",
                        "line": 3,
                        "body": "fix it",
                        "comments": [{"id": "comment-1", "author": "reviewer", "body": "fix it"}],
                    }
                ],
                "remediation_output": {
                    "addressed": ["thread-l"],
                    "replies": {"thread-l": "[Response] Fixed the missing guard."},
                },
            }
        )

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"pushed": True, "head_sha": "b" * 40}),
            ctx,
        )

        assert "remediation_reply_error" not in item.payload
        assert "pending_implementation_reply_handoff" not in item.payload
        assert "implementation_remediation" not in item.payload
        assert "remediation_output" not in item.payload
        assert not any(
            name == "post_implementation_thread_replies" for name, _ in github.mutation_log
        )

        item.state = "PR_CREATE"
        assert _drive_github_jobs(stage, item, ctx) == StageOutcome(
            Disposition.ADVANCE, "PR #1001 ready for review"
        )

    def test_no_commit_remediation_with_malformed_reply_mapping_fails_closed(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Malformed replies remain terminal when there is no new head to review."""
        stage = ImplementationStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="COMMIT_PUSH_WAIT")
        item.payload.update(
            {
                "implementation_remediation": True,
                "remediation_thread_snapshots": [
                    {
                        "id": "thread-1",
                        "path": "a.py",
                        "line": 3,
                        "body": "fix it",
                        "comments": [{"id": "comment-1", "author": "reviewer", "body": "fix it"}],
                    }
                ],
                "remediation_output": {
                    "addressed": ["thread-l"],
                    "replies": {"thread-l": "[Response] Fixed the missing guard."},
                },
            }
        )

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"pushed": False, "head_sha": "a" * 40}),
            ctx,
        )

        assert item.payload["remediation_reply_error"] is True
        assert "pending_implementation_reply_handoff" not in item.payload
        assert not any(
            name == "post_implementation_thread_replies" for name, _ in github.mutation_log
        )

        item.state = "PR_CREATE"
        assert stage.step(item, ctx) == StageOutcome(
            Disposition.FINISH_FAIL, "implementation_reply_failed"
        )

    def test_remediation_push_posts_response_replies_after_the_commit(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The writer posts one [Response] reply per addressed review thread."""
        stage = ImplementationStage()
        github = FakeStageGitHub(
            pr_state={"state": "OPEN", "headRefOid": "b" * 40, "autoMergeRequest": None}
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="COMMIT_PUSH_WAIT")
        item.payload.update(
            {
                "implementation_remediation": True,
                "remediation_thread_snapshots": [
                    {
                        "id": "thread-1",
                        "path": "a.py",
                        "line": 3,
                        "body": "fix it",
                        "comments": [{"id": "comment-1", "author": "reviewer", "body": "fix it"}],
                    }
                ],
                "remediation_output": {
                    "addressed": ["thread-1"],
                    "replies": {"thread-1": "[Response] Fixed the missing guard."},
                },
            }
        )

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"pushed": True, "head_sha": "b" * 40}),
            ctx,
        )

        item.state = "PR_CREATE"
        assert _drive_github_jobs(stage, item, ctx) == StageOutcome(
            Disposition.ADVANCE, "PR #1001 ready for review"
        )
        assert ("post_implementation_thread_replies", (1001, ("thread-1",))) in github.mutation_log
        assert "implementation_remediation" not in item.payload

    def test_remediation_reply_handoff_waits_for_github_head_visibility(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A post-push visibility lag retries the exact response batch without another commit."""

        class HeadVisibilityLagGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self._states = deque(
                    [
                        {"state": "OPEN", "headRefOid": "a" * 40, "autoMergeRequest": None},
                        {"state": "OPEN", "headRefOid": "b" * 40, "autoMergeRequest": None},
                    ]
                )

            def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
                del pr_number
                return self._states.popleft() if self._states else None

        stage = ImplementationStage()
        github = HeadVisibilityLagGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="COMMIT_PUSH_WAIT")
        item.payload.update(
            {
                "implementation_remediation": True,
                "remediation_thread_snapshots": [
                    {
                        "id": "thread-1",
                        "path": "a.py",
                        "line": 3,
                        "side": "RIGHT",
                        "body": "fix it",
                        "comments": [{"id": "comment-1", "author": "reviewer", "body": "fix it"}],
                    }
                ],
                "remediation_output": {
                    "addressed": ["thread-1"],
                    "replies": {"thread-1": "[Response] Fixed the missing guard."},
                },
            }
        )

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"pushed": True, "head_sha": "b" * 40}),
            ctx,
        )

        assert "pending_implementation_reply_handoff" in item.payload
        assert "remediation_reply_error" not in item.payload
        assert not any(
            name == "post_implementation_thread_replies" for name, _ in github.mutation_log
        )

        item.state = "PR_CREATE"
        assert _drive_github_jobs(stage, item, ctx) == StageOutcome(
            Disposition.RETRY, "implementation_reply_handoff_visibility_wait"
        )
        assert item.payload["retry_delay_s"] == 1.0
        assert not any(
            name == "post_implementation_thread_replies" for name, _ in github.mutation_log
        )

        assert _drive_github_jobs(stage, item, ctx) == StageOutcome(
            Disposition.ADVANCE, "PR #1001 ready for review"
        )
        assert ("post_implementation_thread_replies", (1001, ("thread-1",))) in github.mutation_log
        assert "pending_implementation_reply_handoff" not in item.payload
        assert "implementation_remediation" not in item.payload

    def test_remediation_reply_handoff_retries_a_transient_pr_state_read(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A failed PR-state read preserves the exact batch for one host-only retry."""

        class TransientReadGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self._states = deque(
                    [
                        None,
                        {
                            "state": "OPEN",
                            "headRefOid": "b" * 40,
                            "autoMergeRequest": None,
                        },
                    ]
                )

            def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
                del pr_number
                return self._states.popleft() if self._states else None

        stage = ImplementationStage()
        github = TransientReadGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="COMMIT_PUSH_WAIT")
        item.payload.update(
            {
                "implementation_remediation": True,
                "remediation_thread_snapshots": [
                    {
                        "id": "thread-1",
                        "path": "a.py",
                        "line": 3,
                        "side": "RIGHT",
                        "body": "fix it",
                        "comments": [{"id": "comment-1", "author": "reviewer", "body": "fix it"}],
                    }
                ],
                "remediation_output": {
                    "addressed": ["thread-1"],
                    "replies": {"thread-1": "[Response] Fixed the missing guard."},
                },
            }
        )

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"pushed": True, "head_sha": "b" * 40}),
            ctx,
        )
        item.state = "PR_CREATE"

        assert _drive_github_jobs(stage, item, ctx) == StageOutcome(
            Disposition.RETRY, "implementation_reply_handoff_retry"
        )
        assert "pending_implementation_reply_handoff" in item.payload
        assert not any(
            name == "post_implementation_thread_replies" for name, _ in github.mutation_log
        )

        assert _drive_github_jobs(stage, item, ctx) == StageOutcome(
            Disposition.ADVANCE, "PR #1001 ready for review"
        )
        assert (
            github.mutation_log.count(("post_implementation_thread_replies", (1001, ("thread-1",))))
            == 1
        )

    def test_remediation_reply_handoff_backoffs_for_a_lagging_thread_snapshot(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Adapter-side head lag uses the visibility delay rather than transport retries."""

        class ThreadSnapshotLagGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__(
                    pr_state={
                        "state": "OPEN",
                        "headRefOid": "b" * 40,
                        "autoMergeRequest": None,
                    }
                )
                self._reply_results = deque(
                    [
                        ImplementationThreadReplyResult(
                            retryable_thread_ids=("thread-1",),
                            retryable=True,
                            visibility_lag=True,
                        )
                    ]
                )

            def post_implementation_thread_replies(
                self,
                pr_number: int,
                *,
                expected_head_sha: str,
                threads: list[dict[str, Any]],
                replies: dict[str, str],
                batch_nonce: str,
            ) -> ImplementationThreadReplyResult:
                if self._reply_results:
                    return self._reply_results.popleft()
                return super().post_implementation_thread_replies(
                    pr_number,
                    expected_head_sha=expected_head_sha,
                    threads=threads,
                    replies=replies,
                    batch_nonce=batch_nonce,
                )

        stage = ImplementationStage()
        github = ThreadSnapshotLagGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="COMMIT_PUSH_WAIT")
        item.payload.update(
            {
                "implementation_remediation": True,
                "remediation_thread_snapshots": [
                    {
                        "id": "thread-1",
                        "path": "a.py",
                        "line": 3,
                        "side": "RIGHT",
                        "body": "fix it",
                        "comments": [{"id": "comment-1", "author": "reviewer", "body": "fix it"}],
                    }
                ],
                "remediation_output": {
                    "addressed": ["thread-1"],
                    "replies": {"thread-1": "[Response] Fixed the missing guard."},
                },
            }
        )

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"pushed": True, "head_sha": "b" * 40}),
            ctx,
        )
        item.state = "PR_CREATE"

        assert _drive_github_jobs(stage, item, ctx) == StageOutcome(
            Disposition.RETRY, "implementation_reply_handoff_visibility_wait"
        )
        assert item.payload["retry_delay_s"] == 1.0
        assert "pending_implementation_reply_handoff_retries" not in item.payload

        assert _drive_github_jobs(stage, item, ctx) == StageOutcome(
            Disposition.ADVANCE, "PR #1001 ready for review"
        )
        assert (
            github.mutation_log.count(("post_implementation_thread_replies", (1001, ("thread-1",))))
            == 1
        )

    def test_remediation_reply_handoff_reconstructs_after_restart_without_new_commit(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A restart replays its exact GitHub-journaled batch without another agent or commit."""

        class TransientReadGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__()
                self._states = deque(
                    [
                        None,
                        {
                            "state": "OPEN",
                            "headRefOid": "b" * 40,
                            "autoMergeRequest": None,
                        },
                    ]
                )

            def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
                del pr_number
                return self._states.popleft() if self._states else None

        stage = ImplementationStage()
        github = TransientReadGitHub()
        ctx = make_ctx(github=github)
        publisher = make_work_item(issue=1, pr=1001, state="COMMIT_PUSH_WAIT")
        publisher.payload.update(
            {
                "implementation_remediation": True,
                "remediation_thread_snapshots": [
                    {
                        "id": "thread-1",
                        "path": "a.py",
                        "line": 3,
                        "side": "RIGHT",
                        "body": "fix it",
                        "review_commit_sha": "a" * 40,
                        "pr_state": {
                            "state": "OPEN",
                            "headRefOid": "a" * 40,
                            "autoMergeRequest": None,
                        },
                        "comments": [{"id": "comment-1", "author": "reviewer", "body": "fix it"}],
                    }
                ],
                "remediation_output": {
                    "addressed": ["thread-1"],
                    "replies": {"thread-1": "[Response] Verified the already-pushed guard."},
                },
            }
        )

        # The original writer records its exact, already-validated response
        # before the process is interrupted after its push.
        stage.on_job_done(
            publisher,
            JobResult(ok=True, value={"pushed": True, "head_sha": "b" * 40}),
            ctx,
        )
        publisher.state = "PR_CREATE"
        assert _drive_github_jobs(stage, publisher, ctx) == StageOutcome(
            Disposition.RETRY,
            "implementation_reply_handoff_retry",
        )

        resumed = make_work_item(issue=1, pr=1001, state="IMPLEMENT_WAIT")
        # The normal restarted read sees the writer's new head and may see a
        # relocated diff anchor.  These mutable fields must not invalidate an
        # otherwise identical source-review conversation.
        post_push_snapshots = [
            {
                **publisher.payload["remediation_thread_snapshots"][0],
                "path": "renamed.py",
                "line": 97,
                "body": "fix it at its new location",
                "pr_state": {
                    "state": "OPEN",
                    "headRefOid": "b" * 40,
                    "autoMergeRequest": None,
                },
            }
        ]
        resumed.payload.update(
            {
                "implementation_remediation": True,
                "remediation_threads": post_push_snapshots,
                "remediation_thread_snapshots": post_push_snapshots,
            }
        )

        assert _drive_github_jobs(stage, resumed, ctx) == StageOutcome(
            Disposition.ADVANCE, "PR #1001 ready for review"
        )
        assert (
            github.mutation_log.count(("post_implementation_thread_replies", (1001, ("thread-1",))))
            == 1
        )

        stale = make_work_item(issue=1, pr=1001, state="IMPLEMENT_WAIT")
        changed_head_snapshots = [
            {
                **post_push_snapshots[0],
                "pr_state": {
                    "state": "OPEN",
                    "headRefOid": "c" * 40,
                    "autoMergeRequest": None,
                },
            }
        ]
        stale.payload.update(
            {
                "implementation_remediation": True,
                "remediation_threads": changed_head_snapshots,
                "remediation_thread_snapshots": changed_head_snapshots,
            }
        )

        stale_result = _drive_github_jobs(stage, stale, ctx)

        assert isinstance(stale_result, JobRequest)
        assert stale_result.job.descr == "address_review"
        assert "pending_implementation_reply_handoff" not in stale.payload

    def test_remediation_reply_handoff_retries_a_transient_journal_write_without_a_new_commit(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An immutable-journal write failure retries only the prepared batch."""

        class TransientJournalGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__(
                    pr_state={
                        "state": "OPEN",
                        "headRefOid": "b" * 40,
                        "autoMergeRequest": None,
                    }
                )
                self.journal_calls = 0

            def append_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
                self.journal_calls += 1
                if self.journal_calls == 1:
                    raise OSError("temporary GitHub outage")
                super().append_issue_comment(issue_number, marker, body)

        stage = ImplementationStage()
        github = TransientJournalGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="COMMIT_PUSH_WAIT")
        item.payload.update(
            {
                "implementation_remediation": True,
                "remediation_thread_snapshots": [
                    {
                        "id": "thread-1",
                        "path": "a.py",
                        "line": 3,
                        "side": "RIGHT",
                        "body": "fix it",
                        "comments": [{"id": "comment-1", "author": "reviewer", "body": "fix it"}],
                    }
                ],
                "remediation_output": {
                    "addressed": ["thread-1"],
                    "replies": {"thread-1": "[Response] Fixed the missing guard."},
                },
            }
        )

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"pushed": True, "head_sha": "b" * 40}),
            ctx,
        )

        assert github.journal_calls == 0
        assert "pending_implementation_reply_handoff" in item.payload
        assert "pending_implementation_reply_handoff_journal" in item.payload
        assert item.attempts.get("implement", 0) == 0

        item.state = "PR_CREATE"
        assert _drive_github_jobs(stage, item, ctx) == StageOutcome(
            Disposition.RETRY, "implementation_reply_handoff_journal_retry"
        )
        assert github.journal_calls == 1
        assert _drive_github_jobs(stage, item, ctx) == StageOutcome(
            Disposition.ADVANCE, "PR #1001 ready for review"
        )
        assert github.journal_calls == 2
        assert "pending_implementation_reply_handoff_journal" not in item.payload
        assert (
            github.mutation_log.count(("post_implementation_thread_replies", (1001, ("thread-1",))))
            == 1
        )

    def test_remediation_reply_handoff_warns_when_source_review_head_is_unchanged(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A no-op run posts its reply with a warning for thorough reviewer analysis."""
        stage = ImplementationStage()
        github = FakeStageGitHub(
            pr_state={"state": "OPEN", "headRefOid": "a" * 40, "autoMergeRequest": None}
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="COMMIT_PUSH_WAIT")
        item.payload.update(
            {
                "implementation_remediation": True,
                "remediation_thread_snapshots": [
                    {
                        "id": "thread-1",
                        "path": "a.py",
                        "line": 3,
                        "side": "RIGHT",
                        "body": "fix it",
                        "review_commit_sha": "b" * 40,
                        "pr_state": {
                            "state": "OPEN",
                            "headRefOid": "a" * 40,
                            "autoMergeRequest": None,
                        },
                        "comments": [{"id": "comment-1", "author": "reviewer", "body": "fix it"}],
                    }
                ],
                "remediation_output": {
                    "addressed": ["thread-1"],
                    "replies": {"thread-1": "[Response] The existing behavior is correct."},
                },
            }
        )

        stage.on_job_done(
            item,
            JobResult(ok=True, value={"pushed": False, "head_sha": "a" * 40}),
            ctx,
        )

        assert "remediation_reply_error" not in item.payload
        assert "pending_implementation_reply_handoff" in item.payload

        item.state = "PR_CREATE"
        assert _drive_github_jobs(stage, item, ctx) == StageOutcome(
            Disposition.ADVANCE, "PR #1001 ready for review"
        )
        assert github._thread_replies["thread-1"][-1]["body"] == (
            "[Response] The existing behavior is correct.\n\n"
            "[auto-msg] reply has no corresponding commit, review thoroughly"
        )

    def test_no_commit_warning_reserves_space_at_reply_limit(self) -> None:
        """Appending the required warning never invalidates a maximal reply."""
        reply = implementation_module._append_no_commit_reply_warning("x" * 4_000)

        assert len(reply) <= 4_000
        assert "[auto-msg] reply truncated to fit review limit" in reply
        assert reply.endswith("[auto-msg] reply has no corresponding commit, review thoroughly")

    def test_commit_push_requests_git_job(self, make_ctx: Any, make_work_item: Any) -> None:
        """COMMIT_PUSH_WAIT submits the commit_push GitJob."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="COMMIT_PUSH_WAIT")
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
            "agent_model": "claude-haiku-4-5",
        }
        assert result.on_done_state == "PR_CREATE"

    def test_commit_push_uses_configured_codex_implementer_model(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Commit-message generation inherits the CLI-selected Codex tier and effort."""
        stage = ImplementationStage()
        ctx = make_ctx(
            config_overrides={
                "agent": "codex",
                "implementer_model": "sol",
                "implementer_reasoning_effort": "medium",
            }
        )
        item = make_work_item(issue=1, state="COMMIT_PUSH_WAIT")
        item.branch = "1-auto-impl"
        item.worktree = "/tmp/wt"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.kwargs["agent_model"] == "sol:medium"

    def test_direct_scope_commit_push_carries_its_remote_reservation_pin(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The publish job can reject a remote writer that changed the reservation."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="COMMIT_PUSH_WAIT")
        item.branch = "1-auto-impl"
        item.worktree = "/tmp/wt"
        item.payload["_direct_scope_base_sha"] = "a" * 40

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.kwargs["expected_remote_sha"] == "a" * 40

    def test_adopted_direct_commit_push_uses_its_pr_branch_without_a_fresh_pin(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An adopted PR must not lease-push against its cursor's trunk SHA."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="COMMIT_PUSH_WAIT")
        item.branch = "1-auto-impl-direct-" + "b" * 32
        item.worktree = "/tmp/wt"
        item.payload["existing_pr"] = True
        item.payload["_direct_scope_base_sha"] = "a" * 40

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert "expected_remote_sha" not in result.job.kwargs

    def test_commit_push_no_commit_sets_skip_payload(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A successful commit_push with value=False skips PR creation."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="COMMIT_PUSH_WAIT")

        stage.on_job_done(item, JobResult(ok=True, value=False), ctx)

        assert item.payload["no_commits"] is True

    def test_direct_no_commit_transfers_receipt_to_local_branch_cleanup(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Removing the remote no-op reservation must not strand its local ref."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="COMMIT_PUSH_WAIT")
        item.payload["_direct_scope_reservation"] = {
            "branch": "1-auto-impl",
            "base_sha": "a" * 40,
        }

        stage.on_job_done(item, JobResult(ok=True, value=False), ctx)

        assert item.payload["no_commits"] is True
        assert "_direct_scope_reservation" not in item.payload
        assert item.payload["_direct_scope_local_branch_cleanup"] == {
            "branch": "1-auto-impl",
            "base_sha": "a" * 40,
        }

    def test_commit_push_success_consumes_direct_reservation_receipt(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A published branch must not be released by the terminal cleanup."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="COMMIT_PUSH_WAIT")
        item.payload["_direct_scope_reservation"] = {
            "branch": "1-auto-impl",
            "base_sha": "a" * 40,
        }

        stage.on_job_done(item, JobResult(ok=True, value=True), ctx)

        assert "_direct_scope_reservation" not in item.payload

    def test_pr_create_journals_pr_without_auto_merge_mutation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """PR creation journals only the PR; merge authority is external."""
        stage = ImplementationStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=9, state="PR_CREATE")
        item.branch = "9-auto-impl"
        item.payload["issue_title"] = "Add the widget"
        item.payload["implement_summary"] = (
            "Added the widget.\n\n"
            "Full suite: 7,000 passed. Changes remain uncommitted at "
            "/private/tmp/issue-9."
        )

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE
        assert item.pr == 1001
        assert [name for name, _ in github.mutation_log] == ["gh_pr_create"]
        # The PR body is a get_pr_description body carrying the closing line.
        assert "Closes #9" in github.prs[1001]["body"]
        assert "Not run by the automation pipeline" in github.prs[1001]["body"]
        assert "7,000 passed" not in github.prs[1001]["body"]
        assert "remain uncommitted" not in github.prs[1001]["body"]
        assert "/private/tmp" not in github.prs[1001]["body"]
        assert github.prs[1001]["title"] == "chore: Add the widget"

    @pytest.mark.parametrize(
        ("issue_title", "expected_title"),
        [
            ("fix(ci): align commit enforcement", "fix(ci): align commit enforcement"),
            ("fix(): repair title normalization", "fix: repair title normalization"),
            ("fix: ", "fix: update"),
        ],
    )
    def test_pr_create_normalizes_issue_title_to_strict_conventional_form(
        self,
        make_ctx: Any,
        make_work_item: Any,
        issue_title: str,
        expected_title: str,
    ) -> None:
        """The created PR title always satisfies the strict squash-title gate."""
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=9, state="PR_CREATE")
        item.branch = "9-auto-impl"
        item.payload["issue_title"] = issue_title

        ImplementationStage().step(item, ctx)

        assert github.prs[1001]["title"] == expected_title

    def test_pr_create_includes_passing_host_test_receipt(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """PR testing metadata identifies the command the host actually ran."""
        stage = ImplementationStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=9, state="PR_CREATE")
        item.branch = "9-auto-impl"
        item.payload["test_receipt"] = "`uv run pytest tests/unit/example.py -q` — passed"

        stage.step(item, ctx)

        assert item.pr == 1001
        assert "`uv run pytest tests/unit/example.py -q` — passed" in github.prs[1001]["body"]

    def test_pr_create_does_not_call_the_removed_auto_merge_mutator(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A legacy mutator override cannot affect PR creation."""

        class DeferFailsGitHub(FakeStageGitHub):
            def defer_auto_merge(self, pr_number: int) -> None:
                raise RuntimeError(f"PR #{pr_number} remains armed")

        stage = ImplementationStage()
        ctx = make_ctx(github=DeferFailsGitHub())
        item = make_work_item(issue=9, state="PR_CREATE")
        item.branch = "9-auto-impl"

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.ADVANCE, "PR #1001 ready for review"
        )
        assert item.pr == 1001

    def test_pr_create_is_idempotent_for_existing_pr(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An item that already has a PR advances without a merge mutation."""
        stage = ImplementationStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=9, pr=777, state="PR_CREATE")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE
        assert github.mutation_log == []

    def test_no_commits_applies_skip_durably_before_skip(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """No-PR legacy no-commit handling still maps to a durable skip."""
        stage = ImplementationStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=9, state="COMMIT_PUSH_WAIT")

        stage.on_job_done(
            item, JobResult(ok=False, error="RuntimeError: no commits between main and head"), ctx
        )
        item.state = "PR_CREATE"
        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.SKIP
        assert github.mutation_log == [
            ("gh_issue_add_labels", (9, (STATE_SKIP,))),
            ("gh_issue_upsert_comment", (9, "<!-- hephaestus-state-skip-reason -->")),
        ]

    def test_no_commits_with_externally_armed_pr_blocks_without_skip_label(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A late external arm owns the PR and forbids the skip-label mutation."""
        stage = ImplementationStage()
        github = FakeStageGitHub(
            pr_state={
                "state": "OPEN",
                "headRefOid": "a" * 40,
                "autoMergeRequest": {"enabledAt": "2026-07-24T00:00:00Z"},
            }
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=9, pr=1001, state="PR_CREATE", payload={"no_commits": True})

        assert stage.step(item, ctx) == StageOutcome(
            Disposition.BLOCKED, "auto_merge_already_armed"
        )
        assert item.payload["no_commits"] is True
        assert github.mutation_log == []

    def test_no_commits_with_partial_pr_state_fails_without_skip_label(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An incomplete PR read cannot authorize the skip-label mutation."""
        stage = ImplementationStage()
        github = FakeStageGitHub(pr_state={"state": "OPEN", "headRefOid": "a" * 40})
        ctx = make_ctx(github=github)
        item = make_work_item(issue=9, pr=1001, state="PR_CREATE", payload={"no_commits": True})

        assert stage.step(item, ctx) == StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified")
        assert item.payload["no_commits"] is True
        assert github.mutation_log == []

    def test_no_commits_with_confirmed_unarmed_pr_applies_skip_label(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A complete, unarmed retained PR still permits the no-commit skip."""
        stage = ImplementationStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=9, pr=1001, state="PR_CREATE", payload={"no_commits": True})

        assert stage.step(item, ctx) == StageOutcome(Disposition.SKIP, "no commits vs base")
        assert github.mutation_log == [
            ("gh_issue_add_labels", (9, (STATE_SKIP,))),
            ("gh_issue_upsert_comment", (9, "<!-- hephaestus-state-skip-reason -->")),
        ]

    def test_skip_label_write_is_non_fatal(self, make_ctx: Any, make_work_item: Any) -> None:
        """A failing state:skip write never turns the SKIP into a crash."""

        class AddFailsGitHub(FakeStageGitHub):
            def add_labels(self, issue_number: int, labels: list[str]) -> None:
                raise RuntimeError("gh add failed")

        stage = ImplementationStage()
        ctx = make_ctx(github=AddFailsGitHub())
        item = make_work_item(issue=9, state="PR_CREATE")
        item.payload["no_commits"] = True

        result = stage.step(item, ctx)  # must not raise

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.SKIP

    def test_push_failure_retries_without_pr(self, make_ctx: Any, make_work_item: Any) -> None:
        """A non-"no commits" push failure RETRYs with no PR created."""
        stage = ImplementationStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=9, state="COMMIT_PUSH_WAIT")

        stage.on_job_done(item, JobResult(ok=False, error="remote hung up"), ctx)
        item.state = "PR_CREATE"
        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.RETRY
        assert github.mutation_log == []

    def test_push_failure_retry_reenters_commit_push_without_pr_mutation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A retried failed push must resubmit commit_push before creating a PR."""
        stage = ImplementationStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=9, state="COMMIT_PUSH_WAIT")
        item.branch = "9-auto-impl"
        item.worktree = "/tmp/wt"

        stage.on_job_done(item, JobResult(ok=False, error="remote hung up"), ctx)
        item.state = "PR_CREATE"
        retry = stage.step(item, ctx)

        assert retry == StageOutcome(Disposition.RETRY, "commit_push failed")
        assert item.state == "COMMIT_PUSH_WAIT"
        assert github.mutation_log == []

        retry_job = stage.step(item, ctx)

        assert isinstance(retry_job, JobRequest)
        assert isinstance(retry_job.job, GitJob)
        assert retry_job.job.op == "commit_push"
        assert github.mutation_log == []

    def test_unknown_state_fails(self, make_ctx: Any, make_work_item: Any) -> None:
        """An unknown state finishes failed instead of looping silently."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="BOGUS")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL

    def test_no_issue_number_fails(self, make_ctx: Any, make_work_item: Any) -> None:
        """Step without an issue number finishes failed."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=None, state="ENTER")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL


class TestFullWalks:
    """Full pool-driven walks of the whole stage (canonical FakeWorkerPool)."""

    def test_happy_path_walk(self, make_ctx: Any, make_work_item: Any) -> None:
        """GATE -> worktree -> advise -> implement -> tests -> push -> PR.

        Asserts the exact job order and PR creation journal.
        """
        stage = ImplementationStage()
        github = FakeStageGitHub(labels=["state:plan-go"])
        ctx = make_ctx(
            github=github,
            config_overrides={"run_pre_pr_tests": True},
        )
        item = make_work_item(issue=5, state="ENTER")
        item.payload["issue_title"] = "Add the widget"

        pool = FakeWorkerPool()
        pool.script(
            JobResult(ok=True, value={"path": "/tmp/wt5", "dirty": False}),  # worktree
            JobResult(ok=True, value="prior learnings"),  # advise
            JobResult(ok=True, value="Implemented the widget."),  # implement
            JobResult(ok=True, value=0),  # pre-PR tests green
            JobResult(ok=True, value=True),  # commit_push
        )

        outcome = _drive(stage, item, ctx, pool)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.ADVANCE
        assert [h.job.descr for h in pool.submitted] == [
            "create_worktree",
            "advise",
            "implement",
            "pre_pr_tests",
            "commit_push",
        ]
        assert item.worktree == "/tmp/wt5"
        assert item.attempts["implement"] == 1
        assert item.pr == 1001
        assert [name for name, _ in github.mutation_log] == ["gh_pr_create"]

    def test_walk_with_red_tests_and_one_fix(self, make_ctx: Any, make_work_item: Any) -> None:
        """A red test run earns exactly one test_fix attempt, then converges."""
        stage = ImplementationStage()
        github = FakeStageGitHub(labels=["state:plan-go"])
        ctx = make_ctx(
            github=github,
            config_overrides={"no_advise": True, "run_pre_pr_tests": True},
        )
        item = make_work_item(issue=6, state="ENTER")

        pool = FakeWorkerPool()
        pool.script(
            JobResult(ok=True, value={"path": "/tmp/wt6", "dirty": False}),  # worktree
            JobResult(ok=True, value="done"),  # implement
            JobResult(ok=False, value=1, stdout_tail="FAILED test_z"),  # tests red
            JobResult(ok=True, value="fixed"),  # test_fix resume
            JobResult(ok=True, value=0),  # tests green
            JobResult(ok=True, value=True),  # commit_push
        )

        outcome = _drive(stage, item, ctx, pool)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.ADVANCE
        assert [h.job.descr for h in pool.submitted] == [
            "create_worktree",
            "implement",
            "pre_pr_tests",
            "test_fix",
            "pre_pr_tests",
            "commit_push",
        ]
        assert item.attempts["test_fix"] == 1

    def test_walk_agent_error_retry_then_exhaustion(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Two agent_error implement runs consume the budget; the third entry fails.

        Doc rule: agent_error -> RETRY consumes the implement budget (2);
        exhaustion -> finished(fail).
        """
        stage = ImplementationStage()
        github = FakeStageGitHub(labels=["state:plan-go"])
        ctx = make_ctx(github=github, config_overrides={"no_advise": True})
        item = make_work_item(issue=8, state="ENTER")

        for expected_attempts in (1, 2):
            pool = FakeWorkerPool()
            pool.script(
                JobResult(ok=True, value={"path": "/tmp/wt8"}),  # worktree
                JobResult(ok=False, error="529 overload"),  # implement crash
            )
            outcome = _drive(stage, item, ctx, pool)
            assert isinstance(outcome, StageOutcome)
            assert outcome.disposition == Disposition.RETRY
            assert outcome.note == "agent_error"
            assert item.attempts["implement"] == expected_attempts
            item.state = "ENTER"  # coordinator RETRY re-enters the stage

        pool = FakeWorkerPool()
        pool.script(JobResult(ok=True, value={"path": "/tmp/wt8"}))  # worktree
        outcome = _drive(stage, item, ctx, pool)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.FINISH_FAIL
        assert outcome.note == "implement_exhausted"
        assert github.mutation_log == []  # exhaustion here owns no labels

    def test_reply_journal_append_dispatches_without_inline_github_calls(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The coordinator step only freezes and dispatches the journal append."""

        class InlineGitHubForbidden:
            def append_issue_comment(self, *_args: object, **_kwargs: object) -> None:
                raise AssertionError("GitHub append ran inline")

            def gh_pr_state(self, *_args: object, **_kwargs: object) -> object:
                raise AssertionError("GitHub state read ran inline")

            def post_implementation_thread_replies(
                self, *_args: object, **_kwargs: object
            ) -> object:
                raise AssertionError("GitHub reply mutation ran inline")

        threads = [
            {
                "id": "thread-1",
                "comments": [{"id": "comment-1", "body": "fix it"}],
            }
        ]
        handoff = implementation_reply_handoff(
            "a" * 40,
            threads,
            {"thread-1": "[Response] fixed"},
            "b" * 32,
        )
        assert handoff is not None
        journal = implementation_reply_handoff_journal_entry(7, handoff)
        assert journal is not None
        marker, body = journal
        item = make_work_item(issue=3, pr=7, state="REPLY_JOURNAL_APPEND_WAIT")
        item.payload.update(
            {
                "pending_implementation_reply_handoff": handoff,
                "pending_implementation_reply_handoff_journal": {
                    "marker": marker,
                    "body": body,
                },
            }
        )
        stage = ImplementationStage()
        ctx = make_ctx(github=InlineGitHubForbidden())

        started = time.monotonic()
        result = stage.step(item, ctx)
        elapsed = time.monotonic() - started

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitHubJob)
        assert result.job.request == AppendReplyJournalRequest(
            issue_number=3,
            marker=marker,
            body=body,
        )
        assert elapsed < 0.25

        stage.on_job_done(
            item,
            JobResult(ok=True, value=ReplyJournalAppended(request=result.job.request)),
            ctx,
        )
        assert "pending_implementation_reply_handoff_journal" not in item.payload
        assert item.payload["pending_implementation_reply_handoff"] == handoff

        item.state = result.on_done_state
        assert stage.step(item, ctx) == Continue(next_state="REPLY_HANDOFF_WAIT")
