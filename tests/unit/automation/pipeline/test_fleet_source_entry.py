"""Admit the exact existing source through normal publication discovery."""

from __future__ import annotations

import time
from dataclasses import replace
from unittest.mock import Mock, patch

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.first_publication_recovery import FirstPublicationStore
from hephaestus.automation.pipeline.git_jobs import FirstPublicationRecord, GitJob
from hephaestus.automation.pipeline.github_jobs import GitHubJob, ReadCurrentPlanScopeRequest
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.jobs import AgentJob, JobHandle
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner
from hephaestus.automation.rebase_recovery import PendingRebaseStore
from hephaestus.automation.source_worktree import _PreparationDeadline
from hephaestus.utils.file_lock import LockUnavailableError, file_lock
from tests.unit.automation.pipeline.test_fleet_execution import _fleet_coordinator, _git


def _discovery(attempt):
    """Use the existing read job with the caller's exact source binding."""
    return GitJob(
        repo="Athena",
        op="discover_first_publication",
        workspace=attempt.workspace,
        expected_repository=attempt.repository,
        timeout_s=30,
        deadline_s=time.monotonic() + 30,
        kwargs={
            "repo_root": str(attempt.workspace.reusable_root),
            "issue_number": 265,
            "branch": attempt.source_receipt.branch,
            "publication_discovery_request_id": "f" * 32,
        },
        descr="discover_first_publication",
    )


@pytest.mark.parametrize("source_repository", ["Athena", "HomericIntelligence/Athena"])
def test_bound_discovery_accepts_only_the_clean_admitted_source_under_its_lease(
    tmp_path, monkeypatch, source_repository
):
    """The existing source owner survives without fabricated start or reservation records."""
    coordinator, executor, manager, binding, loop = _fleet_coordinator(
        tmp_path, source_repository=source_repository
    )
    receipt = manager._read_receipt(265, SourceLane.IMPLEMENTATION)
    candidate = FirstPublicationStore.candidate
    observations = []

    def read_candidate(store, issue):
        with (
            pytest.raises(LockUnavailableError),
            file_lock(
                manager._lane_lock_path(issue, SourceLane.IMPLEMENTATION),
                require_exclusive=True,
                blocking=False,
            ),
        ):
            pytest.fail("discovery released the admitted source lease")
        observations.append(issue)
        return candidate(store, issue)

    monkeypatch.setattr(FirstPublicationStore, "candidate", read_candidate)
    try:
        result = coordinator.pool._discover_first_publication(_discovery(executor.attempt))
        assert result.ok, result.error
        assert result.fleet_hold is None
        assert result.value["first_publication_candidate"] is None
        assert not result.value.get("first_publication_pre_intent", False)
        assert observations
        assert result.value["source_workspace"] == binding.to_dict()
        assert result.value["source_receipt"] == receipt.to_dict()
        assert manager._read_receipt(265, SourceLane.IMPLEMENTATION) == receipt
        assert _git(binding.cwd, "status", "--porcelain") == ""
        assert not (manager.state_dir / "265-implementation-start.json").exists()
        assert not (manager.state_dir / "265-implementation-start.pending.json").exists()
    finally:
        coordinator._shutdown_pool()
        loop.close()


def _stage_case(tmp_path):
    """Use real intake and stages with authenticated synthetic GitHub facts."""
    from hephaestus.automation.current_plan import read_current_plan
    from hephaestus.automation.state_labels import ATHENA_FINALIZED_PLAN_LABEL, STATE_PLAN_GO
    from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub
    from tests.unit.automation.test_finalized_plan_scope import _finalized_body

    body = _finalized_body("## Files to Modify\n- `tracked.txt`")
    coordinator, executor, manager, binding, loop = _fleet_coordinator(
        tmp_path, plan=body, source_repository="Athena"
    )
    github = FakeStageGitHub(
        labels=[STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL],
        issue_body=body,
        issue_title="Implement the admitted issue",
    )
    coordinator.github = github
    executor.plan_reader = lambda: read_current_plan(265, github)
    entry = coordinator._seed_direct_issue_entry("Athena", 265, github=github)
    assert entry.stage is StageName.PLANNING
    return coordinator, executor, manager, binding, loop, entry


