#!/usr/bin/env python3
"""Merge open PRs with successful CI/CD using GitHub API.

Supports dry-run mode and can detect repository name from git remote.

Usage:
    python -m hephaestus.github.pr_merge [--dry-run] [--push-all] [--repo OWNER/REPO]

Flags:
    --dry-run    Print git and API actions without executing them
    --push-all   Push every PR head branch to origin before attempting merge
    --repo       Repository in format OWNER/REPO (auto-detected from git remote if not provided)

Requires:
    - PyGithub (pip install PyGithub)
    - Git installed locally
    - GITHUB_TOKEN environment variable with 'repo' scope
"""

import argparse
import os
import re
import subprocess
import sys
from typing import Any

from hephaestus.logging.utils import get_logger
from hephaestus.utils.helpers import run_subprocess

logger = get_logger(__name__)


def detect_repo_from_remote() -> str | None:
    """Detect repository name from git remote.

    Returns:
        Repository in format 'owner/repo' or None if not detected

    """
    try:
        result = run_subprocess(["git", "remote", "get-url", "origin"])
        remote_url = result.stdout.strip()

        # Parse github.com:owner/repo.git or https://github.com/owner/repo.git
        patterns = [
            r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$",
            r"github\.com/([^/]+)/([^/]+?)(?:\.git)?$",
        ]

        for pattern in patterns:
            match = re.search(pattern, remote_url)
            if match:
                owner, repo = match.groups()
                return f"{owner}/{repo}"

        logger.warning("Could not parse GitHub repo from remote URL: %s", remote_url)
        return None

    except Exception as e:  # broad catch intentional: git subprocess can fail in many ways
        logger.warning("Could not detect repo from git remote: %s", e)
        return None


def run_git_cmd(cmd: list[str], dry_run: bool = False, cwd: str | None = None) -> None:
    """Run a git command with dry-run support.

    Args:
        cmd: Command and arguments
        dry_run: If True, only print the command
        cwd: Working directory

    """
    logger.info("$ %s", " ".join(cmd))
    run_subprocess(cmd, cwd=cwd, dry_run=dry_run)


def checks_success_and_print(commit: Any) -> tuple[bool | None, list[Any]]:
    """Check if commit has successful CI/CD checks.

    Args:
        commit: GitHub commit object

    Returns:
        Tuple of (success status, checks list) or (None, []) if no check runs present

    """
    try:
        checks = list(commit.get_check_runs())
    except Exception as e:  # broad catch intentional: PyGithub raises many exception subtypes
        logger.error("Error getting check runs: %s", e)
        return None, []

    bad = {"failure", "timed_out", "cancelled", "action_required"}
    any_success = False

    if checks:
        for cr in checks:
            logger.info("    - %s: status=%s, conclusion=%s", cr.name, cr.status, cr.conclusion)
            if cr.status != "completed":
                return False, checks
            if cr.conclusion in bad:
                return False, checks
            if cr.conclusion == "success":
                any_success = True
        return any_success, checks

    return None, []


def legacy_status_and_print(commit: Any) -> str:
    """Get legacy commit status and print contexts.

    Args:
        commit: GitHub commit object

    Returns:
        Combined status state

    """
    try:
        combined = commit.get_combined_status()
        for ctx in combined.statuses:
            logger.info(
                "    - %s: state=%s, description=%s", ctx.context, ctx.state, ctx.description
            )
        return combined.state or "unknown"
    except Exception as e:  # broad catch intentional: PyGithub raises many exception subtypes
        logger.error("Error getting combined status: %s", e)
        return "unknown"


def local_branch_exists(branch_name: str) -> bool:
    """Check if a local branch exists.

    Args:
        branch_name: Name of branch to check

    Returns:
        True if branch exists locally

    """
    try:
        out = subprocess.check_output(
            ["git", "branch", "--list", branch_name], stderr=subprocess.DEVNULL
        )
        return bool(out.strip())
    except subprocess.CalledProcessError:
        return False


def try_push_head_branch(head_branch: str, dry_run: bool) -> None:
    """Push local head branch to origin if it exists locally.

    Args:
        head_branch: Branch name to push
        dry_run: If True, only print the action

    """
    if dry_run:
        logger.info(
            "[DRY-RUN] Would push local branch '%s' to origin if it exists locally.", head_branch
        )
        return

    if local_branch_exists(head_branch):
        run_git_cmd(["git", "push", "origin", f"{head_branch}:{head_branch}"], dry_run=False)
    else:
        logger.info(
            "  Local branch '%s' not found; assuming remote branch already present.", head_branch
        )


