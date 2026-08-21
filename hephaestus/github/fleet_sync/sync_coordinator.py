"""Per-repository fleet-sync orchestration."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from hephaestus.github.fleet_sync.conflict_resolver import resolve_conflict_with_agent
from hephaestus.github.fleet_sync.git_ops import ensure_repo_clone, rebase_and_resign
from hephaestus.github.fleet_sync.models import (
    UNICODE_SYMBOLS,
    FleetTimeouts,
    PRInfo,
    PRStatus,
    Symbols,
)
from hephaestus.github.fleet_sync.pr_api import list_prs, merge_pr
from hephaestus.logging.utils import get_logger

logger = get_logger(__name__)

Counts = dict[str, int]
RepoCloneLoader = Callable[[], Path]


def _resign_args(args: argparse.Namespace) -> dict[str, str | bool]:
    """Return only explicitly populated re-signing CLI values."""
    values: dict[str, str | bool] = {}
    resign_email = getattr(args, "resign_email", None)
    if isinstance(resign_email, str) and resign_email.strip():
        values["resign_email"] = resign_email
    if getattr(args, "skip_email_key_check", False) is True:
        values["skip_email_key_check"] = True
    return values


def _initial_counts() -> Counts:
    """Return a fresh outcome counter for one repository."""
    return {
        "merged": 0,
        "rebased": 0,
        "conflict_resolved": 0,
        "skipped": 0,
        "failed": 0,
    }


def _repo_clone_loader(
    repo: str,
    org: str,
    args: argparse.Namespace,
    clone_dir: Path,
    timeouts: FleetTimeouts | None,
) -> RepoCloneLoader:
    """Return a lazy clone loader shared across PRs for one repository."""
    repo_clone: Path | None = None

    def _repo_clone() -> Path:
        nonlocal repo_clone
        if repo_clone is None:
            repo_clone = ensure_repo_clone(
                repo,
                org,
                clone_dir,
                dry_run=args.dry_run,
                **({"timeouts": timeouts} if timeouts is not None else {}),
            )
        return repo_clone

    return _repo_clone


def _status_label(status: PRStatus) -> str:
    """Return the stable display label for a PR status."""
    return {
        PRStatus.READY: "READY",
        PRStatus.OUTDATED: "OUTDATED",
        PRStatus.CONFLICTED: "CONFLICTED",
        PRStatus.FAILING: "FAILING",
        PRStatus.UNKNOWN: "UNKNOWN",
    }[status]


def _log_pr(pr: PRInfo) -> None:
    """Log one PR summary line."""
    logger.info(
        "  PR #%d [%s] %r  (CI=%s mergeable=%s state=%s)",
        pr.number,
        _status_label(pr.status),
        pr.title[:60],
        pr.ci_state,
        pr.mergeable,
        pr.merge_state,
    )


def _record_result(counts: Counts, success_key: str, ok: bool) -> None:
    """Record a successful outcome or failure."""
    counts[success_key if ok else "failed"] += 1


def _process_conflicted_pr(
    pr: PRInfo,
    org: str,
    args: argparse.Namespace,
    repo_clone: RepoCloneLoader,
    counts: Counts,
    symbols: Symbols,
    timeouts: FleetTimeouts | None,
) -> None:
    """Resolve or skip one conflicted PR."""
    if args.skip_conflict_resolution:
        logger.info("  %s Skipping (--skip-conflict-resolution)", symbols.arrow)
        counts["skipped"] += 1
        return

    resign_values = _resign_args(args)
    resign_email = resign_values.get("resign_email")
    resolved_email = resign_email if isinstance(resign_email, str) else None
    skip_email_key_check = resign_values.get("skip_email_key_check") is True
    if timeouts is None and resolved_email is None and not skip_email_key_check:
        ok = resolve_conflict_with_agent(
            pr,
            org,
            repo_clone(),
            dry_run=args.dry_run,
            agent=args.agent,
            symbols=symbols,
        )
    else:
        ok = resolve_conflict_with_agent(
            pr,
            org,
            repo_clone(),
            dry_run=args.dry_run,
            agent=args.agent,
            symbols=symbols,
            timeouts=timeouts,
            resign_email=resolved_email,
            skip_email_key_check=skip_email_key_check,
        )
    _record_result(counts, "conflict_resolved", ok)


def _process_pr(
    pr: PRInfo,
    org: str,
    args: argparse.Namespace,
    repo_clone: RepoCloneLoader,
    counts: Counts,
    symbols: Symbols,
    timeouts: FleetTimeouts | None,
) -> None:
    """Process one PR and update outcome counts."""
    _log_pr(pr)

    if pr.status == PRStatus.READY:
        _record_result(
            counts,
            "merged",
            merge_pr(
                pr,
                org,
                dry_run=args.dry_run,
                **({"timeouts": timeouts} if timeouts is not None else {}),
            ),
        )
    elif pr.status == PRStatus.OUTDATED:
        resign_values = _resign_args(args)
        resign_email = resign_values.get("resign_email")
        resolved_email = resign_email if isinstance(resign_email, str) else None
        skip_email_key_check = resign_values.get("skip_email_key_check") is True
        if timeouts is None and resolved_email is None and not skip_email_key_check:
            ok = rebase_and_resign(
                pr,
                repo_clone(),
                dry_run=args.dry_run,
                symbols=symbols,
            )
        else:
            ok = rebase_and_resign(
                pr,
                repo_clone(),
                dry_run=args.dry_run,
                symbols=symbols,
                timeouts=timeouts,
                resign_email=resolved_email,
                skip_email_key_check=skip_email_key_check,
            )
        _record_result(counts, "rebased", ok)
    elif pr.status == PRStatus.CONFLICTED:
        _process_conflicted_pr(pr, org, args, repo_clone, counts, symbols, timeouts)
    else:
        logger.info("  %s Skipping (CI failing or unknown state)", symbols.arrow)
        counts["skipped"] += 1


def process_repo(
    repo: str,
    org: str,
    args: argparse.Namespace,
    clone_dir: Path,
    *,
    symbols: Symbols = UNICODE_SYMBOLS,
    timeouts: FleetTimeouts | None = None,
) -> Counts:
    """Process all open PRs in one repo and return counts by outcome."""
    counts = _initial_counts()
    args_timeouts = getattr(args, "timeouts", None)
    resolved_timeouts = timeouts or (
        args_timeouts if isinstance(args_timeouts, FleetTimeouts) else None
    )

    logger.info("\n%s %s %s", symbols.banner, repo, symbols.banner)
    if args.dry_run:
        logger.info("  [dry-run] Would inspect open PRs for %s (GitHub reads suppressed)", repo)
        return counts

    try:
        prs = list_prs(
            repo,
            org,
            **({"timeouts": resolved_timeouts} if resolved_timeouts is not None else {}),
        )
    except RuntimeError as e:
        logger.error("  %s", e)
        counts["failed"] += 1
        return counts

    if not prs:
        logger.info("  No open PRs")
        return counts

    logger.info("  %d open PR(s)", len(prs))
    repo_clone = _repo_clone_loader(repo, org, args, clone_dir, resolved_timeouts)
    for pr in prs:
        _process_pr(pr, org, args, repo_clone, counts, symbols, resolved_timeouts)
    return counts
