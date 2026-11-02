"""Drive real stages with synthetic provider, build and publication boundaries."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import Mock

import pytest

from hephaestus.agents.execution_policy import AgentOperation
from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.pipeline.athena_skill_jobs import AthenaSkillJob, AthenaSkillRequest
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.github_jobs import GitHubJob, ReadCurrentPlanScopeRequest
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.jobs import AgentJob, BuildTestJob, CompactJob, JobHandle
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.stages.base import JobRequest
from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner
from tests.unit.automation.pipeline.test_fleet_execution import _git, _job
from tests.unit.automation.pipeline.test_fleet_source_entry import _stage_case


def _complete_scope_reads(coordinator, pending):
    """Use the existing scope reader at its normal worker-result boundary."""
    for _ in range(3):
        if not pending or not isinstance(pending[0].job, GitHubJob):
            return
        handle = pending.pop(0)
        assert isinstance(handle.job.request, ReadCurrentPlanScopeRequest)
        receipt = PipelineGitHubJobRunner._read_current_plan_scope(
            handle.job.request, coordinator.github
        )
        coordinator._handle_completion(handle, JobResult(ok=True, value=receipt))
    pytest.fail("the stage repeated its scope read without progressing")


@contextmanager
def _flow(tmp_path, monkeypatch):
    """Retain normal intake, queues and callbacks while recording worker boundaries."""
    coordinator, executor, manager, binding, loop, entry = _stage_case(tmp_path)
    pending, submitted = [], []

    def submit(job, on_done_state, **kwargs):
        handle = JobHandle(job=job, on_done_state=on_done_state)
        pending.append(handle)
        submitted.append(job)
        return handle

    monkeypatch.setattr(coordinator.pool, "submit", submit)
    try:
        item = coordinator._prepare_direct_item(entry, "Athena", binding.revision, "e" * 32)
        coordinator._push_item(item, item.stage, enter=True)
        for _ in range(3):
            coordinator._drain_queues()
        _complete_scope_reads(coordinator, pending)
        assert len(pending) == 1, item.result
        discovery = pending.pop(0)
        assert isinstance(discovery.job, GitJob)
        assert discovery.job.op == "discover_first_publication"
        result = coordinator.pool._discover_first_publication(
            replace(discovery.job, deadline_s=time.monotonic() + 30)
        )
        assert result.ok, result.error
        coordinator._handle_completion(discovery, result)
        assert len(pending) == 1, item.result
        implementation = pending.pop(0)
        assert isinstance(implementation.job, AgentJob)
        assert implementation.job.descr == "implement"
        assert implementation.job.fleet_attempt is executor.attempt
        yield coordinator, executor, manager, binding, item, implementation, pending, submitted
    finally:
        coordinator._shutdown_pool()
        loop.close()


def _published_result(manager, binding, branch):
    """Produce real local source facts and synthetic external publication facts."""
    with manager.implementation_local_commit(
        265, branch=branch, path=binding.cwd, expected_binding=binding
    ) as record:
        _git(binding.cwd, "add", "tracked.txt")
        _git(binding.cwd, "commit", "--no-gpg-sign", "-m", "test: synthetic implementation")
        head = _git(binding.cwd, "rev-parse", "HEAD")
        successor = record(head)
    receipt = manager._read_receipt(265, SourceLane.IMPLEMENTATION)
    return JobResult(
        ok=True,
        value={
            "publication_state": "published",
            "head_sha": head,
            "baseline_remote_sha": None,
            "observed_remote_sha": head,
            "pushed": True,
            "refresh_phase": None,
            "source_workspace": successor.to_dict(),
            "source_receipt": receipt.to_dict(),
        },
    )


@pytest.mark.parametrize(
    "outcome",
    ["published", "tests-failed", "publication-unknown", "pr-create-unknown", "cancelled-test"],
)
def test_fleet_implementation_uses_normal_test_publication_and_preserved_holds(
    tmp_path, monkeypatch, outcome
):
    """Only confirmed build and publication results can reach PR creation."""
    with _flow(tmp_path, monkeypatch) as flow:
        coordinator, executor, manager, binding, item, implementation, pending, submitted = flow
        private_answer = "Synthetic private implementation answer."
        (binding.cwd / "tracked.txt").write_text("Synthetic implementation edit.\n")
        coordinator._handle_completion(implementation, JobResult(ok=True, value=private_answer))
        assert item.payload["implement_summary"] == private_answer
        assert len(pending) == 1
        build = pending.pop(0)
        assert isinstance(build.job, BuildTestJob)
        assert build.job.cwd == binding.cwd
        assert build.job.argv == tuple(coordinator.config.pre_pr_test_argv)
        assert coordinator.github.mutation_log == []
        create = Mock(wraps=coordinator.github.create_pr)
        monkeypatch.setattr(coordinator.github, "create_pr", create)
        if outcome == "cancelled-test":
            coordinator._handle_completion(
                build, JobResult(ok=False, interrupted=True, error="cancelled")
            )
        else:
            coordinator._handle_completion(
                build,
                JobResult(
                    ok=outcome != "tests-failed", value=0 if outcome != "tests-failed" else 1
                ),
            )
            _complete_scope_reads(coordinator, pending)
            if outcome != "tests-failed":
                assert len(pending) == 1, item.result
                publication = pending.pop(0)
                assert isinstance(publication.job, GitJob) and publication.job.op == "commit_push"
                assert publication.job.workspace == binding
                assert publication.job.kwargs["allowed_paths"] == ("tracked.txt",)
                assert publication.job.kwargs["publish_base_sha"] == binding.revision
                assert "expected_remote_sha" not in publication.job.kwargs
                if outcome == "publication-unknown":
                    result = JobResult(ok=False, error="publication reply was lost")
                else:
                    result = _published_result(manager, binding, item.branch)
                    if outcome == "pr-create-unknown":
                        create.side_effect = TimeoutError("PR creation reply was lost")
                coordinator._handle_completion(publication, result)
        assert item.result is not None and not item.result.passed
        expected_hold = (
            "new_admission_required"
            if outcome in {"published", "tests-failed"}
            else "reconciliation_required"
        )
        assert expected_hold in item.result.reason
        assert item.stage is StageName.IMPLEMENTATION
        assert item.state != "RESUMABLE"
        assert not pending
        assert sum(isinstance(job, AgentJob) for job in submitted) == 1
        assert ("Athena", 265, str(binding.cwd)) in coordinator.preserved
        assert binding.cwd.is_dir()
        assert manager._read_receipt(265, SourceLane.IMPLEMENTATION) is not None
        assert coordinator._all_idle()
        if outcome == "published":
            create.assert_called_once()
            assert item.pr == 1001
            assert "passed" in create.call_args.args[3]
            assert private_answer not in create.call_args.args[3]
        elif outcome == "pr-create-unknown":
            create.assert_called_once()
        else:
            create.assert_not_called()
        executor.client.fleet_command.assert_not_called()


@pytest.mark.parametrize("operation", ["test-fix", "compact", "skill-review"])
def test_fleet_rejects_other_model_dispatch_branches_before_worker_submission(
    tmp_path, monkeypatch, operation
):
    """A configured Fleet run cannot reach another native model dispatch path."""
    coordinator, executor, _, binding, loop, entry = _stage_case(tmp_path)
    submit = Mock()
    monkeypatch.setattr(coordinator.pool, "submit", submit)
    try:
        item = coordinator._prepare_direct_item(entry, "Athena", binding.revision, "e" * 32)
        if operation == "test-fix":
            job = _job(binding, operation=AgentOperation.TEST_FIX)
        elif operation == "compact":
            job = CompactJob("Athena", 265, "codex", "implementer", "", binding.cwd, 30)
        else:
            job = AthenaSkillJob(
                AthenaSkillRequest(
                    "pr-review", "Athena", 265, "codex", "", binding.cwd, 30, workspace=binding
                )
            )
        coordinator._submit_ready_job(item, JobRequest(job, "NEXT"))
        assert item.result is not None and "new_admission_required" in item.result.reason
        submit.assert_not_called()
        executor.client.fleet_command.assert_not_called()
    finally:
        coordinator._shutdown_pool()
        loop.close()


def test_fleet_commit_uses_existing_deterministic_message_without_provider(tmp_path, monkeypatch):
    """The controlled commit path must not create a hidden model turn."""
    from hephaestus.automation import commit_runtime

    coordinator, _, _, binding, loop, _ = _stage_case(tmp_path)
    provider = Mock(return_value='{"subject":"feat: Provider-generated message","body":""}')
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.worker_pool._invoke_claude_commit_message", provider
    )
    monkeypatch.setattr(
        commit_runtime,
        "_staged_change_context",
        lambda *args, **kwargs: ("tracked.txt", "one change"),
    )
    monkeypatch.setattr(commit_runtime, "_agentic_commit_email", lambda: "test@example.invalid")
    messages = []

    def commit(issue, cwd, agent, **kwargs):
        messages.append(
            commit_runtime._generate_commit_message(
                commit_runtime.CommitIssueMetadata(
                    issue, "Admitted implementation", "Reviewed body"
                ),
                cwd,
                agent,
                git_message_timeout=30,
                git_timeout=30,
                agent_model=None,
                pi_dir=None,
                git_env=None,
                claude_message_agent=kwargs.get("claude_message_agent"),
            )
        )
        return True

    monkeypatch.setattr(
        "hephaestus.automation.pipeline.worker_pool.git_utils.commit_if_changes", commit
    )
    try:
        job = GitJob(repo="Athena", op="commit_push", timeout_s=30)
        result = coordinator.pool._commit_if_changes_with_controlled_signing(
            job, (265, binding.cwd, "codex"), ("tracked.txt",), None, 30
        )
        assert result is True
        provider.assert_not_called()
        assert messages[0].startswith("feat: Implement #265\n")
    finally:
        coordinator._shutdown_pool()
        loop.close()
