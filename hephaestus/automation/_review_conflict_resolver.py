"""Resolve an implementation PR's merge conflict before review begins."""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hephaestus.github.client import gh_call

from .git_utils import (
    get_repo_info,
    issue_ref,
    pr_ref,
    rebase_worktree_onto,
    sync_worktree_to_remote_branch,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConflictResolutionRequest:
    """Immutable context required to resolve a PR before it is reviewed."""

    issue_number: int
    pr_number: int
    worktree_path: Path
    branch_name: str
    slot_id: int | None
    thread_id: int | None


@dataclass(frozen=True)
class ConflictResolutionOperations:
    """GitHub and Git operations used by :class:`ReviewConflictResolver`."""

    merge_state: Callable[[int], tuple[str, str]]
    base_branch: Callable[[int], str]
    sync_worktree: Callable[[Path, str], None]
    rebase_worktree: Callable[[Path, str], bool]


def build_conflict_resolution_operations(
    repo_root: Path,
    *,
    github_call: Callable[..., Any] = gh_call,
    repo_info: Callable[[Path], tuple[str, str]] = get_repo_info,
    sync_worktree: Callable[[Path, str], None] = sync_worktree_to_remote_branch,
    rebase_worktree: Callable[[Path, str], bool] = rebase_worktree_onto,
) -> ConflictResolutionOperations:
    """Bind production GitHub and Git helpers behind narrow effect callbacks."""

    def _repo_name_with_owner() -> str:
        owner, repo = repo_info(repo_root)
        return f"{owner}/{repo}"

    def _merge_state(pr_number: int) -> tuple[str, str]:
        try:
            result = github_call(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    _repo_name_with_owner(),
                    "--json",
                    "mergeStateStatus,mergeable",
                ]
            )
            payload = json.loads(result.stdout or "{}")
            if not isinstance(payload, dict):
                return "", ""
        except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            logger.warning("Could not fetch %s merge state: %s", pr_ref(pr_number), exc)
            return "", ""
        return (
            str(payload.get("mergeStateStatus") or "").upper(),
            str(payload.get("mergeable") or "").upper(),
        )

    def _base_branch(pr_number: int) -> str:
        try:
            result = github_call(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    _repo_name_with_owner(),
                    "--json",
                    "baseRefName",
                ]
            )
            payload = json.loads(result.stdout or "{}")
            if not isinstance(payload, dict):
                return "main"
            return str(payload.get("baseRefName") or "main")
        except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            logger.debug("Could not fetch %s base branch: %s", pr_ref(pr_number), exc)
            return "main"

    return ConflictResolutionOperations(
        merge_state=_merge_state,
        base_branch=_base_branch,
        sync_worktree=sync_worktree,
        rebase_worktree=rebase_worktree,
    )


class ReviewConflictResolver:
    """Coordinate merge-conflict resolution through injected collaborators."""

    def __init__(
        self,
        *,
        dry_run: Callable[[], bool],
        operations: ConflictResolutionOperations,
        resume_implementation: Callable[[str], bool],
        commit_changes: Callable[[], bool],
        push_rebased_branch: Callable[[str, Path], None],
        push_agent_branch: Callable[[str, Path], None],
        log: Callable[[str, str, int | None], None],
        update_slot: Callable[[int, str], None],
    ) -> None:
        """Store the explicit effects owned by the review-phase facade."""
        self._dry_run = dry_run
        self._operations = operations
        self._resume_implementation = resume_implementation
        self._commit_changes = commit_changes
        self._push_rebased_branch = push_rebased_branch
        self._push_agent_branch = push_agent_branch
        self._log = log
        self._update_slot = update_slot

    def resolve(self, request: ConflictResolutionRequest) -> bool:
        """Return whether the PR is safe to send to the review loop.

        An unreadable initial GitHub state is fail-open so a transient read
        failure does not strand a healthy PR.  Once a conflict is confirmed,
        final readback is fail-closed.
        """
        if self._dry_run():
            return True
        if not self._is_conflicting(*self._operations.merge_state(request.pr_number)):
            return True
        self._report_conflict(request)
        base_branch = self._operations.base_branch(request.pr_number)
        if self._attempt_mechanical_resolution(request, base_branch):
            return True
        return self._attempt_agent_resolution(request, base_branch)

    def _report_conflict(self, request: ConflictResolutionRequest) -> None:
        """Publish the existing issue/PR context before mutation begins."""
        self._log(
            "warning",
            f"issue #{request.issue_number}: PR #{request.pr_number} has a "
            "merge conflict before review; resolving it first",
            request.thread_id,
        )
        if request.slot_id is not None:
            self._update_slot(request.slot_id, f"PR #{request.pr_number}: resolving merge conflict")

    def _attempt_mechanical_resolution(
        self,
        request: ConflictResolutionRequest,
        base_branch: str,
    ) -> bool:
        """Try the inexpensive rebase path before consuming an agent session."""
        try:
            self._operations.sync_worktree(request.worktree_path, request.branch_name)
            if not self._operations.rebase_worktree(request.worktree_path, base_branch):
                self._log(
                    "warning",
                    f"{pr_ref(request.pr_number)} mechanical rebase onto {base_branch} hit "
                    "conflicts; aborted; "
                    "deferring to implementation agent",
                    request.thread_id,
                )
                return False
            self._push_rebased_branch(request.branch_name, request.worktree_path)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self._log(
                "warning",
                f"{pr_ref(request.pr_number)} mechanical rebase failed: {exc}",
                request.thread_id,
            )
            return False
        return not self._is_conflicting(*self._operations.merge_state(request.pr_number))

    def _attempt_agent_resolution(
        self,
        request: ConflictResolutionRequest,
        base_branch: str,
    ) -> bool:
        """Ask the implementer to resolve a confirmed remaining conflict."""
        feedback = self._agent_feedback(base_branch)
        try:
            if self._resume_implementation(feedback) and self._commit_changes():
                self._push_agent_branch(request.branch_name, request.worktree_path)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self._log(
                "warning",
                f"{pr_ref(request.pr_number)} agent resolution failed: {exc}",
                request.thread_id,
            )
            return False
        if not self._is_conflicting(*self._operations.merge_state(request.pr_number)):
            return True
        self._log(
            "warning",
            f"issue {issue_ref(request.issue_number)}: {pr_ref(request.pr_number)} still has an "
            "unresolved merge conflict after resolution; skipping review",
            request.thread_id,
        )
        return False

    @staticmethod
    def _agent_feedback(base_branch: str) -> str:
        """Describe the exact merge-conflict action for the implementer session."""
        return (
            f"This PR has a MERGE CONFLICT with `origin/{base_branch}` and cannot merge. "
            "Rebase the PR head branch onto the base branch, preserve both the PR intent "
            "and the latest base changes, then commit the resolution."
        )

    @staticmethod
    def _is_conflicting(merge_state: str, mergeable: str) -> bool:
        """Return whether either GitHub merge-state field confirms a conflict."""
        return merge_state in {"DIRTY", "CONFLICTING"} or mergeable == "CONFLICTING"
