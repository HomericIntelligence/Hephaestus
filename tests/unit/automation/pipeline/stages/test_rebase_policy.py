"""Tests for the allowed rebase triggers."""

from typing import Any

import pytest

from hephaestus.automation.pipeline.jobs import AgentJob, GitJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.implementation import ImplementationStage
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


@pytest.mark.parametrize(
    ("payload", "next_state", "reason"),
    [
        ({"existing_pr": True}, "ADOPTED", None),
        ({"existing_pr": True, "implementation_remediation": True}, "IMPLEMENT_WAIT", None),
        ({}, "REBASE_WAIT", "implementation_start"),
        ({"implementation_started": True}, "ADVISE_WAIT", None),
        ({"existing_pr": True, "manual_rebase_required": True}, "REBASE_WAIT", "manual"),
    ],
)
def test_only_initial_or_manual_work_prepares_a_rebase(
    make_ctx: Any,
    make_work_item: Any,
    payload: dict[str, Any],
    next_state: str,
    reason: str | None,
) -> None:
    """PR adoption and corrections keep their current base."""
    item = make_work_item(issue=1, state="DIRTY_DECISION_WAIT")
    item.payload.update(payload)
    result = ImplementationStage().step(item, make_ctx())
    assert result == Continue(next_state=next_state)
    assert item.payload.get("rebase_reason") == reason


def test_initial_rebase_has_no_pr_publication(make_ctx: Any, make_work_item: Any) -> None:
    """The first implementation rebases before it changes source files."""
    item = make_work_item(issue=1, state="REBASE_WAIT")
    item.payload.update(rebase_reason="implementation_start", _impl_source_revision="a" * 40)
    request = ImplementationStage().step(item, make_ctx())
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, GitJob)
    assert request.job.kwargs["rebase_reason"] == "implementation_start"
    assert request.job.kwargs["expected_head_sha"] == "a" * 40
    assert request.job.kwargs["publish_rebased_head"] is False


def test_no_reason_cannot_schedule_rebase(make_ctx: Any, make_work_item: Any) -> None:
    """An old stage state does not grant rebase permission."""
    item = make_work_item(issue=1, pr=1001, state="REBASE_WAIT")
    assert ImplementationStage().step(item, make_ctx()) == StageOutcome(
        Disposition.FINISH_FAIL, "rebase_reason_unavailable"
    )


def test_review_conflict_head_drift_requires_review(make_ctx: Any, make_work_item: Any) -> None:
    """A changed head cannot use an earlier review to start a rebase."""
    item = make_work_item(issue=1, pr=1001, state="REBASE_WAIT")
    item.payload.update(rebase_reason="review_conflict", reviewed_pr_head_sha="b" * 40)
    github = FakeStageGitHub(pr_impl_state=(True, False))
    result = ImplementationStage().step(item, make_ctx(github=github))
    assert result == Continue(next_state="ADOPTED")
    assert "reviewed_pr_head_sha" not in item.payload


def test_initial_rebase_returns_to_first_implementation(make_ctx: Any, make_work_item: Any) -> None:
    """A new issue continues to implementation after the host rebase."""
    item = make_work_item(issue=1, state="REBASE_WAIT")
    item.payload.update(rebase_reason="implementation_start", rebase_complete=True)
    assert ImplementationStage().step(item, make_ctx()) == Continue(next_state="ADVISE_WAIT")
    assert item.payload["implementation_started"] is True


