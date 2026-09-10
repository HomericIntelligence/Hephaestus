"""Keep the current local source receipt after a rebase validation failure."""

import os
import subprocess
import threading
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import SourceWorkspaceManager, SourceWorkspaceReceipt


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a local fixture command within a fixed time limit."""
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        cwd=path,
        env={**os.environ, "GIT_EDITOR": "true"},
        text=True,
        capture_output=True,
        check=check,
        timeout=5,
    )


@pytest.mark.parametrize("failed_gate", ["structural", "semantic", "metadata"])
def test_completed_rebase_keeps_local_receipt_when_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_gate: str
) -> None:
    """Failed validation prevents publication and keeps the completed local commit."""
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test User")
    (root / "tracked.txt").write_text("initial\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "initial")
    initial = _git(root, "rev-parse", "HEAD").stdout.strip()
    manager = SourceWorkspaceManager(root, repository="repo")
    binding = manager.prepare(42, SourceLane.IMPLEMENTATION, initial, branch="writer")
    with manager.implementation_local_commit(42, branch="writer", path=binding.cwd) as record:
        (binding.cwd / "tracked.txt").write_text("writer\n")
        _git(binding.cwd, "commit", "-am", "writer change")
        writer_head = _git(binding.cwd, "rev-parse", "HEAD").stdout.strip()
        binding = record(writer_head)
    (root / "tracked.txt").write_text("main\n")
    _git(root, "commit", "-am", "main change")
    base_head = _git(root, "rev-parse", "HEAD").stdout.strip()
    assert _git(binding.cwd, "rebase", base_head, check=False).returncode == 1
    paused_head = _git(binding.cwd, "rev-parse", "HEAD").stdout.strip()
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=CompletionQueue(maxsize=1),
        lock_dir=tmp_path / "locks",
    )
    monkeypatch.setattr(pool, "_validate_rebase_continuation_remote", lambda *args, **kw: None)
    monkeypatch.setattr(pool, "_validate_rebase_conflict_edits", lambda *args, **kw: None)
    monkeypatch.setattr(pool, "_select_rebase_policy", lambda _repo: None)

    def complete(cwd: Path, **_kwargs: Any) -> None:
        (cwd / "tracked.txt").write_text("resolved writer and main\n")
        _git(cwd, "add", "tracked.txt")
        _git(cwd, "rebase", "--continue")

    monkeypatch.setattr(pool, "_continue_rebase_process", complete)
    failure = JobResult(ok=False, error=f"{failed_gate} validation failed")
    gates = {
        "structural": "_run_rebase_structural_validation",
        "semantic": "_validate_rebased_tree",
        "metadata": "_verify_rebased_commit_metadata",
    }
    for kind, method in gates.items():
        monkeypatch.setattr(
            pool, method, Mock(return_value=failure if kind == failed_gate else None)
        )
    publication = Mock(side_effect=AssertionError("Failed validation must prevent publication"))
    monkeypatch.setattr(pool, "_prepare_rebase_review_publication", publication)
    job = GitJob(
        "repo",
        "continue_rebase",
        30,
        workspace=binding,
        kwargs={
            "cwd": str(binding.cwd),
            "repo_root": str(root),
            "issue_number": 42,
            "branch": "writer",
            "base_sha": base_head,
            "expected_remote_sha": writer_head,
            "conflict_paths": ("tracked.txt",),
            "conflict_snapshot": {},
            "conflict_index_snapshot": "f" * 64,
            "paused_head_sha": paused_head,
            "rebase_reason": "review_conflict",
            "publish_rebased_head": True,
        },
    )
    try:
        result = pool._run_git(job)
        current_head = _git(binding.cwd, "rev-parse", "HEAD").stdout.strip()
        assert current_head != writer_head
        assert not result.ok and result.error == failure.error
        publication.assert_not_called()
        assert manager._require_receipt(42, SourceLane.IMPLEMENTATION).revision == current_head
        assert isinstance(result.value, dict)
        current = WorkspaceBinding.from_dict(result.value["source_workspace"])
        receipt = SourceWorkspaceReceipt.from_dict(result.value["source_receipt"])
        assert current.revision == current_head
        assert receipt.to_binding(root) == current
        assert result.value["head_sha"] == current_head
    finally:
        pool.shutdown(mark_interrupted=False)
