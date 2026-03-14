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

        logger.warning(f"Could not parse GitHub repo from remote URL: {remote_url}")
        return None

    except Exception as e:
        logger.warning(f"Could not detect repo from git remote: {e}")
        return None


def run_git_cmd(cmd: list[str], dry_run: bool = False, cwd: str | None = None) -> None:
    """Run a git command with dry-run support.

    Args:
        cmd: Command and arguments
        dry_run: If True, only print the command
        cwd: Working directory

    """
    if dry_run:
        logger.info(f"[DRY-RUN] $ {' '.join(cmd)}")
        return

    logger.info(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True, cwd=cwd)


def checks_success_and_print(commit) -> tuple[bool | None, list]:
    """Check if commit has successful CI/CD checks.

    Args:
        commit: GitHub commit object

    Returns:
        Tuple of (success status, checks list) or (None, []) if no check runs present

    """
    try:
        checks = list(commit.get_check_runs())
    except Exception as e:
        logger.error(f"Error getting check runs: {e}")
        return None, []

    bad = {"failure", "timed_out", "cancelled", "action_required"}
    any_success = False

    if checks:
        for cr in checks:
            logger.info(f"    - {cr.name}: status={cr.status}, conclusion={cr.conclusion}")
            if cr.status != "completed":
                return False, checks
            if cr.conclusion in bad:
                return False, checks
            if cr.conclusion == "success":
                any_success = True
        return any_success, checks

    return None, []


def legacy_status_and_print(commit) -> str:
    """Get legacy commit status and print contexts.

    Args:
        commit: GitHub commit object

    Returns:
        Combined status state

    """
    try:
        combined = commit.get_combined_status()
        for ctx in combined.statuses:
            logger.info(f"    - {ctx.context}: state={ctx.state}, description={ctx.description}")
        return combined.state or "unknown"
    except Exception as e:
        logger.error(f"Error getting combined status: {e}")
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
            ["git", "branch", "--list", branch_name],
            stderr=subprocess.DEVNULL
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
        logger.info(f"[DRY-RUN] Would push local branch '{head_branch}' to origin if it exists locally.")
        return

    if local_branch_exists(head_branch):
        run_git_cmd(["git", "push", "origin", f"{head_branch}:{head_branch}"], dry_run=False)
    else:
        logger.info(f"  Local branch '{head_branch}' not found; assuming remote branch already present.")


def handle_merge_result(result, pr_number: int, base_branch: str) -> None:
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
    except Exception:
        # Fallback for unexpected types
        merged = False
        message = str(result)
        sha = None

    if merged:
        logger.info(f"  🎉 PR #{pr_number} merged into {base_branch} via rebase. sha={sha}")
    else:
        logger.error(f"  Failed to merge PR #{pr_number}. API message: {message}")


def main():
    """Main entry point for PR merge automation."""
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
        logger.error("Please set GITHUB_TOKEN environment variable with a token that has 'repo' scope.")
        sys.exit(1)

    # Detect or use provided repo name
    repo_name = args.repo or detect_repo_from_remote()
    if not repo_name:
        logger.error(
            "Could not detect repository name. Please provide --repo OWNER/REPO or "
            "ensure you're in a git repository with a GitHub remote."
        )
        sys.exit(1)

    logger.info(f"Working with repository: {repo_name}")

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
    except Exception as e:
        logger.error(f"Error accessing repo {repo_name}: {e}")
        sys.exit(1)

    # Update local main
    logger.info("Updating local 'main'...")
    run_git_cmd(["git", "checkout", "main"], dry_run=args.dry_run)
    run_git_cmd(["git", "pull", "origin", "main"], dry_run=args.dry_run)

    # Process open PRs
    for pr in repo.get_pulls(state="open", sort="created"):
        head_branch = pr.head.ref
        base_branch = pr.base.ref
        logger.info(f"\nChecking PR #{pr.number}: {head_branch} -> {base_branch}")

        try:
            commit = repo.get_commit(pr.head.sha)
        except Exception as e:
            logger.error(f"  Unable to retrieve head commit for PR #{pr.number}: {e}")
            continue

        logger.info("  Checks API results:")
        success, checks = checks_success_and_print(commit)

        if success is None:
            logger.info("  No check runs found; falling back to legacy status contexts:")
            state = legacy_status_and_print(commit)
            success = (state == "success")

        # Handle push-all flag
        if args.push_all:
            logger.info(f"  Pushing head branch '{head_branch}' (--push-all mode)...")
            try_push_head_branch(head_branch, args.dry_run)

        # Merge if checks passed
        if success:
            logger.info(f"  CI/CD checks passed for PR #{pr.number}. Attempting merge...")
            if not args.push_all:
                try_push_head_branch(head_branch, args.dry_run)

            if args.dry_run:
                logger.info(f"[DRY-RUN] Would merge PR #{pr.number} via rebase")
            else:
                try:
                    result = pr.merge(merge_method="rebase")
                    handle_merge_result(result, pr.number, base_branch)
                except Exception as e:
                    logger.error(f"  Error merging PR #{pr.number}: {e}")
        else:
            logger.warning(f"  CI/CD checks not successful for PR #{pr.number}. Skipping merge.")

    logger.info("\nDone processing all open PRs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
