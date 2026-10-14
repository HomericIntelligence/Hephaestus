"""Read current plan scope after pending reply work is complete."""

from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.automation.pipeline.github_jobs import (
    CurrentPlanScopeRead,
    DeliverReplyHandoffRequest,
    GitHubJob,
    ReadCurrentPlanScopeRequest,
    RecoverRemediationReplyJournalRequest,
    RemediationReplyJournalRecovered,
    ReplyHandoffAttempted,
)
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.jobs import AgentJob
from hephaestus.automation.pipeline.reply_handoff import PENDING_IMPLEMENTATION_REPLY_HANDOFF
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.implementation import ImplementationStage
from tests.unit.automation.pipeline.stages.test_stage_implementation import (
    _prepared_writer,
    _remediation_commit_receipt,
)


def _remediation_item(make_work_item: Any, *, state: str) -> Any:
    """Start with a prepared writer and one unresolved review thread."""
    item = make_work_item(issue=1, pr=1001, stage=StageName.IMPLEMENTATION, state=state)
    item.payload.update(
        implementation_remediation=True,
        remediation_threads=[{"id": "thread-1", "body": "Correct the check."}],
        remediation_thread_snapshots=[
            {
                "id": "thread-1",
                "isResolved": False,
                "path": "module.py",
                "line": 3,
                "side": "RIGHT",
                "comments": [
                    {"id": "comment-1", "author": "reviewer", "body": "Correct the check."}
                ],
            }
        ],
    )
    _prepared_writer(item)
    return item


def test_pending_reply_completes_before_any_plan_scope_read(
    make_ctx: Any, make_work_item: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending reply can complete when a current plan read is unavailable."""
    item = _remediation_item(make_work_item, state="IMPLEMENT_WAIT")
    item.payload["remediation_output"] = {
        "addressed": ["thread-1"],
        "replies": {"thread-1": "[Response] Corrected the check."},
    }
    handoff = _remediation_commit_receipt(item)["remediation_handoff"]
    item.payload[PENDING_IMPLEMENTATION_REPLY_HANDOFF] = handoff
    ctx = make_ctx()
    live = Mock(side_effect=RuntimeError("current plan read is unavailable"))
    monkeypatch.setattr(ctx.github, "discover_plan", live)
    stage = ImplementationStage()

    route = stage.step(item, ctx)

    assert isinstance(route, Continue)
    assert route == Continue(next_state="PR_CREATE")
    item.state = route.next_state
    delivery_route = stage.step(item, ctx)
    assert isinstance(delivery_route, Continue)
    assert delivery_route == Continue(next_state="REPLY_HANDOFF_WAIT")
    item.state = delivery_route.next_state
    request = stage.step(item, ctx)
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, GitHubJob)
    assert isinstance(request.job.request, DeliverReplyHandoffRequest)
    stage.on_job_done(
        item,
        JobResult(
            ok=True,
            value=ReplyHandoffAttempted(request.job.request, "completed", None, 0, None),
        ),
        ctx,
    )
    item.state = request.on_done_state

    outcome = stage.step(item, ctx)

    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.ADVANCE
    assert PENDING_IMPLEMENTATION_REPLY_HANDOFF not in item.payload
    assert "_pending_github_request" not in item.payload
    live.assert_not_called()


def test_journal_retry_completes_before_scope_read_and_provider_dispatch(
    make_ctx: Any, make_work_item: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A journal retry keeps its deadline and obtains scope before the provider."""
    item = _remediation_item(make_work_item, state="REPLY_JOURNAL_RECOVERY_WAIT")
    ctx = make_ctx()
    live = Mock(side_effect=RuntimeError("coordinator must not read the current plan"))
    monkeypatch.setattr(ctx.github, "discover_plan", live)
    stage = ImplementationStage()
    journal = stage.step(item, ctx)
    assert isinstance(journal, JobRequest)
    assert isinstance(journal.job, GitHubJob)
    assert isinstance(journal.job.request, RecoverRemediationReplyJournalRequest)
    stage.on_job_done(
        item,
        JobResult(
            ok=False,
            error="github_rate_limit",
            value={"failure_kind": "github_rate_limit", "retry_delay_s": 45.0},
        ),
        ctx,
    )
    item.state = journal.on_done_state

    outcome = stage.step(item, ctx)

    assert outcome == StageOutcome(Disposition.RETRY, "implementation_reply_handoff_journal_read")
    assert item.payload.pop("retry_delay_s") == 45.0
    assert item.state == "REPLY_JOURNAL_RECOVERY_WAIT"
    retry = stage.step(item, ctx)
    assert isinstance(retry, JobRequest)
    assert isinstance(retry.job, GitHubJob)
    assert retry.job.request == journal.job.request
    stage.on_job_done(
        item,
        JobResult(
            ok=True,
            value=RemediationReplyJournalRecovered(journal.job.request, None),
        ),
        ctx,
    )
    item.state = retry.on_done_state

    scope = stage.step(item, ctx)

    assert isinstance(scope, JobRequest)
    assert isinstance(scope.job, GitHubJob)
    assert isinstance(scope.job.request, ReadCurrentPlanScopeRequest)
    stage.on_job_done(
        item,
        JobResult(
            ok=True,
            value=CurrentPlanScopeRead(scope.job.request, ("current.py",), "b" * 64),
        ),
        ctx,
    )
    item.state = scope.on_done_state

    provider = stage.step(item, ctx)

    assert isinstance(provider, JobRequest)
    assert isinstance(provider.job, AgentJob)
    assert provider.on_done_state == "TEST_WAIT"
    inputs = provider.job.remediation_pretest_input
    assert inputs is not None
    assert inputs.allowed_paths == ("current.py",)
    assert inputs.approved_scope_sha256 == "b" * 64
    assert "_pending_github_request" not in item.payload
    assert "_reply_journal_recovery_complete" not in item.payload
    live.assert_not_called()