def handle_merge_result(result: Any, pr_number: int, base_branch: str) -> None:
    """Handle and log the result of a PR merge.

    Args:
        result: Merge result object from PyGithub
        pr_number: PR number
        base_branch: Base branch name

    """
    try:
        merged = getattr(result, "merged", None)
        message = getattr(result, "message", None)
        sha = getattr(result, "sha", None)
    except AttributeError:
        # Fallback for unexpected types
        merged = False
        message = str(result)
        sha = None

    if merged:
        logger.info("  PR #%d merged into %s via rebase. sha=%s", pr_number, base_branch, sha)
    else:
        logger.error("  Failed to merge PR #%d. API message: %s", pr_number, message)


def main() -> None:  # noqa: C901
    """Serve as the main entry point for PR merge automation."""
    parser = argparse.ArgumentParser(
        description="Merge open PRs with successful CI/CD into main (rebase via PR API)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands and API actions without executing",
    )
    parser.add_argument(
        "--push-all",
        action="store_true",
        help="Push all PR head branches to origin even if CI/CD failed",
    )
    parser.add_argument(
        "--repo",
        help="Repository in format OWNER/REPO (auto-detected if not provided)",
    )
    args = parser.parse_args()

    # Get GitHub token
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        logger.error(
            "Please set GITHUB_TOKEN environment variable with a token that has 'repo' scope."
        )
        sys.exit(1)

    # Detect or use provided repo name
    repo_name = args.repo or detect_repo_from_remote()
    if not repo_name:
        logger.error(
            "Could not detect repository name. Please provide --repo OWNER/REPO or "
            "ensure you're in a git repository with a GitHub remote."
        )
        sys.exit(1)

    logger.info("Working with repository: %s", repo_name)

    # Import PyGithub
    try:
        from github import Github
    except ImportError:
        logger.error(
            "PyGithub not installed. Install with: pip install PyGithub\n"
            "Or install hephaestus with github extras: pip install hephaestus[github]"
        )
        sys.exit(1)

    # Connect to GitHub
    gh = Github(token)
    try:
        repo = gh.get_repo(repo_name)
    except Exception as e:  # broad catch intentional: PyGithub raises many exception subtypes
        logger.error("Error accessing repo %s: %s", repo_name, e)
        sys.exit(1)

    # Update local main
    logger.info("Updating local 'main'...")
    run_git_cmd(["git", "checkout", "main"], dry_run=args.dry_run)
    run_git_cmd(["git", "pull", "origin", "main"], dry_run=args.dry_run)

    # Process open PRs
    for pr in repo.get_pulls(state="open", sort="created"):
        head_branch = pr.head.ref
        base_branch = pr.base.ref
        logger.info("\nChecking PR #%d: %s -> %s", pr.number, head_branch, base_branch)

        try:
            commit = repo.get_commit(pr.head.sha)
        except Exception as e:  # broad catch intentional: PyGithub raises many exception subtypes
            logger.error("  Unable to retrieve head commit for PR #%d: %s", pr.number, e)
            continue

        logger.info("  Checks API results:")
        success, _checks = checks_success_and_print(commit)

        if success is None:
            logger.info("  No check runs found; falling back to legacy status contexts:")
            state = legacy_status_and_print(commit)
            success = state == "success"

        # Handle push-all flag
        if args.push_all:
            logger.info("  Pushing head branch '%s' (--push-all mode)...", head_branch)
            try_push_head_branch(head_branch, args.dry_run)

        # Merge if checks passed
        if success:
            logger.info("  CI/CD checks passed for PR #%d. Attempting merge...", pr.number)
            if not args.push_all:
                try_push_head_branch(head_branch, args.dry_run)

            if args.dry_run:
                logger.info("[DRY-RUN] Would merge PR #%d via rebase", pr.number)
            else:
                try:
                    result = pr.merge(merge_method="rebase")
                    handle_merge_result(result, pr.number, base_branch)
                # broad catch intentional: PyGithub raises many exception subtypes
                except Exception as e:
                    logger.error("  Error merging PR #%d: %s", pr.number, e)
        else:
            logger.warning("  CI/CD checks not successful for PR #%d. Skipping merge.", pr.number)

    logger.info("\nDone processing all open PRs.")


if __name__ == "__main__":
    main()
