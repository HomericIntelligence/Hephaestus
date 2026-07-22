"""Cross-stage composition: the agent_error ping-pong terminates (M1).

The failure mode being pinned: pr_review FAIL_BACK(agent_error) routes to
implementation, whose GATE sees the existing PR and ADVANCEs straight back
to pr_review. Without the M1 bound no budget ever moves, so the cycle spins
forever. The bound: pr_review flags its agent_error fail-backs
(``payload["agent_error_failback"]``), and the implementation GATE consumes
the ``implement`` budget when it re-adopts the PR on such a re-entry;
exhaustion terminates with ``agent_error_exhausted``. No labels are ever
written along the dead cycle.
"""

from __future__ import annotations

from typing import Any

from hephaestus.automation.pipeline.jobs import JobResult
from hephaestus.automation.pipeline.routing import ROUTES, Disposition, StageName
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.implementation import ImplementationStage
from hephaestus.automation.pipeline.stages.pr_review import PrReviewStage
from hephaestus.automation.state_labels import STATE_PLAN_GO
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

# Sanity anchors: the reasons this composition exercises are ROUTES rows.
assert ROUTES[StageName.PR_REVIEW].fail_routes["agent_error"] == StageName.IMPLEMENTATION
assert ROUTES[StageName.IMPLEMENTATION].next == StageName.PR_REVIEW

_LABEL_MUTATIONS = {
    "gh_issue_add_labels",
    "gh_issue_remove_labels",
    "mark_pr_implementation_go",
    "mark_pr_implementation_no_go",
    "arm_auto_merge",
}


def _drive(stage: Any, item: Any, ctx: Any, pool: FakeWorkerPool, max_steps: int = 60) -> Any:
    """Drive one stage pass to its outcome, mirroring the coordinator loop."""
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
            stage.on_job_done(item, job_result, ctx)
            item.state = result.on_done_state
            continue
        return result
    raise AssertionError("stage driver did not terminate")


def _drive_pr_review_to_agent_error(
    pr_stage: PrReviewStage, item: Any, ctx: Any, max_passes: int = 10
) -> StageOutcome:
    """Run pr_review passes with a failing reviewer until it fails back.

    Each pass's review job fails (no verdict), so EVAL RETRYs until the
    consecutive-failure cap, then fails back ``agent_error``. RETRY passes
    re-enter the stage (coordinator semantics: same stage, fresh pass).
    """
    for _ in range(max_passes):
        item.state = "ENTER"
        pool = FakeWorkerPool()
        pool.script(JobResult(ok=False, error="reviewer crashed"))
        outcome = _drive(pr_stage, item, ctx, pool)
        assert isinstance(outcome, StageOutcome)
        if outcome.disposition == Disposition.RETRY:
            continue
        return outcome
    raise AssertionError("pr_review never escalated past RETRY")


class TestAgentErrorPingPongTerminates:
    """pr_review agent_error -> implementation -> pr_review twice: bounded."""

    def test_composition_terminates_at_the_implement_budget(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Two fail-back round trips exhaust implement=2; no label writes.

        Trip 1: pr_review reviewer-error cap -> FAIL_BACK(agent_error) ->
        implementation GATE re-adopts the PR, consuming implement (1/2) ->
        ADVANCE back to pr_review (fresh cycle, error streak reset).
        Trip 2: same fail-back -> GATE consumption hits the budget (2/2) ->
        FINISH_FAIL(agent_error_exhausted). Nothing ever labels the PR or
        the issue along the way.
        """
        impl_stage = ImplementationStage()
        pr_stage = PrReviewStage()
        github = FakeStageGitHub(
            labels=[STATE_PLAN_GO], open_pr=1001, pr_head_branch="1-real-branch"
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state="ENTER")
        item.branch = "1-real-branch"

        # Trip 1: pr_review exhausts its reviewer-error retries.
        outcome = _drive_pr_review_to_agent_error(pr_stage, item, ctx)
        assert outcome.disposition == Disposition.FAIL_BACK
        assert outcome.note == "agent_error"
        assert item.payload["agent_error_failback"] is True

        # ROUTES: agent_error -> implementation. GATE re-adopts, consuming
        # the implement budget; the adopted worktree leg runs and ADVANCEs.
        item.state = "ENTER"
        implementation_pool = FakeWorkerPool()
        implementation_pool.queue_result(
            JobResult(ok=True, value={"path": "/tmp/adopted-pr", "dirty": False})
        )
        outcome = _drive(impl_stage, item, ctx, implementation_pool)
        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.ADVANCE
        assert item.attempts["implement"] == 1  # the bound moved

        # Trip 2: pr_review gets a FRESH error budget (new cycle)...
        outcome = _drive_pr_review_to_agent_error(pr_stage, item, ctx)
        assert outcome.disposition == Disposition.FAIL_BACK
        assert outcome.note == "agent_error"

        # ...and the second GATE re-adoption exhausts the implement budget.
        item.state = "ENTER"
        outcome = _drive(impl_stage, item, ctx, FakeWorkerPool())
        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.FINISH_FAIL
        assert outcome.note == "agent_error_exhausted"
        assert item.attempts["implement"] == 2  # terminated AT the budget

        # No label writes anywhere along the dead cycle.
        label_writes = [name for name, _ in github.mutation_log if name in _LABEL_MUTATIONS]
        assert label_writes == []

    def test_address_error_path_is_bounded_the_same_way(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The address-failure fail-back consumes the same GATE budget."""
        impl_stage = ImplementationStage()
        pr_stage = PrReviewStage()
        github = FakeStageGitHub(
            labels=[STATE_PLAN_GO], open_pr=1001, pr_head_branch="1-real-branch"
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=2, pr=1001, state="EVAL")
        item.branch = "1-real-branch"
        item.attempts["implement"] = 1  # one prior trip already consumed

        assert pr_stage.on_enter(item, ctx) is None
        item.state = "EVAL"
        item.payload["address_error"] = True
        outcome = pr_stage.step(item, ctx)
        assert isinstance(outcome, StageOutcome)
        assert outcome.note == "agent_error"

        item.state = "ENTER"
        final = _drive(impl_stage, item, ctx, FakeWorkerPool())
        assert isinstance(final, StageOutcome)
        assert final.disposition == Disposition.FINISH_FAIL
        assert final.note == "agent_error_exhausted"
        label_writes = [name for name, _ in github.mutation_log if name in _LABEL_MUTATIONS]
        assert label_writes == []
