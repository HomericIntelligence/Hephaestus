"""Per-repository fleet-sync orchestration."""

from __future__ import annotations

import argparse
from pathlib import Path

from hephaestus.github.fleet_sync.conflict_resolver import resolve_conflict_with_agent
from hephaestus.github.fleet_sync.git_ops import ensure_repo_clone, rebase_and_resign
from hephaestus.github.fleet_sync.models import UNICODE_SYMBOLS, PRStatus, Symbols
from hephaestus.github.fleet_sync.pr_api import list_prs, merge_pr
from hephaestus.logging.utils import get_logger

logger = get_logger(__name__)


def process_repo(
    repo: str,
    org: str,
    args: argparse.Namespace,
    clone_dir: Path,
    *,
    symbols: Symbols = UNICODE_SYMBOLS,
) -> dict[str, int]:
    """Process all open PRs in one repo and return counts by outcome."""
    counts: dict[str, int] = {
        "merged": 0,
        "rebased": 0,
        "conflict_resolved": 0,
        "skipped": 0,
        "failed": 0,
    }

    logger.info("\n%s %s %s", symbols.banner, repo, symbols.banner)
    try:
        prs = list_prs(repo, org)
    except RuntimeError as e:
        logger.error("  %s", e)
        counts["failed"] += 1
        return counts

    if not prs:
        logger.info("  No open PRs")
        return counts

    logger.info("  %d open PR(s)", len(prs))

    repo_clone: Path | None = None

    def _repo_clone() -> Path:
        nonlocal repo_clone
        if repo_clone is None:
            repo_clone = ensure_repo_clone(repo, org, clone_dir, dry_run=args.dry_run)
        return repo_clone

    status_labels = {
        PRStatus.READY: "READY",
        PRStatus.OUTDATED: "OUTDATED",
        PRStatus.CONFLICTED: "CONFLICTED",
        PRStatus.FAILING: "FAILING",
        PRStatus.UNKNOWN: "UNKNOWN",
    }

    for pr in prs:
        label = status_labels[pr.status]
        logger.info(
            "  PR #%d [%s] %s  (CI=%s mergeable=%s state=%s)",
            pr.number,
            label,
            pr.title[:60],
            pr.ci_state,
            pr.mergeable,
            pr.merge_state,
        )

        if pr.status == PRStatus.READY:
            ok = merge_pr(pr, org, dry_run=args.dry_run)
            counts["merged" if ok else "failed"] += 1

        elif pr.status == PRStatus.OUTDATED:
            ok = rebase_and_resign(pr, _repo_clone(), dry_run=args.dry_run, symbols=symbols)
            counts["rebased" if ok else "failed"] += 1

        elif pr.status == PRStatus.CONFLICTED:
            if args.skip_conflict_resolution:
                logger.info("  %s Skipping (--skip-conflict-resolution)", symbols.arrow)
                counts["skipped"] += 1
            else:
                ok = resolve_conflict_with_agent(
                    pr,
                    org,
                    _repo_clone(),
                    dry_run=args.dry_run,
                    agent=args.agent,
                    symbols=symbols,
                )
                counts["conflict_resolved" if ok else "failed"] += 1

        else:
            logger.info("  %s Skipping (CI failing or unknown state)", symbols.arrow)
            counts["skipped"] += 1

    return counts
