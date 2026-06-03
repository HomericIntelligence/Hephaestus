"""Pull request management functions for issue implementation.

Provides:
- Committing changes with secret file filtering
- Ensuring PR is created (fallback when Claude doesn't do it)
- Creating pull requests via GitHub CLI
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import cast

from hephaestus.agents.runtime import is_codex

from ._secret_patterns import SECRET_FILE_EXTENSIONS, SECRET_FILE_NAMES
from .claude_models import implementer_model
from .git_utils import issue_ref, run
from .github_api import (
    _gh_call,
    fetch_issue_info,
    gh_issue_add_labels,
    gh_issue_remove_labels,
    gh_pr_create,
)
from .prompts import get_pr_description
from .state_labels import (
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
    is_implementation_go,
)
from .status_tracker import StatusTracker

logger = logging.getLogger(__name__)


def _agent_display_name(agent: str) -> str:
    """Return a short human-facing name for generated commits/PR bodies."""
    return "Codex" if is_codex(agent) else "Claude Code"


def _coauthor_for_agent(agent: str) -> tuple[str, str]:
    """Return the co-author identity for fallback commits made by automation."""
    if is_codex(agent):
        return ("Codex", "noreply@openai.com")
    return (implementer_model(), "noreply@anthropic.com")


def _detect_default_base_branch(worktree_path: Path) -> str:
    """Return the repo default branch name for PR/signature ranges."""
    result = run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
        cwd=worktree_path,
        capture_output=True,
        check=False,
    )
    ref = (result.stdout or "").strip()
    if ref.startswith("origin/") and len(ref) > len("origin/"):
        return ref.removeprefix("origin/")
    return "main"


def _pr_auto_merge_enabled(pr_number: int) -> bool:
    """Return whether GitHub currently has auto-merge armed for a PR."""
    result = _gh_call(["pr", "view", str(pr_number), "--json", "autoMergeRequest"])
    data = json.loads(result.stdout or "{}")
    return data.get("autoMergeRequest") is not None


def ensure_pr_auto_merge_deferred(pr_number: int) -> None:
    """Disable premature auto-merge before implementation review reaches GO."""
    if not _pr_auto_merge_enabled(pr_number):
        return
    _gh_call(["pr", "merge", str(pr_number), "--disable-auto"])
    logger.warning(
        "Disabled premature auto-merge for PR #%s; implementation review has not reached GO yet",
        pr_number,
    )


def mark_pr_implementation_go(pr_number: int) -> None:
    """Mark a PR as implementation-review GO."""
    gh_issue_add_labels(pr_number, [STATE_IMPLEMENTATION_GO])
    gh_issue_remove_labels(pr_number, [STATE_IMPLEMENTATION_NO_GO])


def mark_pr_implementation_no_go(pr_number: int) -> None:
    """Mark a PR as implementation-review not-GO."""
    gh_issue_add_labels(pr_number, [STATE_IMPLEMENTATION_NO_GO])
    gh_issue_remove_labels(pr_number, [STATE_IMPLEMENTATION_GO])


def pr_has_implementation_go_label(pr: dict[str, object]) -> bool:
    """Return True when a PR dictionary carries the implementation-GO label."""
    labels = pr.get("labels")
    if not isinstance(labels, list):
        return False
    names: list[str] = []
    for label in labels:
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                names.append(name)
    return is_implementation_go(names)


def enable_auto_merge_after_implementation_go(pr_number: int) -> None:
    """Arm auto-merge after implementation review has labeled the PR GO."""
    _gh_call(["pr", "merge", str(pr_number), "--auto", "--squash"])
    logger.info("Enabled auto-merge for implementation-GO PR #%s", pr_number)


def commit_changes(issue_number: int, worktree_path: Path, agent: str = "claude") -> None:
    """Commit changes in worktree, filtering out secret files.

    Args:
        issue_number: Issue number (used in commit message and error text)
        worktree_path: Path to git worktree
        agent: Selected implementation agent. Defaults to Claude for backwards
            compatibility with existing direct callers.

    Raises:
        RuntimeError: If there are no changes, or all changes are secret files.

    """
    # Check if there are changes
    result = run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
        capture_output=True,
    )

    if not result.stdout.strip():
        raise RuntimeError(
            f"No changes to commit for issue {issue_ref(issue_number)}. "
            "Check if the implementation was successful or if the plan needs revision."
        )

    # Parse git status --porcelain output to get all changed files
    # Format: XY filename or XY "quoted filename" for special chars
    # X = index status, Y = worktree status
    # Common codes: M (modified), A (added), D (deleted), R (renamed), ?? (untracked)
    files_to_add = []

    for line in result.stdout.splitlines():
        if not line:
            continue

        # Parse status code and filename
        # Format: "XY filename" where X and Y are status codes
        # Position 0-1: status codes, position 2: space, position 3+: filename
        status = line[:2]
        filename_part = line[3:]  # Don't strip - filename starts at position 3

        # Handle renamed files (format: "old -> new")
        if status.startswith("R") and " -> " in filename_part:
            filename_part = filename_part.split(" -> ", 1)[1]

        # Handle quoted filenames (git quotes names with special chars)
        if filename_part.startswith('"') and filename_part.endswith('"'):
            # Remove quotes - git uses C-style escaping
            filename_part = filename_part[1:-1]

        # Check if file is a potential secret
        filename = Path(filename_part).name

        # Skip secret files (never stage these)
        has_secret_ext = any(filename.endswith(ext) for ext in SECRET_FILE_EXTENSIONS)
        if filename in SECRET_FILE_NAMES or has_secret_ext:
            logger.warning("Skipping potential secret file: %s", filename_part)
            continue

        files_to_add.append(filename_part)

    if not files_to_add:
        raise RuntimeError(
            f"No non-secret files to commit for issue {issue_ref(issue_number)}. "
            "All changes appear to be secret files."
        )

    # Stage the files
    run(["git", "add", *files_to_add], cwd=worktree_path)

    # Generate commit message
    issue = fetch_issue_info(issue_number)
    coauthor_name, coauthor_email = _coauthor_for_agent(agent)
    commit_msg = f"""feat: Implement #{issue_number}

