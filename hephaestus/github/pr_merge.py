#!/usr/bin/env python3
"""Merge open PRs with successful CI/CD using the shared gh adapter.

Supports dry-run mode and can detect repository name from git remote.

Usage:
    python -m hephaestus.github.pr_merge [--dry-run] [--push-all] [--repo OWNER/REPO]

Flags:
    --dry-run    Print git and API actions without executing them
    --push-all   Push every PR head branch to origin before attempting merge
    --repo       Repository in format OWNER/REPO (auto-detected from git remote if not provided)

Requires:
    - gh authenticated for the target repository
    - Git installed locally
"""

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from hephaestus.cli.utils import (
    add_github_throttle_args,
    add_json_arg,
    add_version_arg,
    configure_github_throttle_from_args,
    emit_json_status,
)
from hephaestus.github.client import gh_call
from hephaestus.github.git_ops import git_branch_exists, git_push, git_remote_url, run_git
from hephaestus.logging.utils import get_logger

logger = get_logger(__name__)


class _MergeStatus(StrEnum):
    """Terminal state for one requested pull request."""

    MERGED = "merged"
    QUEUED = "queued"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


def _escape_summary_detail(value: object) -> str:
    """Return printable single-line text for logs and structured output."""
    return ascii(str(value))[1:-1]


@dataclass(frozen=True)
class _PrMergeOutcome:
    """Describe the terminal result of processing one pull request."""

    pr_number: int | None
    status: _MergeStatus
    detail: str

    def __post_init__(self) -> None:
        """Normalize untrusted provider text before output."""
        object.__setattr__(self, "detail", _escape_summary_detail(self.detail))

    def as_dict(self) -> dict[str, int | str | None]:
        """Return a JSON-serializable representation of this outcome."""
        return {
            "pr_number": self.pr_number,
            "status": self.status.value,
            "detail": self.detail,
        }


def choose_merge_flag(repository_settings: Mapping[str, Any]) -> str | None:
    """Return the preferred permitted manual ``gh pr merge`` flag.

    The GitHub repository payload returned by ``gh api repos/OWNER/REPO``
    exposes the three ``allow_*`` settings.  Keep strategy selection here so
    Python callers do not need to source a shell helper; callers retain
    ownership of the actual merge operation.
    """
    for setting, flag in (
        ("allow_rebase_merge", "--rebase"),
        ("allow_squash_merge", "--squash"),
        ("allow_merge_commit", "--merge"),
    ):
        if repository_settings.get(setting):
            return flag
    return None


def detect_repo_from_remote() -> str | None:
    """Detect repository name from git remote.

    Returns:
        Repository in format 'owner/repo' or None if not detected

    """
    try:
        remote_url = git_remote_url()
        if not remote_url:
            logger.warning("Could not read GitHub repo from origin remote")
            return None

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
    if cwd is not None:
        logger.info("$ %s (cwd=%s)", " ".join(cmd), cwd)
    else:
        logger.info("$ %s", " ".join(cmd))
    run_git(cmd, cwd=cwd, dry_run=dry_run)


def checks_success_and_log(commit: Any) -> tuple[bool | None, list[Any]]:
    """Check if commit has successful CI/CD checks.

    Args:
        commit: GitHub commit object

    Returns:
        Tuple of (success status, checks list) or (None, []) if no check runs present

    """
    try:
        checks = list(commit.get_check_runs())
    except Exception as e:  # broad catch intentional: git remote detection can fail in many ways
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