def test_manual_conflict_restarts_from_captured_base(make_ctx: Any, make_work_item: Any) -> None:
    """The manual conflict retry uses the base from the aborted attempt."""
    stage = ImplementationStage()
    item = make_work_item(issue=1, pr=1001, state="REBASE_WAIT")
    item.payload["rebase_reason"] = "manual"
    stage.on_job_done(
        item,
        JobResult(
            ok=False,
            error="rebase conflict restart required",
            value={
                "rebase_restart_required": True,
                "base_sha": "b" * 40,
                "head_sha": "a" * 40,
            },
        ),
        make_ctx(),
    )
    request = stage.step(item, make_ctx())
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, AgentJob)
    item.state = request.on_done_state
    stage.on_job_done(item, JobResult(ok=True), make_ctx())
    assert stage.step(item, make_ctx()) == Continue(next_state="REBASE_WAIT")
    item.state = "REBASE_WAIT"
    request = stage.step(item, make_ctx())
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, GitJob)
    assert request.job.kwargs["resolve_conflicts"] is True
    assert request.job.kwargs["expected_base_sha"] == "b" * 40


def test_reviewed_conflict_starts_an_agent_before_rebase(
    make_ctx: Any, make_work_item: Any
) -> None:
    """An admitted conflict starts an agent even if the later replay is clean."""
    github = FakeStageGitHub(
        pr_impl_state=(True, False),
        pr_state={
            "state": "OPEN",
            "autoMergeRequest": None,
            "headRefOid": "a" * 40,
            "baseRefName": "main",
            "mergeable": "CONFLICTING",
            "mergeStateStatus": "DIRTY",
        },
    )
    item = make_work_item(issue=1, pr=1001, state="REBASE_WAIT")
    item.payload.update(rebase_reason="review_conflict", reviewed_pr_head_sha="a" * 40)
    request = ImplementationStage().step(item, make_ctx(github=github))
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, AgentJob)
    assert request.job.descr == "prepare_conflict_rebase"


def test_manual_new_issue_returns_through_implementation_gate(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Manual preparation does not replace normal implementation admission."""
    item = make_work_item(issue=1, state="REBASE_WAIT")
    item.payload.update(
        rebase_reason="manual",
        rebase_complete=True,
        manual_rebase_required=True,
        manual_rebase_resume_stage="implementation",
    )
    assert ImplementationStage().step(item, make_ctx()) == Continue(next_state="GATE")
    assert "manual_rebase_required" not in item.payload


def test_manual_request_does_not_claim_dirty_continuation(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A manual request cannot take the direct implementation shortcut."""
    from types import SimpleNamespace

    item = make_work_item(issue=1, state="WORKTREE_WAIT")
    item.payload["manual_rebase_required"] = True
    ctx = make_ctx(paths=SimpleNamespace(repo_root="/tmp/repo", source_workspaces=object()))
    request = ImplementationStage().step(item, ctx)
    assert isinstance(request, JobRequest)
    assert request.job.op == "create_worktree"


def test_dirty_initial_source_waits_before_implementation(
    make_ctx: Any, make_work_item: Any
) -> None:
    """The initial rebase cannot start with uncommitted source changes."""
    item = make_work_item(issue=1, state="DIRTY_DIRECT_CLAIM_WAIT")
    item.payload["dirty_direct_claim_result"] = {"ok": True, "value": {}}
    assert ImplementationStage().step(item, make_ctx()) == StageOutcome(
        Disposition.BLOCKED, "initial_rebase_requires_clean_worktree"
    )


def test_worker_admission_change_requires_new_review(make_ctx: Any, make_work_item: Any) -> None:
    """A changed live admission cannot keep the previous review proof."""
    stage = ImplementationStage()
    item = make_work_item(issue=1, pr=1001, state="REBASE_WAIT")
    item.payload.update(rebase_reason="review_conflict", reviewed_pr_head_sha="a" * 40)
    stage.on_job_done(
        item,
        JobResult(ok=False, value={"rebase_admission_changed": True}, error="conflict changed"),
        make_ctx(),
    )
    assert stage.step(item, make_ctx()) == Continue(next_state="ADOPTED")
    assert "reviewed_pr_head_sha" not in item.payload


@pytest.mark.parametrize(
    "manual,started,record", [(False, False, True), (True, False, False), (False, True, False)]
)
def test_creation_proof_requested_only_for_initial_implementation(
    make_ctx: Any, make_work_item: Any, manual: bool, started: bool, record: bool
) -> None:
    """Only a first automatic start can request new-branch proof."""
    item = make_work_item(issue=1, state="WORKTREE_WAIT")
    item.payload.update(
        manual_rebase_required=manual, implementation_started=started, dirty_direct_checked=True
    )
    request = ImplementationStage().step(item, make_ctx())
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, GitJob)
    assert request.job.kwargs.get("record_initial_creation", False) is record


