"""Shared low-level cleanup operations for both pipeline worker lanes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.git_utils import (
    delete_local_branch_if_unchanged,
    delete_reserved_branch_if_unchanged,
    run,
)
from hephaestus.automation.source_worktree import SourceWorkspaceError, SourceWorkspaceManager
from hephaestus.automation.worktree_manager import WorktreeManager
from hephaestus.utils.file_lock import file_lock
from hephaestus.utils.helpers import get_repo_root
from hephaestus.utils.worktree_identity import is_expected_managed_worktree_path

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
    return is_expected_managed_worktree_path(
        path,
        repo_root=repo_root,
        issue_number=issue_number,
    )


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


def _ownership_changed(
    record: dict[str, str] | None,
    *,
    expected_branch: object,
    expected_head: object,
    expected_detached: bool,
) -> bool:
    """Return whether the registered worktree differs from its cleanup proof."""
    branch_changed = expected_branch is not None and (
        not isinstance(expected_branch, str)
        or not expected_branch
        or record is None
        or record.get("branch") != f"refs/heads/{expected_branch}"
    )
    head_changed = expected_head is not None and (
        not _is_full_commit_sha(expected_head)
        or record is None
        or record.get("commit") != expected_head
    )
    detached_changed = expected_detached and (record is None or bool(record.get("branch")))
    return branch_changed or head_changed or detached_changed


def _run_source_lane_cleanup(
    job: GitJob,
    *,
    worktree_path: Path,
    repo_root: Path,
    issue_number: int,
    expected_head: object,
    expected_detached: bool,
    worktree_manager_type: Any,
    remote_env: dict[str, str] | None,
    remote_config: tuple[str, ...],
    revalidate_remote: Callable[[], tuple[dict[str, str], tuple[str, ...]]] | None,
) -> JobResult:
    """Clean one receipt-owned source lane through its state owner."""
    try:
        source_lane = SourceLane(job.kwargs["source_lane"])
    except (KeyError, TypeError, ValueError):
        return JobResult(ok=False, error="source worktree cleanup lane is invalid")
    if issue_number < 1 or not _is_full_commit_sha(expected_head) or not expected_detached:
        return JobResult(ok=False, error="source worktree cleanup proof is invalid")
    manager = SourceWorkspaceManager(repo_root, repository=job.repo)
    if worktree_path.resolve() != manager.path_for(issue_number, source_lane).resolve():
        return JobResult(ok=False, error="source worktree cleanup identity is invalid")

    def physical_cleanup() -> None:
        generic_kwargs = dict(job.kwargs)
        generic_kwargs.pop("source_lane", None)
        result = run_cleanup_job(
            GitJob(
                repo=job.repo,
                op=job.op,
                timeout_s=job.timeout_s,
                kwargs=generic_kwargs,
                descr=job.descr,
            ),
            worktree_manager_type=worktree_manager_type,
            remote_env=remote_env,
            remote_config=remote_config,
            revalidate_remote=revalidate_remote,
        )
        if not result.ok:
            raise SourceWorkspaceError(result.error or "worktree cleanup failed")

    try:
        manager.cleanup(
            issue_number,
            source_lane,
            expected_revision=str(expected_head),
            expected_detached=expected_detached,
            physical_cleanup=physical_cleanup,
        )
    except SourceWorkspaceError as exc:
        return JobResult(ok=False, error=str(exc))
    return JobResult(ok=True)


def _release_branch_reservation(
    job: GitJob,
    *,
    remote_env: dict[str, str] | None,
    remote_config: tuple[str, ...],
    revalidate_remote: Callable[[], tuple[dict[str, str], tuple[str, ...]]] | None,
) -> JobResult:
    """Release one reserved branch after validating its immutable base."""
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
        env=remote_env,
        remote_config=remote_config,
        revalidate_remote=revalidate_remote,
    )
    return JobResult(ok=True, value=released)


def run_cleanup_job(
    job: GitJob,
    *,
    worktree_manager_type: Any = WorktreeManager,
    remote_env: dict[str, str] | None = None,
    remote_config: tuple[str, ...] = (),
    revalidate_remote: Callable[[], tuple[dict[str, str], tuple[str, ...]]] | None = None,
) -> JobResult:
    """Run one validated worktree or reservation cleanup operation."""
    if job.op == "release_branch_reservation":
        return _release_branch_reservation(
            job,
            remote_env=remote_env,
            remote_config=remote_config,
            revalidate_remote=revalidate_remote,
        )

    if job.op != "remove_worktree":
        raise TypeError(f"unsupported cleanup Git operation: {job.op}")
    if job.kwargs.get("worktree_path"):
        worktree_path = Path(str(job.kwargs["worktree_path"]))
        repo_root = Path(str(job.kwargs.get("repo_root") or get_repo_root()))
        issue_number = job.kwargs.get("issue_number")
        expected_branch = job.kwargs.get("expected_branch")
        expected_head = job.kwargs.get("expected_head")
        expected_detached = job.kwargs.get("expected_detached", False)
        if (
            isinstance(issue_number, bool)
            or not isinstance(issue_number, int)
            or not _is_expected_worktree_path(
                worktree_path,
                repo_root=repo_root,
                issue_number=issue_number,
            )
            or (expected_branch is None and expected_head is None)
            or not isinstance(expected_detached, bool)
        ):
            return JobResult(ok=False, error="worktree cleanup identity is invalid")
        if job.kwargs.get("source_lane") is not None:
            return _run_source_lane_cleanup(
                job,
                worktree_path=worktree_path,
                repo_root=repo_root,
                issue_number=issue_number,
                expected_head=expected_head,
                expected_detached=expected_detached,
                worktree_manager_type=worktree_manager_type,
                remote_env=remote_env,
                remote_config=remote_config,
                revalidate_remote=revalidate_remote,
            )
        with file_lock(worktree_manager_type.git_metadata_lock_path(repo_root)):
            record = _worktree_record(
                worktree_path,
                repo_root=repo_root,
                timeout=job.timeout_s,
                worktree_manager_type=worktree_manager_type,
            )
            if record is None and not worktree_path.exists():
                return JobResult(ok=True)
            if _ownership_changed(
                record,
                expected_branch=expected_branch,
                expected_head=expected_head,
                expected_detached=expected_detached,
            ):
                return JobResult(ok=False, error="worktree cleanup ownership changed")
            status = run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=worktree_path,
                timeout=job.timeout_s,
            )
            if status.stdout.strip():
                return JobResult(
                    ok=False,
                    error="worktree cleanup refused a dirty checkout",
                )
            command = ["git", "worktree", "remove", str(worktree_path)]
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