def legacy_status_and_log(commit: Any) -> str:
    """Get legacy commit status and log contexts via logger.info.

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
    except Exception as e:  # broad catch retained for legacy object-style helper compatibility
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
        return git_branch_exists(branch_name)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
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
        git_push(None, "origin", f"{head_branch}:{head_branch}", dry_run=False)
    else:
        logger.info(
            "  Local branch '%s' not found; assuming remote branch already present.", head_branch
        )


def handle_merge_result(result: Any, pr_number: int, base_branch: str) -> _PrMergeOutcome:
    """Classify the result of a PR merge.

    Args:
        result: Merge result dict from gh api, or a legacy object-style result
        pr_number: PR number
        base_branch: Base branch name

    Returns:
        The terminal provider outcome for the pull request.

    """
    queued = False
    if isinstance(result, dict):
        merged = result.get("merged")
        message = result.get("message")
        sha = result.get("sha")
        queued = bool(result.get("queued"))
    else:
        try:
            merged = getattr(result, "merged", None)
            message = getattr(result, "message", None)
            sha = getattr(result, "sha", None)
        except AttributeError:
            merged = False
            message = str(result)
            sha = None

    if merged:
        return _PrMergeOutcome(
            pr_number,
            _MergeStatus.MERGED,
            f"merged into {base_branch} via squash; sha={sha}",
        )
    if queued:
        return _PrMergeOutcome(
            pr_number,
            _MergeStatus.QUEUED,
            f"enqueued in the {base_branch} merge queue; {message}",
        )
    return _PrMergeOutcome(
        pr_number,
        _MergeStatus.FAILED,
        f"merge API did not merge PR: {message}",
    )


def _gh_json(args: list[str]) -> Any:
    """Run gh through the shared adapter and parse JSON stdout."""
    result = gh_call(args)
    return json.loads(result.stdout or "null")


def _verify_repo_access(repo_name: str) -> bool:
    """Return whether gh can read the target repository."""
    try:
        _gh_json(["repo", "view", repo_name, "--json", "nameWithOwner"])
    except (subprocess.CalledProcessError, RuntimeError, json.JSONDecodeError) as exc:
        logger.error("Error accessing repo %s: %s", repo_name, exc)
        return False
    return True


def _list_open_prs(repo_name: str) -> list[dict[str, Any]]:
    """Return open PR metadata in oldest-created order."""
    data = _gh_json(
        [
            "pr",
            "list",
            "--repo",
            repo_name,
            "--state",
            "open",
            "--limit",
            "1000",
            "--json",
            "number,headRefName,headRefOid,baseRefName",
        ]
    )
    return data if isinstance(data, list) else []


_CHECK_BAD_BUCKETS = {"fail", "cancel"}


def _checks_success_and_log(repo_name: str, pr_number: int) -> bool | None:
    """Return PR checks success, false, or ``None`` when no checks exist."""
    try:
        checks = _gh_json(
            [
                "pr",
                "checks",
                str(pr_number),
                "--repo",
                repo_name,
                "--json",
                "name,state,bucket,workflow",
            ]
        )
    except subprocess.CalledProcessError as exc:
        blob = (exc.stderr or "") + (exc.stdout or "")
        if "no checks reported" in blob:
            return None
        logger.error("Error getting check runs for PR #%d: %s", pr_number, blob.strip() or exc)
        return None
    except (RuntimeError, json.JSONDecodeError) as exc:
        logger.error("Error getting check runs for PR #%d: %s", pr_number, exc)
        return None

    if not isinstance(checks, list) or not checks:
        return None

    any_success = False
    for check in checks:
        if not isinstance(check, dict):
            continue
        name = check.get("name", "")
        state = check.get("state", "")
        bucket = str(check.get("bucket", "")).lower()
        logger.info("    - %s: state=%s, bucket=%s", name, state, bucket)
        if bucket in _CHECK_BAD_BUCKETS:
            return False
        if bucket == "pending":
            return False
        if bucket == "pass":
            any_success = True
    return any_success


def _legacy_status_and_log(repo_name: str, head_sha: str) -> str:
    """Return combined legacy commit status for ``head_sha``."""
    try:
        payload = _gh_json(["api", f"/repos/{repo_name}/commits/{head_sha}/status"])
    except (subprocess.CalledProcessError, RuntimeError, json.JSONDecodeError) as exc:
        logger.error("Error getting combined status: %s", exc)
        return "unknown"
    if not isinstance(payload, dict):
        return "unknown"
    for ctx in payload.get("statuses") or []:
        if not isinstance(ctx, dict):
            continue
        logger.info(
            "    - %s: state=%s, description=%s",
            ctx.get("context", ""),
            ctx.get("state", ""),
            ctx.get("description", ""),
        )
    return str(payload.get("state") or "unknown")


# GitHub returns HTTP 405 with this message from the direct merge API when the
# base branch is protected by a merge queue: the PR cannot be merged directly and
# must be enqueued instead. Matched case-insensitively on the message substring so
# a wording change in the surrounding text does not break detection. Gated on the
# "(HTTP 405)" marker `gh` appends to stderr so an unrelated failure whose message
# happens to mention "merge queue" (e.g. in a PR title or comment echoed back by
# the API) is not misclassified as the queue-required case.
_MERGE_QUEUE_REQUIRED_MARKER = "merge queue"
_HTTP_405_MARKER = "(http 405)"


def _is_merge_queue_error(exc: subprocess.CalledProcessError) -> bool:
    """Return whether a failed direct-merge call means the repo uses a merge queue."""
    blob = f"{getattr(exc, 'stderr', '') or ''}{getattr(exc, 'stdout', '') or ''}".lower()
    return _MERGE_QUEUE_REQUIRED_MARKER in blob and _HTTP_405_MARKER in blob


def _enqueue_pr(repo_name: str, pr_number: int, head_sha: str) -> dict[str, Any]:
    """Add ``pr_number`` to the branch's merge queue via ``gh pr merge``.

    The merge queue owns the merge method and rebuilds/merges the PR server-side,
    so no ``--squash``/``--merge`` flag is passed (passing one is rejected on a
    queue-enabled branch). ``--match-head-commit`` preserves the direct-merge
    compare-and-swap guard, preventing a changed PR head from being enqueued.
    ``gh pr merge`` exits 0 both when it newly enqueues the PR and when the PR is
    already queued; both are success from our perspective.
    """
    result = gh_call(
        [
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            repo_name,
            "--match-head-commit",
            head_sha,
        ]
    )
    blob = f"{result.stdout or ''}{getattr(result, 'stderr', '') or ''}"
    return {"queued": True, "message": blob.strip() or "enqueued in merge queue"}


def _merge_pr(repo_name: str, pr_number: int, head_sha: str) -> dict[str, Any]:
    """Merge ``pr_number``, using the branch's merge queue when one is required.

    Tries a direct squash-merge first. If the base branch is protected by a merge
    queue, the direct merge API returns HTTP 405; in that case the PR is enqueued
    via ``gh pr merge`` and the result is reported as ``queued`` rather than an
    error.
    """
    try:
        payload = _gh_json(
            [
                "api",
                "-X",
                "PUT",
                f"/repos/{repo_name}/pulls/{pr_number}/merge",
                "-f",
                "merge_method=squash",
                "-f",
                f"sha={head_sha}",
            ]
        )
    except subprocess.CalledProcessError as exc:
        if _is_merge_queue_error(exc):
            return _enqueue_pr(repo_name, pr_number, head_sha)
        raise
    return payload if isinstance(payload, dict) else {"merged": False, "message": str(payload)}


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the PR merge CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Merge open PRs with successful CI/CD into main (squash via PR API)"
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
    add_github_throttle_args(parser)
    add_json_arg(parser)
    add_version_arg(parser)
    return parser


def _emit_pr_merge_error(json_output: bool, message: str = "setup failed") -> int:
    return _emit_merge_summary(
        [],
        requested=0,
        json_output=json_output,
        forced_exit_code=1,
        message=message,
    )


def _resolve_repo_name(repo_arg: str | None) -> str | None:
    repo_name = repo_arg or detect_repo_from_remote()
    if not repo_name:
        logger.error(
            "Could not detect repository name. Please provide --repo OWNER/REPO or "
            "ensure you're in a git repository with a GitHub remote."
        )
    return repo_name


def _update_main_branch(dry_run: bool) -> None:
    logger.info("Updating local 'main'...")
    run_git_cmd(["git", "checkout", "main"], dry_run=dry_run)
    run_git_cmd(["git", "pull", "origin", "main"], dry_run=dry_run)


def _list_open_prs_for_cli(repo_name: str) -> list[dict[str, Any]] | None:
    """Return open PRs, or None when the CLI should exit after a list failure."""
    try:
        return _list_open_prs(repo_name)
    except (subprocess.CalledProcessError, RuntimeError, json.JSONDecodeError) as exc:
        logger.error("Error listing open PRs for %s: %s", repo_name, exc)
        return None


def _checks_pass_or_legacy(repo_name: str, pr_number: int, head_sha: str) -> bool:
    logger.info("  Checks API results:")
    success = _checks_success_and_log(repo_name, pr_number)
    if success is not None:
        return success

    logger.info("  No check runs found; falling back to legacy status contexts:")
    return _legacy_status_and_log(repo_name, head_sha) == "success"


def _attempt_pr_merge(
    repo_name: str,
    pr_number: int,
    head_sha: str,
    base_branch: str,
    dry_run: bool,
) -> _PrMergeOutcome:
    if dry_run:
        logger.info("[DRY-RUN] Would merge PR #%d via squash", pr_number)
        return _PrMergeOutcome(
            pr_number,
            _MergeStatus.SKIPPED,
            "dry-run suppressed merge",
        )

    result = _merge_pr(repo_name, pr_number, head_sha)
    return handle_merge_result(result, pr_number, base_branch)


def _process_pr(
    repo_name: str,
    pr: dict[str, Any],
    push_all: bool,
    dry_run: bool,
) -> _PrMergeOutcome:
    pr_number = int(pr["number"])
    head_branch = str(pr.get("headRefName") or "")
    head_sha = str(pr.get("headRefOid") or "")
    base_branch = str(pr.get("baseRefName") or "main")
    if not head_sha:
        return _PrMergeOutcome(
            pr_number,
            _MergeStatus.FAILED,
            "head commit is missing",
        )

    logger.info("\nChecking PR #%d: %s -> %s", pr_number, head_branch, base_branch)
    success = _checks_pass_or_legacy(repo_name, pr_number, head_sha)

    if push_all:
        logger.info("  Pushing head branch '%s' (--push-all mode)...", head_branch)
        try_push_head_branch(head_branch, dry_run)

    if not success:
        return _PrMergeOutcome(
            pr_number,
            _MergeStatus.BLOCKED,
            "CI/CD checks are not successful",
        )

    logger.info("  CI/CD checks passed for PR #%d. Attempting merge...", pr_number)
    if not push_all and not dry_run:
        try_push_head_branch(head_branch, dry_run)

    return _attempt_pr_merge(repo_name, pr_number, head_sha, base_branch, dry_run)


def _pr_number_or_none(pr: dict[str, Any]) -> int | None:
    """Return the PR number when the listing row contains a valid integer."""
    try:
        return int(pr["number"])
    except (KeyError, TypeError, ValueError):
        return None


def _process_open_prs(
    repo_name: str,
    prs: list[dict[str, Any]],
    push_all: bool,
    dry_run: bool,
) -> list[_PrMergeOutcome]:
    outcomes: list[_PrMergeOutcome] = []
    for pr in prs:
        try:
            outcomes.append(_process_pr(repo_name, pr, push_all=push_all, dry_run=dry_run))
        except KeyboardInterrupt:
            outcomes.append(
                _PrMergeOutcome(
                    _pr_number_or_none(pr),
                    _MergeStatus.INTERRUPTED,
                    "processing interrupted",
                )
            )
            break
        except Exception as exc:
            outcomes.append(
                _PrMergeOutcome(
                    _pr_number_or_none(pr),
                    _MergeStatus.FAILED,
                    f"{type(exc).__name__}: {exc}",
                )
            )
    return outcomes


def _merge_totals(outcomes: list[_PrMergeOutcome]) -> dict[str, int]:
    """Count outcomes by every supported terminal status."""
    totals = {status.value: 0 for status in _MergeStatus}
    for outcome in outcomes:
        totals[outcome.status.value] += 1
    return totals


def _merge_exit_code(
    outcomes: list[_PrMergeOutcome],
    requested: int,
    *,
    interrupted: bool = False,
) -> int:
    """Return the command exit status for a completed or interrupted batch."""
    statuses = {outcome.status for outcome in outcomes}
    if interrupted or _MergeStatus.INTERRUPTED in statuses:
        return 130
    if len(outcomes) != requested:
        return 1
    if statuses & {_MergeStatus.BLOCKED, _MergeStatus.FAILED}:
        return 1
    return 0


def _emit_merge_summary(
    outcomes: list[_PrMergeOutcome],
    requested: int,
    *,
    json_output: bool,
    interrupted: bool = False,
    forced_exit_code: int | None = None,
    message: str | None = None,
) -> int:
    """Log and optionally serialize the complete merge-batch summary."""
    totals = _merge_totals(outcomes)
    exit_code = (
        forced_exit_code
        if forced_exit_code is not None
        else _merge_exit_code(outcomes, requested, interrupted=interrupted)
    )

    for outcome in outcomes:
        logger.info(
            "pr_merge_result pr=%s status=%s detail=%s",
            outcome.pr_number,
            outcome.status.value,
            outcome.detail,
        )
    logger.info(
        "pr_merge_summary requested=%d processed=%d totals=%s exit_code=%d",
        requested,
        len(outcomes),
        totals,
        exit_code,
    )

    if json_output:
        emit_json_status(
            exit_code,
            message,
            results=[outcome.as_dict() for outcome in outcomes],
            totals=totals,
            requested=requested,
            processed=len(outcomes),
        )
    return exit_code


def main() -> int:
    """Serve as the main entry point for PR merge automation."""
    parser = _build_arg_parser()
    args = parser.parse_args()

    try:
        configure_github_throttle_from_args(args)
        repo_name = _resolve_repo_name(args.repo)
        if not repo_name:
            return _emit_pr_merge_error(args.json, "repository could not be resolved")

        logger.info("Working with repository: %s", repo_name)
        if not _verify_repo_access(repo_name):
            return _emit_pr_merge_error(args.json, "repository access failed")

        _update_main_branch(args.dry_run)
        prs = _list_open_prs_for_cli(repo_name)
        if prs is None:
            return _emit_pr_merge_error(args.json, "pull-request listing failed")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.error("PR merge setup failed: %s", exc)
        return _emit_pr_merge_error(args.json)
    except KeyboardInterrupt:
        logger.warning("PR merge interrupted before batch discovery completed")
        return _emit_merge_summary(
            [],
            requested=0,
            json_output=args.json,
            interrupted=True,
            message="interrupted before PR batch discovery completed",
        )

    outcomes = _process_open_prs(
        repo_name,
        prs,
        push_all=args.push_all,
        dry_run=args.dry_run,
    )
    return _emit_merge_summary(
        outcomes,
        requested=len(prs),
        json_output=args.json,
        message="PR merge batch complete",
    )


if __name__ == "__main__":
    sys.exit(main())
