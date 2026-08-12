"""Shared low-level cleanup operations for both pipeline worker lanes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hephaestus.automation.git_utils import (
    delete_local_branch_if_unchanged,
    delete_reserved_branch_if_unchanged,
    run,
)
from hephaestus.automation.worktree_manager import WorktreeManager
from hephaestus.utils.file_lock import file_lock
from hephaestus.utils.helpers import get_repo_root

from .git_jobs import GitJob
from .job_results import JobResult


def _is_full_commit_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in (40, 64)
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _is_expected_worktree_path(path: Path, *, repo_root: Path, issue_number: int) -> bool:
    """Bind destructive cleanup to one managed issue worktree identity."""
    if issue_number <= 0:
        return False
    try:
        resolved = path.resolve()
        root = repo_root.resolve()
    except OSError:
        return False
    expected_names = (
        f"issue-{issue_number}",
        f"issue-{issue_number}-direct-",
        f"review-pr-{issue_number}",
    )
    if (
        resolved.name != expected_names[0]
        and not resolved.name.startswith(expected_names[1])
        and resolved.name != expected_names[2]
    ):
        return False
    return resolved.parent in {root, root / "build" / ".worktrees"}


def _worktree_record(
    worktree_path: Path,
    *,
    repo_root: Path,
    timeout: int,
    worktree_manager_type: Any,
) -> dict[str, str] | None:
    """Return the registered record for an exact worktree path."""
    target = worktree_path.resolve()
    manager = worktree_manager_type(repo_root=repo_root)
    return next(
        (
            record
            for record in manager.list_worktrees(raise_on_error=True, timeout=timeout)
            if record.get("path") and Path(record["path"]).resolve() == target
        ),
        None,
    )


def run_cleanup_job(job: GitJob, *, worktree_manager_type: Any = WorktreeManager) -> JobResult:
    """Run one validated worktree or reservation cleanup operation."""
    if job.op == "release_branch_reservation":
        branch_name = str(job.kwargs.get("branch") or "")
        base_sha = str(job.kwargs.get("base_sha") or "")
        repo_root_value = job.kwargs.get("repo_root")
        repo_root = Path(str(repo_root_value)) if repo_root_value else None
        if (
            not branch_name
            or not _is_full_commit_sha(base_sha)
            or repo_root is None
            or not repo_root.is_dir()
        ):
            return JobResult(
                ok=False,
                error="release_branch_reservation requires branch, base_sha, and repo_root",
            )
        released = delete_reserved_branch_if_unchanged(
            branch_name,
            base_sha,
            repo_root,
            timeout=job.timeout_s,
        )
        return JobResult(ok=True, value=released)

    if job.op != "remove_worktree":
        raise TypeError(f"unsupported cleanup Git operation: {job.op}")
    if job.kwargs.get("worktree_path"):
        worktree_path = Path(str(job.kwargs["worktree_path"]))
        repo_root = Path(str(job.kwargs.get("repo_root") or get_repo_root()))
        issue_number = job.kwargs.get("issue_number")
        if (
            isinstance(issue_number, bool)
            or not isinstance(issue_number, int)
            or not _is_expected_worktree_path(
                worktree_path,
                repo_root=repo_root,
                issue_number=issue_number,
            )
        ):
            return JobResult(ok=False, error="worktree cleanup identity is invalid")
        expected_branch = job.kwargs.get("expected_branch")
        with file_lock(worktree_manager_type.git_metadata_lock_path(repo_root)):
            if expected_branch is not None:
                record = _worktree_record(
                    worktree_path,
                    repo_root=repo_root,
                    timeout=job.timeout_s,
                    worktree_manager_type=worktree_manager_type,
                )
                if (
                    not isinstance(expected_branch, str)
                    or not expected_branch
                    or record is None
                    or record.get("branch") != f"refs/heads/{expected_branch}"
                ):
                    return JobResult(ok=False, error="worktree cleanup ownership changed")
            command = ["git", "worktree", "remove", str(worktree_path)]
            if job.kwargs.get("force"):
                command.append("--force")
            run(command, cwd=repo_root, timeout=job.timeout_s)
            run(
                ["git", "worktree", "prune"],
                cwd=repo_root,
                check=False,
                timeout=job.timeout_s,
            )
            local_cleanup = job.kwargs.get("local_branch_cleanup")
            if local_cleanup is not None:
                if not isinstance(local_cleanup, dict):
                    return JobResult(ok=False, error="local branch cleanup receipt is invalid")
                cleanup_branch = local_cleanup.get("branch")
                expected_sha = str(local_cleanup.get("base_sha") or "")
                if (
                    not isinstance(cleanup_branch, str)
                    or cleanup_branch != expected_branch
                    or not _is_full_commit_sha(expected_sha)
                ):
                    return JobResult(ok=False, error="local branch cleanup receipt is invalid")
                deleted = delete_local_branch_if_unchanged(
                    cleanup_branch,
                    expected_sha,
                    repo_root,
                    timeout=job.timeout_s,
                )
                return JobResult(ok=True, value={"local_branch_deleted": deleted})
        return JobResult(ok=True)
    fallback_root = Path(str(job.kwargs.get("repo_root") or get_repo_root()))
    fallback_kwargs = dict(job.kwargs)
    fallback_kwargs.pop("repo_root", None)
    worktree_manager_type(repo_root=fallback_root).remove_worktree(
        **fallback_kwargs,
        timeout=job.timeout_s,
    )
    return JobResult(ok=True)
