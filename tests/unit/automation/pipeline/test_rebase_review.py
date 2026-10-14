"""Check exact rebase trees and host publication evidence."""

import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.rebase_review_verification import verify_rebase_tree


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def rebased_tree(tmp_path: Path) -> tuple[Path, str, str, str, str]:
    """Build one exact replay with a change on each branch."""
    _git(tmp_path, "init", "-b", "original")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "a").write_text("base\n")
    (tmp_path / "b").write_text("base\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "Create base.")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "a").write_text("reviewed\n")
    _git(tmp_path, "commit", "-am", "Change a.")
    reviewed = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-b", "target", base)
    (tmp_path / "b").write_text("upstream\n")
    _git(tmp_path, "commit", "-am", "Change b.")
    target = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "cherry-pick", reviewed)
    result = _git(tmp_path, "rev-parse", "HEAD")
    return tmp_path, base, reviewed, target, result


@pytest.mark.parametrize("extra_change", [False, True])
@pytest.mark.parametrize("review_base_is_ahead", [False, True])
def test_exact_replay_rejects_unrelated_edits(
    rebased_tree: tuple[Path, str, str, str, str], extra_change: bool, review_base_is_ahead: bool
) -> None:
    """Only the exact reviewed change and target base can retain a review."""
    cwd, base, reviewed, target, result = rebased_tree
    if extra_change:
        (cwd / "a").write_text("reviewed \n")
        _git(cwd, "commit", "-am", "Change whitespace.")
        result = _git(cwd, "rev-parse", "HEAD")
    tree = verify_rebase_tree(
        cwd,
        reviewed_head_sha=reviewed,
        reviewed_base_sha=target if review_base_is_ahead else base,
        target_base_sha=target,
        resulting_head_sha=result,
        timeout=30,
    )
    assert tree == (None if extra_change else _git(cwd, "rev-parse", f"{result}^{{tree}}"))


def test_conflict_resolution_does_not_prove_the_original_change(
    rebased_tree: tuple[Path, str, str, str, str],
) -> None:
    """A conflict needs a new source decision."""
    cwd, base, reviewed, target, result = rebased_tree
    _git(cwd, "checkout", "--detach", target)
    (cwd / "a").write_text("different upstream change\n")
    _git(cwd, "commit", "-am", "Change a upstream.")
    conflicting_base = _git(cwd, "rev-parse", "HEAD")
    assert (
        verify_rebase_tree(
            cwd,
            reviewed_head_sha=reviewed,
            reviewed_base_sha=base,
            target_base_sha=conflicting_base,
            resulting_head_sha=result,
            timeout=30,
        )
        is None
    )


@pytest.mark.parametrize("failure", [None, "remote_head", "tree", "audit_changed"])
def test_restart_rebuilds_proof_from_remote_and_original_tree(
    rebased_tree: tuple[Path, str, str, str, str], failure: str | None
) -> None:
    """A durable record needs fresh remote and tree evidence."""
    from unittest.mock import patch

    import hephaestus.automation.git_utils as git_utils
    from hephaestus.automation.pipeline.jobs import GitJob
    from hephaestus.automation.pipeline.rebase_review import RebaseReviewProof
    from hephaestus.automation.pipeline.worker_pool import WorkerPool
    from hephaestus.automation.rebase_review_receipt import (
        RebaseReviewRecord,
        original_audit_identity,
    )
    from hephaestus.automation.review_audit import ReviewAudit

    cwd, base, reviewed, target, result = rebased_tree
    tree = _git(cwd, "rev-parse", f"{result}^{{tree}}")
    record = RebaseReviewRecord(
        repository="test/repo",
        issue_number=7,
        pr_number=8,
        reviewed_head_sha=reviewed,
        reviewed_base_sha=base,
        source_head_sha=reviewed,
        target_base_sha=target,
        resulting_head_sha=result,
        resulting_tree_sha="e" * 40 if failure == "tree" else tree,
        original_audit_id=original_audit_identity(8, reviewed),
        audit=ReviewAudit("A", "Checks passed.", (), "", True, "GO"),
    )
    run = git_utils.run

    def remote(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "ls-remote" in cmd:
            head = reviewed if failure == "remote_head" else result
            return subprocess.CompletedProcess(cmd, 0, f"{head}\trefs/pull/8/head\n", "")
        if "fetch" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return run(cmd, **kwargs)

    pool = object.__new__(WorkerPool)
    pool._shutdown = threading.Event()
    deadline = time.monotonic() + 30
    inspections = 0
    from unittest.mock import MagicMock

    def inspect(job: Any, *, shutdown: threading.Event, deadline_s: float | None) -> object:
        from hephaestus.automation.pipeline.github_jobs import RebaseReviewInspected

        nonlocal inspections
        assert shutdown is pool._shutdown
        assert deadline_s == deadline
        assert job.request.record == record
        inspections += 1
        return RebaseReviewInspected(job.request, failure != "audit_changed")

    pool._github_job_runner = MagicMock(run=inspect)
    with (
        patch.object(pool, "_authenticated_remote_git_configuration", return_value=({}, ())),
        patch.object(git_utils, "run", side_effect=remote),
    ):
        restored = pool._git_verify_rebase_review(
            GitJob(
                repo="repo",
                expected_repository="test/repo",
                op="verify_rebase_review",
                timeout_s=30,
                deadline_s=deadline,
                kwargs={"record": record, "repo_root": str(cwd)},
            )
        )
    assert restored.ok is (failure is None)
    if restored.ok:
        assert isinstance(restored.value, RebaseReviewProof)
        assert restored.value.reviewed_head_sha == reviewed
        assert restored.value.resulting_head_sha == result
        assert inspections == 2
    else:
        assert inspections == 1
