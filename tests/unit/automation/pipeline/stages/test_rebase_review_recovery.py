"""Merge recovery must not repeat review for invalid retained evidence."""

from dataclasses import replace
from typing import Any

import pytest

from hephaestus.automation.pipeline.github_jobs import GitHubJob, RunMergeWaitCycleRequest
from hephaestus.automation.pipeline.jobs import GitJob, JobResult
from hephaestus.automation.pipeline.rebase_review import RebaseReviewProof
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.merge_wait import MergeWaitStage
from hephaestus.automation.rebase_review_receipt import (
    RebaseReviewRecord,
    original_audit_identity,
)
from hephaestus.automation.review_audit import ReviewAudit


def _record() -> RebaseReviewRecord:
    return RebaseReviewRecord(
        repository="test-org/test-repo",
        issue_number=1,
        pr_number=12,
        reviewed_head_sha="a" * 40,
        reviewed_base_sha="b" * 40,
        source_head_sha="a" * 40,
        target_base_sha="c" * 40,
        resulting_head_sha="d" * 40,
        resulting_tree_sha="e" * 40,
        original_audit_id=original_audit_identity(12, "a" * 40),
        audit=ReviewAudit(
            grade="A",
            summary="No findings.",
            findings=(),
            raw_feedback="",
            valid=True,
            verdict="GO",
        ),
    )


def _proof() -> RebaseReviewProof:
    return RebaseReviewProof(
        repository="test-org/test-repo",
        issue_number=1,
        pr_number=12,
        reviewed_head_sha="a" * 40,
        reviewed_base_sha="b" * 40,
        source_head_sha="a" * 40,
        target_base_sha="c" * 40,
        resulting_head_sha="d" * 40,
        resulting_tree_sha="e" * 40,
        original_audit_id=original_audit_identity(12, "a" * 40),
    )


def test_invalid_retained_record_stops_without_another_review(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Invalid restart evidence stops the merge path without another review."""
    item = make_work_item(
        stage=StageName.MERGE_WAIT,
        state="MERGE",
        pr=12,
        payload={"pending_review_rebase_record": {"head": "unverified"}},
    )

    result = MergeWaitStage().step(item, make_ctx())

    assert isinstance(result, StageOutcome)
    assert result.disposition is Disposition.FINISH_FAIL
    assert result.note == "rebase_review_recovery_invalid"


def test_restart_verifies_retained_record_before_merge_checks(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A restart keeps the old review and checks the resulting merge head."""
    record = _record()
    item = make_work_item(
        stage=StageName.MERGE_WAIT,
        state="MERGE",
        pr=12,
        payload={
            "pending_review_rebase_record": record,
            "merge_queue_admitted_head_sha": "a" * 40,
            "merge_queue_admitted_proof_generation": 1,
            "merge_readiness_deadline_s": 1.0,
        },
    )
    stage, ctx = MergeWaitStage(), make_ctx()
    request = stage.step(item, ctx)
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, GitJob)
    assert request.job.op == "verify_rebase_review"
    assert request.job.kwargs["record"] is record
    assert "reviewed_pr_head_sha" not in item.payload

    stage.on_job_done(item, JobResult(ok=True, value=_proof()), ctx)
    item.state = request.on_done_state
    checks = stage.step(item, ctx)

    assert isinstance(checks, JobRequest)
    assert isinstance(checks.job, GitHubJob)
    assert isinstance(checks.job.request, RunMergeWaitCycleRequest)
    assert checks.job.request.reviewed_head_sha == "a" * 40
    assert checks.job.request.merge_head_sha == "d" * 40
    assert not checks.job.request.queue_admitted
    assert item.payload["review_audit"] == record.audit
    assert item.payload["reviewed_pr_head_sha"] == record.reviewed_head_sha
    assert item.payload["reviewed_pr_base_sha"] == record.reviewed_base_sha
    assert "merge_readiness_deadline_s" not in item.payload


@pytest.mark.parametrize("failure", ["host_error", "wrong_head", "record_changed"])
def test_failed_recovery_never_submits_merge_or_another_review(
    make_ctx: Any, make_work_item: Any, failure: str
) -> None:
    """A failed or mismatched host result cannot restore review authority."""
    item = make_work_item(
        stage=StageName.MERGE_WAIT,
        state="MERGE",
        pr=12,
        payload={"pending_review_rebase_record": _record()},
    )
    stage, ctx = MergeWaitStage(), make_ctx()
    request = stage.step(item, ctx)
    assert isinstance(request, JobRequest)
    proof = _proof()
    if failure == "wrong_head":
        proof = replace(proof, resulting_head_sha="f" * 40)
    if failure == "record_changed":
        item.payload["pending_review_rebase_record"] = replace(_record(), state="revoked")
    stage.on_job_done(item, JobResult(ok=failure != "host_error", value=proof), ctx)
    item.state = request.on_done_state
    result = stage.step(item, ctx)

    assert isinstance(result, StageOutcome)
    assert result.disposition is Disposition.FINISH_FAIL
    assert result.note == "rebase_review_recovery_failed"
    assert "retained_rebase_review_proof" not in item.payload


@pytest.mark.parametrize("field", ["repository", "issue_number", "pr_number", "state"])
def test_foreign_or_revoked_recovery_record_is_rejected(
    make_ctx: Any, make_work_item: Any, field: str
) -> None:
    """Recovery is restricted to the active repository, issue, and PR."""
    changes: dict[str, Any] = {
        "repository": "other/repo",
        "issue_number": 2,
        "pr_number": 13,
        "state": "revoked",
    }
    change = {field: changes[field]}
    if field == "pr_number":
        change["original_audit_id"] = original_audit_identity(13, "a" * 40)
    item = make_work_item(
        stage=StageName.MERGE_WAIT,
        state="MERGE",
        pr=12,
        payload={"pending_review_rebase_record": replace(_record(), **change)},
    )
    result = MergeWaitStage().step(item, make_ctx())
    assert isinstance(result, StageOutcome)
    assert result.disposition is Disposition.FINISH_FAIL
    assert result.note == "rebase_review_recovery_invalid"
