"""Tests for the implementation stage (doc section "4. implementation")."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from hephaestus.automation.pipeline.jobs import AgentJob, BuildTestJob, GitJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.implementation import (
    GIT_ERROR_RETRY_CAP,
    PRE_PR_TEST_ARGV,
    ImplementationStage,
    build_implementation_prompt,
    build_test_fix_prompt,
)
from hephaestus.automation.state_labels import STATE_PLAN_GO, STATE_SKIP
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
            pool.submit(result.job, result.on_done_state)  # type: ignore[arg-type]
            _handle, job_result = pool.completion_q.get_nowait()
            assert not job_result.interrupted  # on_job_done contract precondition
            stage.on_job_done(item, job_result, ctx)
            item.state = result.on_done_state
            continue
        return result
    raise AssertionError("stage driver did not terminate")


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

    def test_test_fix_prompt_carries_failure_output(self) -> None:
        """The test-fix resume prompt embeds the failing pytest output."""
        prompt = build_test_fix_prompt(42, 0, "FAILED tests/unit/test_x.py::test_y")

        assert "FAILED tests/unit/test_x.py::test_y" in prompt
        assert "NOGO" in prompt  # framed as the resume template's NOGO feedback


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

    def test_gate_is_at_or_past_not_equality(self, make_ctx: Any, make_work_item: Any) -> None:
        """Already implementation-go (past plan-go) also satisfies the gate."""
        stage = ImplementationStage()
        github = FakeStageGitHub(labels=["state:implementation-go"])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=7, state="GATE")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "WORKTREE_WAIT"

    def test_gate_existing_pr_with_impl_go_routes_to_ci(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An implementation-go PR with a worktree routes straight to CI."""
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

    def test_gate_existing_pr_with_impl_go_without_worktree_adopts_first(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An implementation-go PR still needs an isolated worktree before CI."""
        stage = ImplementationStage()
        github = FakeStageGitHub(
            open_pr=1001, pr_impl_state=(True, False), pr_head_branch="1-real-branch"
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="GATE")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "WORKTREE_WAIT"
        assert item.pr == 1001
        assert item.branch == "1-real-branch"
        assert item.payload["existing_pr"] is True
        assert item.payload["existing_pr_impl_go"] is True

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
        """An existing non-GO PR is adopted: real head branch, deferral, worktree.

        Adoption re-ensures the auto-merge deferral [durable] and routes
        through WORKTREE_WAIT so pr_review's address leg gets an isolated
        worktree on the ADOPTED branch (never the shared checkout).
        """
        stage = ImplementationStage()
        github = FakeStageGitHub(open_pr=1001, pr_head_branch="1-some-real-branch")
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="GATE")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "WORKTREE_WAIT"
        assert item.pr == 1001
        assert item.branch == "1-some-real-branch"  # never assumed {issue}-auto-impl
        assert item.payload["existing_pr"] is True
        assert github.mutation_log == [("defer_auto_merge", (1001,))]

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
        """A clean adopted worktree skips the implement leg and ADVANCEs."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="DIRTY_DECISION_WAIT")
        item.payload["existing_pr"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "ADOPTED"

        item.state = "ADOPTED"
        outcome = stage.step(item, ctx)
        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.ADVANCE

    def test_adopted_dirty_worktree_salvages_then_advances(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A dirty adopted worktree runs the salvage decision, then ADVANCEs."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, pr=1001, state="DIRTY_DECISION_WAIT")
        item.payload["existing_pr"] = True
        item.payload["worktree_dirty"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert result.job.descr == "dirty_decision"
        assert result.on_done_state == "ADOPTED"


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
        github = FakeStageGitHub(open_pr=1001, pr_head_branch="1-real")
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="GATE")
        item.payload["agent_error_failback"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)  # 1 < budget 2: still adopted
        assert result.next_state == "WORKTREE_WAIT"
        assert item.attempts["implement"] == 1  # the bound moved
        assert "agent_error_failback" not in item.payload  # flag consumed

    def test_reentry_exhaustion_finishes_failed_without_labels(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """At the implement budget the re-adoption terminates, labels untouched."""
        stage = ImplementationStage()
        github = FakeStageGitHub(open_pr=1001, pr_head_branch="1-real")
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, state="GATE")
        item.attempts["implement"] = 1  # one fail-back round trip already
        item.payload["agent_error_failback"] = True

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL
        assert result.note == "agent_error_exhausted"
        assert github.mutation_log == [("defer_auto_merge", (1001,))]

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
        """A failed adopted worktree sync must not flow through ADOPTED_CI."""
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
        ctx = make_ctx()
        ctx.config.enable_advise = False
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
        ctx = make_ctx()
        ctx.config.run_pre_pr_tests = True
        item = make_work_item(issue=1, state="TEST_WAIT")
        item.payload["tests_failed"] = True  # stale prior round result

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, BuildTestJob)
        assert result.job.argv == PRE_PR_TEST_ARGV
        assert result.on_done_state == "COMMIT_PUSH_WAIT"
        assert "tests_failed" not in item.payload  # stale result cleared at submit

    def test_tests_enabled_use_configured_argv(self, make_ctx: Any, make_work_item: Any) -> None:
        """The pre-PR test command comes from config when overridden."""
        stage = ImplementationStage()
        ctx = make_ctx()
        ctx.config.run_pre_pr_tests = True
        ctx.config.pre_pr_test_argv = ("pytest", "tests/custom", "-q")
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
        }
        assert result.on_done_state == "PR_CREATE"

    def test_commit_push_no_commit_sets_skip_payload(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A successful commit_push with value=False skips PR creation."""
        stage = ImplementationStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="COMMIT_PUSH_WAIT")

        stage.on_job_done(item, JobResult(ok=True, value=False), ctx)

        assert item.payload["no_commits"] is True

    def test_pr_create_journals_pr_then_defers_auto_merge(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """PR creation (the journal entry) precedes the auto-merge deferral.

        Durable-order oracle: the mutation_log must show gh_pr_create BEFORE
        defer_auto_merge (legacy runner order :623), both before ADVANCE.
        """
        stage = ImplementationStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=9, state="PR_CREATE")
        item.branch = "9-auto-impl"
        item.payload["issue_title"] = "Add the widget"
        item.payload["implement_summary"] = "Added the widget."

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE
        assert item.pr == 1001
        assert [name for name, _ in github.mutation_log] == ["gh_pr_create", "defer_auto_merge"]
        assert github.mutation_log[1] == ("defer_auto_merge", (1001,))
        # The PR body is a get_pr_description body carrying the closing line.
        assert "Closes #9" in github.prs[1001]["body"]
        assert github.prs[1001]["title"] == "Add the widget"

    def test_pr_create_is_idempotent_for_existing_pr(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An item that already has a PR only re-ensures the deferral."""
        stage = ImplementationStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=9, pr=777, state="PR_CREATE")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE
        assert github.mutation_log == [("defer_auto_merge", (777,))]

    def test_no_commits_applies_skip_durably_before_skip(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The legacy "no commits vs base" error maps to a durable state:skip."""
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
        assert github.mutation_log == [("gh_issue_add_labels", (9, (STATE_SKIP,)))]

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

        Asserts the exact job order and the durable journal order
        (gh_pr_create before defer_auto_merge, both before ADVANCE).
        """
        stage = ImplementationStage()
        github = FakeStageGitHub(labels=["state:plan-go"])
        ctx = make_ctx(github=github)
        ctx.config.run_pre_pr_tests = True
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
        assert [name for name, _ in github.mutation_log] == ["gh_pr_create", "defer_auto_merge"]

    def test_walk_with_red_tests_and_one_fix(self, make_ctx: Any, make_work_item: Any) -> None:
        """A red test run earns exactly one test_fix attempt, then converges."""
        stage = ImplementationStage()
        github = FakeStageGitHub(labels=["state:plan-go"])
        ctx = make_ctx(github=github)
        ctx.config.enable_advise = False
        ctx.config.run_pre_pr_tests = True
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
        ctx = make_ctx(github=github)
        ctx.config.enable_advise = False
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
