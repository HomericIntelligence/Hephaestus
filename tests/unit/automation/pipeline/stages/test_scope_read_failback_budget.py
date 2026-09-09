"""Keep the review failure budget across a queued plan scope read."""

from typing import Any

from hephaestus.automation.pipeline.github_jobs import (
    CurrentPlanScopeRead,
    GitHubJob,
    ReadCurrentPlanScopeRequest,
)
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.implementation import ImplementationStage
from hephaestus.automation.pipeline.stages.pr_review import PrReviewStage
from hephaestus.automation.state_labels import STATE_PLAN_GO
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def test_codex_review_failback_charges_budget_after_scope_read(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A first review failure must exhaust a budget of one after scope recovery."""
    github = FakeStageGitHub(labels=[STATE_PLAN_GO], open_pr=1001, pr_head_branch="1-real")
    ctx = make_ctx(
        github=github,
        config_overrides={"agent": "codex"},
        budget_fn=lambda _name: 1,
    )
    item = make_work_item(issue=1, pr=1001, stage=StageName.PR_REVIEW)
    assert PrReviewStage._fail_back_agent_error(item) == StageOutcome(
        Disposition.FAIL_BACK, "agent_error"
    )
    item.stage = StageName.IMPLEMENTATION
    item.state = "GATE"
    stage = ImplementationStage()

    request = stage.step(item, ctx)

    assert isinstance(request, JobRequest)
    assert isinstance(request.job, GitHubJob)
    assert isinstance(request.job.request, ReadCurrentPlanScopeRequest)
    assert request.on_done_state == "GATE"
    stage.on_job_done(
        item,
        JobResult(
            ok=True,
            value=CurrentPlanScopeRead(request.job.request, ("module.py",), "b" * 64),
        ),
        ctx,
    )
    item.state = request.on_done_state

    outcome = stage.step(item, ctx)

    assert outcome == StageOutcome(Disposition.FINISH_FAIL, "agent_error_exhausted")
    assert item.attempts["implement"] == 1
    assert "agent_error_failback" not in item.payload
    assert github.mutation_log == []