@pytest.mark.parametrize("started", [False, True])
def test_dirty_restart_requires_host_first_start_evidence(
    make_ctx: Any, make_work_item: Any, tmp_path: Any, started: bool
) -> None:
    """Resume a dirty writer only after the host confirms its first start."""
    from dataclasses import asdict, replace

    from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
    from hephaestus.automation.pipeline.jobs import DirtyDirectPlanInput
    from tests.unit.agents.test_dirty_workspace import _claim

    item = make_work_item(state="DIRTY_DIRECT_CLAIM_WAIT", issue=12)
    claim = _claim()
    binding = replace(
        WorkspaceBinding.source(
            cwd=tmp_path / "writer",
            reusable_root=tmp_path,
            repository=item.repo,
            ownership_key="test",
            item_number=12,
            lane=SourceLane.IMPLEMENTATION,
            revision=claim.reservation_base_sha,
            generation=2,
            detached=False,
        ),
        schema_version=2,
        dirty_claim=claim,
    )
    item.payload["dirty_direct_claim_result"] = {
        "ok": True,
        "value": {
            "implementation_started": started,
            "source_workspace": binding.to_dict(),
            "dirty_plan": asdict(
                DirtyDirectPlanInput(
                    5, "## Files to Modify\n- `tracked.txt`", 5, "state:plan-go", ("tracked.txt",)
                )
            ),
            "direct_scope_reservation": {"branch": claim.branch},
        },
    }
    result = ImplementationStage().step(item, make_ctx())
    if started:
        assert result == Continue(next_state="IMPLEMENT_WAIT")
        assert item.payload["implementation_started"] is True
    else:
        assert result == StageOutcome(Disposition.BLOCKED, "initial_rebase_requires_clean_worktree")


@pytest.mark.parametrize("valid", [False, True])
def test_initial_reservation_moves_before_implementation(
    make_ctx: Any, make_work_item: Any, valid: bool
) -> None:
    """Use the new reserved head only after a complete host receipt."""
    stage = ImplementationStage()
    item = make_work_item(issue=1, state="REBASE_WAIT")
    reservation = {"branch": item.branch, "base_sha": "a" * 40}
    item.payload.update(
        rebase_reason="implementation_start",
        _impl_source_revision="a" * 40,
        _direct_scope_reservation=reservation,
    )
    request = stage.step(item, make_ctx())
    assert isinstance(request, JobRequest)
    assert request.job.kwargs["direct_scope_reservation"] == reservation
    returned = {"branch": item.branch, "base_sha": "b" * 40 if valid else "c" * 40}
    stage.on_job_done(
        item,
        JobResult(
            ok=True,
            value={
                "head_sha": "b" * 40,
                "published": True,
                "implementation_started": True,
                "direct_scope_reservation": returned,
            },
        ),
        make_ctx(),
    )
    outcome = stage.step(item, make_ctx())
    if valid:
        assert outcome == Continue(next_state="ADVISE_WAIT")
        assert item.payload["_direct_scope_reservation"] == returned
        assert "_post_remediation_review_head_sha" not in item.payload
    else:
        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition is Disposition.FINISH_FAIL
        assert item.payload["_direct_scope_reservation"] == reservation


