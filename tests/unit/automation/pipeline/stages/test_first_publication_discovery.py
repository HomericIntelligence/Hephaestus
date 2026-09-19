"""Test discovery before restored implementation work can change source."""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.github_jobs import GitHubJob, ReadCurrentPlanScopeRequest
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.implementation import ImplementationStage
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.review_journal import PlanDiscoveryResult
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from hephaestus.automation.state_labels import STATE_PLAN_GO
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub
from tests.unit.automation.pipeline.stages.test_stage_implementation import (
    _complete_plan_scope_read,
    _writer_binding,
    _writer_receipt,
)
from tests.unit.automation.pipeline.test_worker_pool import _git
from tests.unit.automation.test_first_publication_process import (
    _pool,
    _publication_job,
    _stop_before_publication_intent,
)


@pytest.mark.requires_posix
@pytest.mark.skipif(os.name != "posix", reason="Source ownership requires POSIX locks.")
@pytest.mark.parametrize(
    "state", ["GATE", "IMPLEMENT_WAIT", "TESTFIX_WAIT", "COMMIT_PUSH_WAIT", "ENTER"]
)
def test_pre_intent_commit_cannot_authorize_writer_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_ctx: Any, make_work_item: Any, state: str
) -> None:
    """Preserve unpublished H and block work when no publication intent exists."""

    class PlanGitHub(FakeStageGitHub):
        def discover_plan(self, issue_number: int) -> PlanDiscoveryResult:
            return PlanDiscoveryResult.found("## Files to Modify\n- `local.txt`\n")

    mask = os.umask(0o022)
    pool = None
    try:
        job = _publication_job(tmp_path, monkeypatch)
        assert job.workspace is not None
        root = Path(job.kwargs["repo_root"])
        manager = SourceWorkspaceManager(root, repository=job.repo)
        receipt = manager._require_receipt(9, SourceLane.IMPLEMENTATION)
        tree = _git(receipt.path, "rev-parse", "HEAD^{tree}")
        if state == "ENTER":
            _stop_before_publication_intent(job)
        pool = _pool(root, monkeypatch)
        stage = ImplementationStage()
        ctx = make_ctx(
            org="example",
            github=PlanGitHub(labels=[STATE_PLAN_GO]),
            paths=SimpleNamespace(repo_root=root, worktree=receipt.path),
        )
        item = make_work_item(repo="project", issue=9, state=state)
        if state != "ENTER":
            item.branch = "writer"
            item.worktree = str(receipt.path)
            item.payload.update(
                _impl_source_workspace=job.workspace.to_dict(),
                _impl_source_revision=job.workspace.revision,
                _impl_source_receipt=receipt,
                _synced_default_branch_sha=job.kwargs["scope_history_base_sha"],
            )
        else:
            item = WorkItem(
                repo="project",
                kind=ItemKind.ISSUE,
                issue=9,
                stage=StageName.IMPLEMENTATION,
                state="ENTER",
            )
            assert not item.branch and not item.worktree
            assert not item.payload
        assert stage.on_enter(item, ctx) is None
        pre_intent_observed = False
        for _ in range(8):
            result = stage.step(item, ctx)
            if isinstance(result, StageOutcome):
                assert result.disposition is Disposition.BLOCKED
                assert pre_intent_observed
                assert result.note == "first_publication_pre_intent_recovery_required"
                break
            if isinstance(result, Continue):
                item.state = result.next_state
                continue
            assert isinstance(result, JobRequest)
            if isinstance(result.job, GitHubJob):
                _complete_plan_scope_read(stage, item, ctx, result)
                continue
            assert isinstance(result.job, GitJob), "Unpublished H must not start another writer."
            assert result.job.op in {
                "discover_first_publication",
                "discover_pending_rebase",
                "create_worktree",
            }, "Unpublished H must not authorize publication or rebase."
            completed = pool._run_git(result.job)
            if result.job.op == "discover_first_publication":
                assert completed.ok, completed.error
                assert completed.value["first_publication_pre_intent"] is True
                pre_intent_observed = True
            item.state = result.on_done_state
            stage.on_job_done(item, completed, ctx)
        else:
            pytest.fail("Pre-intent ambiguity did not reach an operator recovery outcome.")
        assert manager._require_receipt(9, SourceLane.IMPLEMENTATION) == receipt
        assert _git(receipt.path, "rev-parse", "HEAD") == job.workspace.revision
        assert _git(receipt.path, "rev-parse", "HEAD^{tree}") == tree
        assert _git(receipt.path, "status", "--porcelain") == ""
        assert _git(root, "ls-remote", "origin", "refs/heads/writer") == ""
        assert not list((manager.state_dir / "first-publications").glob("9-*.json"))
        assert ctx.github.mutation_log == []
    finally:
        if pool is not None:
            pool.shutdown()
        os.umask(mask)