def test_direct_intake_reaches_one_bound_implementation_without_source_preparation(
    tmp_path, monkeypatch
):
    """Normal stage callbacks retain the admitted branch, receipt and current plan."""
    from hephaestus.automation.pipeline.stages.repo import DIRECT_SCOPE_BASE_SHA_KEY

    coordinator, executor, manager, binding, loop, entry = _stage_case(tmp_path)
    submitted = []

    def submit(job, on_done_state, **kwargs):
        handle = JobHandle(job=job, on_done_state=on_done_state)
        submitted.append(handle)
        return handle

    monkeypatch.setattr(coordinator.pool, "submit", submit)
    try:
        item = coordinator._prepare_direct_item(entry, "Athena", binding.revision, "e" * 32)
        assert item.branch == executor.attempt.source_receipt.branch
        assert item.worktree == str(binding.cwd)
        assert item.payload["_impl_source_workspace"] == binding.to_dict()
        assert item.payload["_impl_source_receipt"] == executor.attempt.source_receipt
        assert DIRECT_SCOPE_BASE_SHA_KEY not in item.payload
        assert item.result is None
        assert item.stage is StageName.PLANNING
        coordinator._push_item(item, item.stage, enter=True)
        for _ in range(3):
            coordinator._drain_queues()
        operations = []
        for _ in range(5):
            assert submitted, item.result
            handle = submitted.pop(0)
            job = handle.job
            if isinstance(job, AgentJob):
                assert job.descr == "implement"
                assert job.fleet_attempt is executor.attempt
                assert job.workspace == binding and job.cwd == binding.cwd
                assert job.model == "" and not job.fallback_model
                assert job.prompt_kwargs["issue_body"] == entry.issue_body
                assert job.prompt_kwargs["branch_name"] == item.branch
                assert not submitted
                break
            if isinstance(job, GitHubJob):
                assert isinstance(job.request, ReadCurrentPlanScopeRequest)
                result = JobResult(
                    ok=True,
                    value=PipelineGitHubJobRunner._read_current_plan_scope(
                        job.request, coordinator.github
                    ),
                )
                operations.append("read_current_plan_scope")
            else:
                assert isinstance(job, GitJob)
                assert job.op == "discover_first_publication", job.op
                assert job.workspace == binding
                operations.append(job.op)
                result = coordinator.pool._discover_first_publication(
                    replace(job, deadline_s=time.monotonic() + 30)
                )
                assert result.ok, result.error
            coordinator._handle_completion(handle, result)
        else:
            pytest.fail("the admitted issue did not reach its implementation job")
        assert operations == ["read_current_plan_scope", "discover_first_publication"]
        assert coordinator.github.mutation_log == []
        assert (
            manager._read_receipt(265, SourceLane.IMPLEMENTATION) == executor.attempt.source_receipt
        )
        assert not (manager.state_dir / "265-implementation-start.json").exists()
        executor.client.fleet_command.assert_not_called()
    finally:
        coordinator._shutdown_pool()
        loop.close()


@pytest.mark.parametrize("plan_state", ["valid", "changed-body", "invalid-seal"])
def test_fleet_plan_review_uses_the_authenticated_bound_body_without_comment_plan(
    tmp_path, plan_state
):
    """A sealed admission can advance only while its authenticated body stays unchanged."""
    from hephaestus.automation.pipeline.routing import Disposition
    from tests.unit.automation.test_finalized_plan_scope import _finalized_body

    coordinator, executor, _, binding, loop, entry = _stage_case(tmp_path)
    try:
        item = coordinator._prepare_direct_item(entry, "Athena", binding.revision, "e" * 32)
        ctx = coordinator._ctx_for(item)
        planning = coordinator.stages[StageName.PLANNING].on_enter(item, ctx)
        assert planning is not None and planning.disposition is Disposition.ADVANCE
        assert not coordinator.github.issue_comments(265)
        if plan_state == "changed-body":
            coordinator.github._issue_body = _finalized_body(
                "## Files to Modify\n- `different.txt`"
            )
        elif plan_state == "invalid-seal":
            coordinator.github._issue_body += "\nUnreviewed content.\n"
        item.stage = StageName.PLAN_REVIEW
        outcome = coordinator.stages[StageName.PLAN_REVIEW].on_enter(item, ctx)
        assert outcome is not None
        if plan_state == "valid":
            assert outcome.disposition is Disposition.ADVANCE, outcome
        else:
            assert outcome.disposition is not Disposition.ADVANCE, outcome
        assert coordinator.github.mutation_log == []
        executor.client.fleet_command.assert_not_called()
    finally:
        coordinator._shutdown_pool()
        loop.close()