def test_reviewed_conflict_that_clears_retains_review(make_ctx: Any, make_work_item: Any) -> None:
    """A PR that has no conflict keeps its completed review."""
    item = make_work_item(issue=1, pr=1001, state="REBASE_WAIT")
    item.payload.update(
        rebase_reason="review_conflict",
        reviewed_pr_head_sha="a" * 40,
        reviewed_pr_node_id="PR_node",
        review_verdict="GO",
    )
    github = FakeStageGitHub(
        pr_impl_state=(True, False),
        pr_state={
            "state": "OPEN",
            "autoMergeRequest": None,
            "headRefOid": "a" * 40,
            "baseRefName": "main",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        },
    )
    result = ImplementationStage().step(item, make_ctx(github=github))
    assert result == StageOutcome(Disposition.FAIL_BACK, "review_retained_after_rebase")
    assert item.payload["reviewed_pr_head_sha"] == "a" * 40
    assert item.payload["reviewed_pr_node_id"] == "PR_node"
    assert item.payload["review_verdict"] == "GO"


def test_host_rebase_receipt_retains_the_original_review(
    make_ctx: Any, make_work_item: Any
) -> None:
    """The resulting head goes to merge checks with the initial review."""
    from hephaestus.automation.pipeline.rebase_review import RebaseReviewProof
    from hephaestus.automation.review_audit import ReviewAudit

    audit = ReviewAudit("A", "Checks passed.", (), "", True, "GO")
    proof = RebaseReviewProof(
        repository="test-org/test-repo",
        issue_number=1,
        pr_number=1001,
        reviewed_head_sha="a" * 40,
        reviewed_base_sha="b" * 40,
        source_head_sha="a" * 40,
        target_base_sha="c" * 40,
        resulting_head_sha="d" * 40,
        resulting_tree_sha="e" * 40,
        original_audit_id=f"<!-- hephaestus-implementation-go-audit:pr=1001:head={'a' * 40} -->",
    )
    item = make_work_item(repo="test-repo", issue=1, pr=1001, state="REBASE_WAIT")
    item.payload.update(
        rebase_reason="review_conflict",
        reviewed_pr_head_sha="a" * 40,
        reviewed_pr_base_sha="b" * 40,
        review_audit=audit,
    )
    github = FakeStageGitHub(pr_impl_state=(True, False))
    ctx = make_ctx(github=github, org="test-org")
    stage = ImplementationStage()
    stage.on_job_done(
        item,
        JobResult(
            ok=True,
            value={
                "published": True,
                "head_sha": "d" * 40,
                "retained_rebase_review_proof": proof,
            },
        ),
        ctx,
    )
    result = stage.step(item, ctx)
    assert result == StageOutcome(Disposition.FAIL_BACK, "review_retained_after_rebase")
    assert item.payload["reviewed_pr_head_sha"] == "a" * 40
    assert item.payload["review_audit"] is audit
    assert item.payload["retained_rebase_review_proof"] is proof
    assert not any(name == "publish_review_rebase_record" for name, _ in github.mutation_log)


def test_host_noop_rebase_keeps_the_review(make_ctx: Any, make_work_item: Any) -> None:
    """An unchanged published head does not need another review."""
    from hephaestus.automation.review_audit import ReviewAudit

    item = make_work_item(issue=1, pr=1001, state="REBASE_WAIT")
    item.payload.update(
        rebase_reason="review_conflict",
        reviewed_pr_head_sha="a" * 40,
        review_audit=ReviewAudit("A", "Checks passed.", (), "", True, "GO"),
    )
    stage = ImplementationStage()
    stage.on_job_done(
        item,
        JobResult(
            ok=True,
            value={
                "rebased": False,
                "published": False,
                "head_sha": "a" * 40,
            },
        ),
        make_ctx(),
    )
    assert stage.step(item, make_ctx()) == StageOutcome(
        Disposition.FAIL_BACK, "review_retained_after_rebase"
    )
    assert item.payload["reviewed_pr_head_sha"] == "a" * 40
