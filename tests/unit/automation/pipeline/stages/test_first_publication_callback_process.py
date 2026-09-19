"""Test stage callbacks after a completed publication worker stops."""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation import git_utils
from hephaestus.automation.first_publication_recovery import FirstPublicationStore
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.github_jobs import GitHubJob
from hephaestus.automation.pipeline.stages import Continue, JobRequest
from hephaestus.automation.pipeline.stages.implementation import ImplementationStage
from hephaestus.automation.review_journal import PlanDiscoveryResult
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from hephaestus.automation.state_labels import STATE_PLAN_GO
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub
from tests.unit.automation.pipeline.stages.test_stage_implementation import (
    _complete_plan_scope_read,
)
from tests.unit.automation.pipeline.test_worker_pool import _git
from tests.unit.automation.test_first_publication_process import _pool, _process, _publication_job

pytestmark = [
    pytest.mark.requires_posix,
    pytest.mark.skipif(os.name != "posix", reason="Source ownership requires POSIX locks."),
]


def _stop_after_completion(job: GitJob) -> None:
    """Stop the owned child after real completion readback and before its callback."""
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_process, args=(job, "after_complete", child))
    try:
        process.start()
        child.close()
        assert parent.poll(30), "The worker did not reach completion readback."
        message = parent.recv()
        assert message == ("checkpoint", process.pid), message
        process.kill()
        process.join(timeout=5)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.kill()
            process.join(timeout=5)
            assert not process.is_alive()
            process.close()
        parent.close()
        child.close()


def test_completed_publication_death_recovers_actual_stage_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_ctx: Any, make_work_item: Any
) -> None:
    """Deliver one real recovered result without another source or publication write."""

    class PlanGitHub(FakeStageGitHub):
        def discover_plan(self, issue_number: int) -> PlanDiscoveryResult:
            assert issue_number == 9
            return PlanDiscoveryResult.found("## Files to Modify\n- `local.txt`\n")

    mask = os.umask(0o022)
    pool = None
    try:
        with pytest.MonkeyPatch.context() as preparation:
            job = _publication_job(tmp_path, preparation)
        job = replace(job, repo="project", expected_repository="example/project")
        assert job.workspace is not None
        _stop_after_completion(job)
        root = Path(job.kwargs["repo_root"])
        manager = SourceWorkspaceManager(root, repository=job.transport_repository)
        receipt = manager._require_receipt(9, SourceLane.IMPLEMENTATION)
        tree = _git(receipt.path, "rev-parse", "HEAD^{tree}")
        records = list((manager.state_dir / "first-publications").glob("9-*.json"))
        assert len(records) == 1
        record = records[0]
        content, identity = record.read_bytes(), record.stat()
        assert json.loads(content)["phase"] == "complete"
        validation = (
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "assert Path('local.txt').read_text() == 'Local change.\\n'; "
            f"Path({str(tmp_path / 'callback-validation')!r}).write_text('validated')",
        )
        pool = _pool(root, monkeypatch)

        def reject_mutation(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("Completed recovery must not repeat a mutation.")

        monkeypatch.setattr(git_utils, "commit_if_changes", reject_mutation)
        monkeypatch.setattr(git_utils, "rebase_worktree_onto", reject_mutation)
        monkeypatch.setattr(FirstPublicationStore, "write", reject_mutation)
        run = git_utils.run

        def no_push(argv: Any, **kwargs: Any) -> Any:
            assert "push" not in argv
            return run(argv, **kwargs)

        monkeypatch.setattr(git_utils, "run", no_push)
        stage = ImplementationStage()
        ctx = make_ctx(
            org="example",
            github=PlanGitHub(labels=[STATE_PLAN_GO]),
            paths=SimpleNamespace(repo_root=root, worktree=receipt.path),
            config_overrides={"run_pre_pr_tests": True, "pre_pr_test_argv": validation},
        )
        item = make_work_item(repo="project", issue=9, state="ENTER")
        assert stage.on_enter(item, ctx) is None
        operations: list[str] = []
        scope_reads = 0
        recovered = None
        for _ in range(10):
            request = stage.step(item, ctx)
            if isinstance(request, Continue):
                item.state = request.next_state
                if item.state == "PR_CREATE":
                    break
                continue
            assert isinstance(request, JobRequest), request
            if isinstance(request.job, GitHubJob):
                scope_reads += 1
                _complete_plan_scope_read(stage, item, ctx, request)
                continue
            assert isinstance(request.job, GitJob), "Recovery must not submit another writer."
            assert request.job.op in {"discover_first_publication", "commit_push"}
            operations.append(request.job.op)
            result = pool._run_git(request.job)
            assert result.ok, result
            if request.job.op == "commit_push":
                assert request.job.kwargs["allowed_paths"] == ("local.txt",)
                assert result.value["head_sha"] == job.workspace.revision
                assert result.value["first_publication_validation"]["tree_sha"] == tree
                recovered = result
            item.state = request.on_done_state
            stage.on_job_done(item, result, ctx)
        else:
            pytest.fail("Recovered publication did not reach PR creation.")
        assert operations == ["discover_first_publication", "commit_push"]
        assert scope_reads == 1
        assert recovered is not None
        before = deepcopy(item)
        stage.on_job_done(item, recovered, ctx)
        assert item == before
        assert item.payload["_worktree_cleanup_head_sha"] == job.workspace.revision
        assert (tmp_path / "callback-validation").read_text() == "validated"
        assert record.read_bytes() == content
        assert record.stat().st_ino == identity.st_ino
        assert record.stat().st_mtime_ns == identity.st_mtime_ns
        assert manager._require_receipt(9, SourceLane.IMPLEMENTATION) == receipt
        assert _git(receipt.path, "rev-parse", "HEAD") == job.workspace.revision
        assert _git(receipt.path, "rev-parse", "HEAD^{tree}") == tree
        assert _git(receipt.path, "status", "--porcelain") == ""
        assert (tmp_path / "pushes").read_text().splitlines() == ["published"]
        assert (tmp_path / "push-attempts").read_text().splitlines() == ["attempted"]
        assert (tmp_path / "record-writes").read_text().splitlines() == [
            "publication_intent",
            "complete",
        ]
        assert ctx.github.mutation_log == []
    finally:
        if pool is not None:
            pool.shutdown()
        os.umask(mask)