@pytest.mark.parametrize(
    "fault", ["source-revision", "body-changed", "ordinary-plan", "existing-pr", "editor-changed"]
)
def test_direct_intake_holds_unmatched_admission_before_any_job(tmp_path, monkeypatch, fault):
    """Changed intake facts cannot create a branch, plan turn or another writer."""
    coordinator, executor, manager, binding, loop, entry = _stage_case(tmp_path)
    base_sha = binding.revision
    if fault == "source-revision":
        base_sha = "e" * 40
    elif fault == "body-changed":
        coordinator.github._issue_body += "\nChanged requirements.\n"
        entry = replace(entry, issue_body=coordinator.github._issue_body)
    elif fault == "ordinary-plan":
        coordinator.github._issue_body = "An ordinary idea requires a new planning admission."
        entry = replace(entry, issue_body=coordinator.github._issue_body)
    elif fault == "existing-pr":
        entry = replace(entry, pr_number=91)
    elif fault == "editor-changed":
        coordinator.github._issue_body_owned_by_viewer = False
    submit = Mock()
    monkeypatch.setattr(coordinator.pool, "submit", submit)
    before = manager._read_receipt(265, SourceLane.IMPLEMENTATION)
    try:
        item = coordinator._prepare_direct_item(entry, "Athena", base_sha, "e" * 32)
        assert item.result is not None and not item.result.passed
        assert "reconciliation_required" in item.result.reason
        assert item.stage is not StageName.FINISHED
        assert item.worktree == str(binding.cwd)
        assert ("Athena", 265, str(binding.cwd)) in coordinator.preserved
        assert manager._read_receipt(265, SourceLane.IMPLEMENTATION) == before
        submit.assert_not_called()
        executor.client.fleet_command.assert_not_called()
    finally:
        coordinator._shutdown_pool()
        loop.close()


@pytest.mark.parametrize(
    "fault",
    [
        "dirty",
        "receipt-changed",
        "binding-changed",
        "prior-fence",
        "publication",
        "pending-initial",
        "pending-rebase",
    ],
)
def test_bound_discovery_holds_changed_or_retained_work_without_repair(tmp_path, fault):
    """An admission reference cannot replace dirty, changed or pending source evidence."""
    coordinator, executor, manager, binding, loop = _fleet_coordinator(tmp_path)
    job = _discovery(executor.attempt)
    receipt = manager._read_receipt(265, SourceLane.IMPLEMENTATION)
    pending_path = manager.state_dir / "265-implementation-start.pending.json"
    if fault == "dirty":
        (binding.cwd / "tracked.txt").write_text("Unfinished work.\n")
    elif fault == "receipt-changed":
        manager._write_receipt(replace(receipt, generation=receipt.generation + 1))
    elif fault == "binding-changed":
        job = replace(job, workspace=replace(binding, generation=binding.generation + 1))
    elif fault == "prior-fence":
        manager._write_receipt(replace(receipt, obligations=("fleet-attempt:" + "d" * 64,)))
    elif fault == "pending-initial":
        pending_path.write_text("Uncertain legacy start.\n")
    elif fault == "publication":
        record = FirstPublicationRecord(
            operation_id="d" * 32,
            repository=executor.attempt.repository,
            scheduler_repository="Athena",
            issue_number=265,
            branch=receipt.branch,
            destination="https://github.com/HomericIntelligence/Athena.git",
            workspace=binding,
            tree_sha=_git(binding.cwd, "rev-parse", "HEAD^{tree}"),
            scope_base_sha=binding.revision,
            allowed_paths=("tracked.txt",),
            phase="publication_intent",
        )
        FirstPublicationStore(
            manager.common_dir, deadline=_PreparationDeadline(time.monotonic() + 30, time.monotonic)
        ).write(record, expected=None)
    before = manager._read_receipt(265, SourceLane.IMPLEMENTATION)
    content = (binding.cwd / "tracked.txt").read_bytes()
    pending = pending_path.read_bytes() if pending_path.exists() else None
    try:
        with patch.object(
            PendingRebaseStore,
            "candidate",
            return_value=object() if fault == "pending-rebase" else None,
        ):
            result = coordinator.pool._discover_first_publication(job)
        assert result.ok is False
        assert result.fleet_hold == "reconciliation_required"
        assert manager._read_receipt(265, SourceLane.IMPLEMENTATION) == before
        assert (binding.cwd / "tracked.txt").read_bytes() == content
        assert (pending_path.read_bytes() if pending_path.exists() else None) == pending
        executor.client.fleet_command.assert_not_called()
    finally:
        coordinator._shutdown_pool()
        loop.close()