@pytest.mark.parametrize(
    "state",
    [
        "WORKTREE_WAIT",
        "IMPLEMENT_WAIT",
        "COMMIT_PUSH_WAIT",
        "PR_CREATE",
        "ADVISE_WAIT",
        "TEST_WAIT",
        "TESTFIX_WAIT",
        "REBASE_WAIT",
        "REBASE_CONTINUE_WAIT",
    ],
)
def test_restored_publication_checks_live_gate_before_source_work(
    make_ctx: Any, make_work_item: Any, state: str
) -> None:
    """Cached process flags cannot authorize a restored writer or publication."""
    stage = ImplementationStage()
    ctx = make_ctx(github=FakeStageGitHub(labels=[STATE_PLAN_GO]))
    item = make_work_item(issue=7, state=state)
    item.payload["first_publication_discovery_checked"] = True

    assert stage.on_enter(item, ctx) is None
    assert item.state == "GATE"
    result = stage.step(item, ctx)
    assert isinstance(result, JobRequest)
    assert isinstance(result.job, GitJob)
    assert result.job.op == "discover_first_publication"
    stage.on_job_done(item, _no_candidate(result.job), ctx)
    assert stage.step(item, ctx) == Continue(next_state=state)


def test_confirmed_no_publication_resumes_source_preparation(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Only a matching successful read permits the ordinary new writer path."""
    stage = ImplementationStage()
    ctx = make_ctx(github=FakeStageGitHub(labels=[STATE_PLAN_GO]))
    item = make_work_item(issue=7, state="GATE")
    request = stage.step(item, ctx)
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, GitJob)
    stage.on_job_done(item, _no_candidate(request.job), ctx)

    assert stage.step(item, ctx) == Continue(next_state="WORKTREE_WAIT")


@pytest.mark.parametrize("drift", ["request", "issue", "repository", "branch", "state"])
def test_foreign_publication_callback_preserves_pending_request(
    make_ctx: Any, make_work_item: Any, drift: str
) -> None:
    """A stale completion cannot consume a different request or publish metadata."""
    stage = ImplementationStage()
    ctx = make_ctx(github=FakeStageGitHub(labels=[STATE_PLAN_GO]))
    item = make_work_item(issue=7, state="GATE")
    request = stage.step(item, ctx)
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, GitJob)
    result = _no_candidate(request.job)
    assert isinstance(result.value, dict)
    if drift == "request":
        result.value["publication_discovery_request_id"] = "f" * 32
    elif drift == "issue":
        item.issue = 8
    elif drift == "repository":
        item.repo = "other"
    elif drift == "branch":
        item.branch = "other"
    else:
        item.state = "PR_CREATE"
    before = deepcopy(item)

    stage.on_job_done(item, result, ctx)

    assert item == before


def _no_candidate(job: GitJob) -> JobResult:
    """Supply a transport completion bound to the actual queued discovery."""
    return JobResult(
        ok=True,
        value={
            "publication_discovery_request_id": job.kwargs["publication_discovery_request_id"],
            "repository": job.transport_repository,
            "issue_number": job.kwargs["issue_number"],
            "first_publication_candidate": None,
        },
    )


@pytest.mark.parametrize(
    ("state", "manual", "next_state"),
    [
        ("REBASE_WAIT", False, "REBASE_WAIT"),
        ("REBASE_CONTINUE_WAIT", False, "REBASE_CONTINUE_WAIT"),
        ("REBASE_CONFLICT_WAIT", False, "REBASE_CONFLICT_WAIT"),
        ("IMPLEMENT_WAIT", True, "WORKTREE_WAIT"),
        ("COMMIT_PUSH_WAIT", True, "WORKTREE_WAIT"),
    ],
)
def test_pre_intent_rebase_route_cannot_authorize_a_writer(
    make_ctx: Any, make_work_item: Any, state: str, manual: bool, next_state: str
) -> None:
    """Route ambiguity only to the existing rebase owner, then require a new read."""
    stage = ImplementationStage()
    ctx = make_ctx(github=FakeStageGitHub(labels=[STATE_PLAN_GO]))
    item = make_work_item(issue=7, state=state)
    if manual:
        item.payload["manual_rebase_required"] = True
    stage.on_enter(item, ctx)
    request = stage.step(item, ctx)
    assert isinstance(request, JobRequest) and isinstance(request.job, GitJob)
    result = _no_candidate(request.job)
    result.value["first_publication_pre_intent"] = True
    stage.on_job_done(item, result, ctx)
    assert stage.step(item, ctx) == Continue(next_state=next_state)
    item.state = "GATE"
    repeated = stage.step(item, ctx)
    assert isinstance(repeated, JobRequest) and isinstance(repeated.job, GitJob)
    assert repeated.job.op == "discover_first_publication"
    assert (
        repeated.job.kwargs["publication_discovery_request_id"]
        != (request.job.kwargs["publication_discovery_request_id"])
    )
    assert ctx.github.mutation_log == []


@pytest.mark.parametrize("timing", ["before_callback", "after_callback"])
@pytest.mark.parametrize("drift", ["resume_state", "manual_request"])
def test_pre_intent_rebase_route_rejects_changed_restart_context(
    make_ctx: Any, make_work_item: Any, timing: str, drift: str
) -> None:
    """A changed route cannot turn a rebase-only result into writer admission."""
    stage = ImplementationStage()
    ctx = make_ctx(github=FakeStageGitHub(labels=[STATE_PLAN_GO]))
    item = make_work_item(issue=7, state="REBASE_WAIT")
    stage.on_enter(item, ctx)
    request = stage.step(item, ctx)
    assert isinstance(request, JobRequest) and isinstance(request.job, GitJob)
    result = _no_candidate(request.job)
    result.value["first_publication_pre_intent"] = True
    if timing == "after_callback":
        stage.on_job_done(item, result, ctx)
    if drift == "resume_state":
        item.payload["first_publication_resume_state"] = "IMPLEMENT_WAIT"
    else:
        item.payload["manual_rebase_required"] = True
    if timing == "before_callback":
        before = deepcopy(item)
        stage.on_job_done(item, result, ctx)
        assert item == before
    else:
        outcome = stage.step(item, ctx)
        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition is Disposition.BLOCKED
    assert ctx.github.mutation_log == []


def test_retained_publication_refreshes_scope_and_dispatches_owned_recovery(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Recover the retained source without preparing or implementing it again."""
    _recovery_request(make_ctx, make_work_item)


def _recovery_request(make_ctx: Any, make_work_item: Any) -> tuple[Any, Any, Any, GitJob]:
    """Drive discovery and a fresh scope read through the actual stage."""

    class PlanGitHub(FakeStageGitHub):
        def discover_plan(self, issue_number: int) -> PlanDiscoveryResult:
            return PlanDiscoveryResult.found("## Files to Modify\n- `local.txt`\n")

    stage = ImplementationStage()
    ctx = make_ctx(
        github=PlanGitHub(labels=[STATE_PLAN_GO]),
        config_overrides={"run_pre_pr_tests": True, "pre_pr_test_argv": ("controlled", "focused")},
    )
    item = make_work_item(issue=7, state="GATE")
    request = stage.step(item, ctx)
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, GitJob)
    result = _no_candidate(request.job)
    assert isinstance(result.value, dict)
    retained = deepcopy(item)
    retained.branch = "7-retained"
    binding = _writer_binding(retained, path=str(Path(ctx.paths.repo_root) / "build/retained"))
    receipt = _writer_receipt(retained, binding)
    result.value.update(
        first_publication_candidate="c" * 32,
        source_workspace=binding.to_dict(),
        source_receipt=receipt.to_dict(),
    )
    item.payload.update(test_receipt="old test pass", tests_failed=True, git_error=True)

    stage.on_job_done(item, result, ctx)

    assert item.branch == retained.branch
    assert item.worktree == str(binding.cwd)
    assert item.payload["_impl_source_receipt"] == receipt
    assert not {"test_receipt", "tests_failed", "git_error"} & item.payload.keys()
    scope = stage.step(item, ctx)
    assert isinstance(scope, JobRequest)
    assert isinstance(scope.job, GitHubJob)
    assert isinstance(scope.job.request, ReadCurrentPlanScopeRequest)
    _complete_plan_scope_read(stage, item, ctx, scope)
    recovery = stage.step(item, ctx)
    assert isinstance(recovery, JobRequest)
    assert isinstance(recovery.job, GitJob)
    assert recovery.job.op == "commit_push"
    assert recovery.job.workspace == binding
    assert recovery.job.kwargs["first_publication_candidate"] == "c" * 32
    assert recovery.job.kwargs["allowed_paths"] == ("local.txt",)
    assert recovery.job.kwargs["publication_test_argv"] == ("controlled", "focused")
    assert len(recovery.job.kwargs["publication_recovery_request_id"]) == 32
    assert "scope_history_base_sha" not in recovery.job.kwargs
    assert recovery.on_done_state == "GATE"
    assert stage.step(item, ctx) == recovery
    assert item.attempts == retained.attempts
    assert ctx.github.mutation_log == []
    return stage, item, ctx, recovery.job