{issue.title}

Closes #{issue_number}

Co-Authored-By: {coauthor_name} <{coauthor_email}>
"""

    # Commit
    run(
        ["git", "commit", "-m", commit_msg],
        cwd=worktree_path,
    )


def ensure_pr_created(
    issue_number: int,
    branch_name: str,
    worktree_path: Path,
    auto_merge: bool = False,
    status_tracker: StatusTracker | None = None,
    slot_id: int | None = None,
    agent: str = "claude",
) -> int:
    """Ensure commit is pushed and PR is created (fallback if Claude didn't do it).

    Args:
        issue_number: Issue number
        branch_name: Git branch name
        worktree_path: Path to worktree
        auto_merge: Whether the caller eventually wants auto-merge. The PR is
            still created with auto-merge disabled until implementation review GO.
        status_tracker: StatusTracker instance for slot updates (optional)
        slot_id: Worker slot ID for status updates
        agent: Selected implementation agent for generated PR metadata.

    Returns:
        PR number

    Raises:
        RuntimeError: If commit doesn't exist or PR creation fails

    """

    def _update_slot(msg: str) -> None:
        if slot_id is not None and status_tracker is not None:
            status_tracker.update_slot(slot_id, msg)

    # Check if commit exists
    _update_slot(f"{issue_ref(issue_number)}: Checking commit")
    result = run(
        ["git", "log", "-1", "--oneline"],
        cwd=worktree_path,
        capture_output=True,
    )
    if not result.stdout.strip():
        raise RuntimeError(
            f"No commit found for issue {issue_ref(issue_number)}. "
            "Claude did not create any commits."
        )

    logger.info("Commit exists: %s", result.stdout.strip()[:80])

    # Check if branch was pushed, if not push it
    _update_slot(f"{issue_ref(issue_number)}: Pushing branch")
    result = run(
        ["git", "ls-remote", "--heads", "origin", branch_name],
        cwd=worktree_path,
        capture_output=True,
        check=False,
    )
    if not result.stdout.strip():
        logger.warning("Branch %s not pushed, pushing now...", branch_name)
        run(["git", "push", "-u", "origin", branch_name], cwd=worktree_path)
        logger.info("Pushed branch %s to origin", branch_name)
    else:
        logger.info("Branch %s already on origin", branch_name)

    # Check if PR exists, if not create it
    _update_slot(f"{issue_ref(issue_number)}: Creating PR")
    pr_number = None
    try:
        result = _gh_call(["pr", "list", "--head", branch_name, "--json", "number", "--limit", "1"])
        pr_data = json.loads(result.stdout)
        if pr_data and len(pr_data) > 0:
            pr_number = cast(int, pr_data[0]["number"])
            logger.info("PR #%s already exists", pr_number)
            return pr_number
    except Exception as e:  # broad catch: gh CLI + JSON parsing; fallback is to create PR
        logger.debug("Could not find existing PR: %s", e)

    # PR doesn't exist, create it
    logger.warning("No PR found for branch %s, creating one...", branch_name)
    if auto_merge:
        logger.info(
            "Deferring auto-merge for branch %s until implementation review marks the PR GO",
            branch_name,
        )
    base_branch = _detect_default_base_branch(worktree_path)
    pr_number = create_pr(
        issue_number,
        branch_name,
        auto_merge=False,
        agent=agent,
        base=base_branch,
    )
    logger.info("Created PR #%s", pr_number)
    return pr_number


def create_pr(
    issue_number: int,
    branch_name: str,
    auto_merge: bool = False,
    agent: str = "claude",
    base: str = "main",
) -> int:
    """Create pull request for issue.

    Args:
        issue_number: Issue number
        branch_name: Git branch name
        auto_merge: Whether to enable auto-merge on the PR
        agent: Selected implementation agent for generated PR metadata.

    Returns:
        PR number

    """
    issue = fetch_issue_info(issue_number)

    pr_title = f"feat: {issue.title}"
    pr_body = get_pr_description(
        issue_number=issue_number,
        summary=f"Implements #{issue_number}",
        changes=f"- Automated implementation via {_agent_display_name(agent)}",
        testing="- Automated tests included",
        generated_by=f"{_agent_display_name(agent)} via ProjectHephaestus automation.",
    )

    return gh_pr_create(
        branch=branch_name,
        title=pr_title,
        body=pr_body,
        auto_merge=auto_merge,
        base=base,
    )
