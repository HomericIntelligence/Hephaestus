"""Keep the original review when a manual rebase follows a restart."""

from dataclasses import replace
from typing import Any

import pytest

from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.jobs import GitJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.seeding import IssueFacts, seed_entry_from_facts
from hephaestus.automation.pipeline.stages import JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.implementation import REBASE_WAIT, ImplementationStage
from hephaestus.automation.state_labels import STATE_PLAN_GO
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub
from tests.unit.automation.pipeline.stages.test_rebase_review_recovery import _proof, _record


@pytest.mark.parametrize("failure", [None, "host_error", "wrong_head", "record_changed"])
def test_manual_restart_restores_review_before_rebase(make_ctx: Any, failure: str | None) -> None:
    """Restore the original review or stop before the next rebase job."""
    record = _record()
    entry = seed_entry_from_facts(
        IssueFacts(
            number=1,
            title="A task",
            is_epic=False,
            labels={STATE_PLAN_GO},
            pr_number=12,
            pr_is_open=True,
            pr_is_merged=False,
            pending_review_rebase_record=record,
        )
    )
    assert entry.stage is StageName.MERGE_WAIT
    github = FakeStageGitHub(
        pr_state={
            "state": "OPEN",
            "headRefOid": record.resulting_head_sha,
            "autoMergeRequest": None,
            "baseRefName": "main",
        }
    )
    ctx = make_ctx(github=github, config_overrides={"rebase": True})
    coordinator = Coordinator(
        ctx.config, github=github, pool=FakeWorkerPool(), install_signals=False
    )
    item = coordinator._prepare_direct_item(entry, "test-repo", "f" * 40)
    assert item.stage is StageName.IMPLEMENTATION
    assert item.payload["manual_rebase_resume_stage"] == "merge_wait"
    assert "reviewed_pr_head_sha" not in item.payload
    item.state = REBASE_WAIT
    item.payload["rebase_reason"] = "manual"
    item.worktree = "/tmp/repo-writer"
    item.branch = "1-task"
    stage = ImplementationStage()
    request = stage.step(item, ctx)
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, GitJob)
    assert request.job.op == "verify_rebase_review"
    assert request.on_done_state == REBASE_WAIT
    proof = _proof()
    if failure == "wrong_head":
        proof = replace(proof, resulting_head_sha="f" * 40)
    if failure == "record_changed":
        item.payload["pending_review_rebase_record"] = replace(record, state="revoked")
    stage.on_job_done(item, JobResult(ok=failure != "host_error", value=proof), ctx)
    item.state = request.on_done_state
    result = stage.step(item, ctx)
    if failure:
        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.FINISH_FAIL
        assert result.note == "rebase_review_recovery_failed"
        assert "retained_rebase_review_proof" not in item.payload
    else:
        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob)
        assert result.job.op == "rebase"
        assert result.job.kwargs["reviewed_head_sha"] == record.reviewed_head_sha
        assert result.job.kwargs["reviewed_base_sha"] == record.reviewed_base_sha
        assert result.job.kwargs["review_audit"] == record.audit
        assert result.job.kwargs["expected_remote_sha"] == record.resulting_head_sha
    assert not github.mutation_log