def _recovery_result(item: Any, job: GitJob) -> JobResult:
    """Return the complete success envelope for the current stage request."""
    assert job.workspace is not None
    head = job.workspace.revision
    return JobResult(
        ok=True,
        value={
            "publication_recovery_request_id": job.kwargs["publication_recovery_request_id"],
            "first_publication_candidate": job.kwargs["first_publication_candidate"],
            "publication_state": "published",
            "head_sha": head,
            "baseline_remote_sha": None,
            "refresh_phase": None,
            "observed_remote_sha": head,
            "pushed": True,
            "source_workspace": job.workspace.to_dict(),
            "source_receipt": item.payload["_impl_source_receipt"].to_dict(),
            "first_publication_validation": {
                "argv": job.kwargs["publication_test_argv"],
                "head_sha": head,
                "tree_sha": "b" * 40,
                "publication_recovery_request_id": job.kwargs["publication_recovery_request_id"],
            },
        },
    )


def test_owned_recovery_completion_advances_without_repeating_work(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Accept one current result and ignore its later duplicate."""
    stage, item, ctx, job = _recovery_request(make_ctx, make_work_item)
    result = _recovery_result(item, job)
    stage.on_job_done(item, result, ctx)
    assert stage.step(item, ctx) == Continue(next_state="PR_CREATE")
    assert job.workspace is not None
    assert item.payload["_worktree_cleanup_head_sha"] == job.workspace.revision
    assert "controlled focused" in item.payload["test_receipt"]
    completed = deepcopy(item)
    stage.on_job_done(item, result, ctx)
    assert item == completed


@pytest.mark.parametrize("failure", ["worker", "source", "validation", "publication"])
def test_invalid_recovery_completion_blocks_without_writer_retry(
    make_ctx: Any, make_work_item: Any, failure: str
) -> None:
    """A failed or malformed completion cannot advance or repeat implementation."""
    stage, item, ctx, job = _recovery_request(make_ctx, make_work_item)
    result = _recovery_result(item, job)
    assert isinstance(result.value, dict)
    if failure == "worker":
        result = JobResult(ok=False, value=result.value, error="check failed", stderr_tail="detail")
    elif failure == "source":
        result.value["source_receipt"]["revision"] = "f" * 40
    elif failure == "validation":
        result.value["first_publication_validation"]["publication_recovery_request_id"] = "f" * 32
    else:
        result.value["observed_remote_sha"] = "f" * 40
    attempts = dict(item.attempts)
    stage.on_job_done(item, result, ctx)
    assert stage.step(item, ctx) == StageOutcome(
        Disposition.BLOCKED, "first_publication_recovery_failed"
    )
    assert item.attempts == attempts
    assert ctx.github.mutation_log == []


@pytest.mark.parametrize("drift", ["nonce", "candidate", "issue", "branch", "state", "source"])
def test_stale_recovery_completion_cannot_consume_another_request(
    make_ctx: Any, make_work_item: Any, drift: str
) -> None:
    """Keep pending ownership and metadata unchanged for an unowned callback."""
    stage, item, ctx, job = _recovery_request(make_ctx, make_work_item)
    result = _recovery_result(item, job)
    assert isinstance(result.value, dict)
    if drift == "nonce":
        result.value["publication_recovery_request_id"] = "f" * 32
    elif drift == "candidate":
        result.value["first_publication_candidate"] = "f" * 32
    elif drift == "issue":
        item.issue = 8
    elif drift == "branch":
        item.branch = "another-branch"
    elif drift == "state":
        item.state = "PR_CREATE"
    else:
        item.payload["_impl_source_revision"] = "f" * 40
    before = deepcopy(item)
    stage.on_job_done(item, result, ctx)
    assert item == before


def test_malformed_source_receipt_blocks_discovery_without_callback_exception(
    make_ctx: Any, make_work_item: Any
) -> None:
    """An invalid worker receipt cannot crash callback handling or admit a writer."""
    stage = ImplementationStage()
    ctx = make_ctx(github=FakeStageGitHub(labels=[STATE_PLAN_GO]))
    item = make_work_item(issue=7, state="GATE")
    request = stage.step(item, ctx)
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, GitJob)
    result = _no_candidate(request.job)
    assert isinstance(result.value, dict)
    binding = WorkspaceBinding.source(
        cwd=Path(ctx.paths.repo_root) / "build/.worktrees/retained",
        reusable_root=Path(ctx.paths.repo_root),
        repository=item.repo,
        ownership_key=f"{item.repo}:test:7:impl",
        item_number=7,
        lane=SourceLane.IMPLEMENTATION,
        revision="a" * 40,
        generation=1,
        detached=False,
    )
    result.value.update(
        first_publication_candidate="b" * 32,
        source_workspace=binding.to_dict(),
        source_receipt={},
    )
    branch, worktree = item.branch, item.worktree

    stage.on_job_done(item, result, ctx)

    assert stage.step(item, ctx) == StageOutcome(
        Disposition.BLOCKED, "first_publication_discovery_failed"
    )
    assert (item.branch, item.worktree) == (branch, worktree)


@pytest.mark.parametrize("stale", ["failure", "pending"])
def test_restart_refreshes_failed_or_pending_scope_before_recovery(
    make_ctx: Any, make_work_item: Any, tmp_path: Path, stale: str
) -> None:
    """An earlier process cannot leave a permanent scope-read failure."""
    stage = ImplementationStage()
    ctx = make_ctx(
        github=FakeStageGitHub(labels=[STATE_PLAN_GO]),
        config_overrides={
            "agent": "codex",
            "codex_isolation_adapter": "production",
            "codex_isolation_deployment_lock": tmp_path / "deployment-lock.json",
            "codex_isolation_deployment_lock_sha256": "a" * 64,
        },
    )
    item = make_work_item(issue=7, state="WORKTREE_WAIT")
    old_request = ReadCurrentPlanScopeRequest("test-org/test-repo", 7, 9999999999.0)
    if stale == "failure":
        item.payload["_implementation_plan_scope_failure"] = True
    else:
        item.payload["_pending_github_request"] = old_request
    assert stage.on_enter(item, ctx) is None
    request = stage.step(item, ctx)
    assert isinstance(request, JobRequest)
    assert isinstance(request.job, GitHubJob)
    assert isinstance(request.job.request, ReadCurrentPlanScopeRequest)
    assert request.job.request != old_request
    assert item.state == "GATE"
    assert ctx.github.mutation_log == []
