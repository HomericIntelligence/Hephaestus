#!/usr/bin/env python3
"""Sync all PRs across the HomericIntelligence fleet.

For every open PR in every configured repo:
  - Ready (CI green, no conflicts) → merge via merge commit (GitHub signs it)
  - Outdated (behind base) → rebase on main, re-sign, push
  - Conflicted → spawn a Claude agent to resolve conflicts semantically,
                  then re-sign and push

Signing uses the local GPG key configured in git's ``user.signingkey``.
All commits produced by this script are signed with ``git commit -S``.

Usage:
    hephaestus-fleet-sync [--dry-run] [--repos REPO ...] [--skip-conflict-resolution]

Options:
    --dry-run                    Print actions without executing
    --repos REPO [REPO ...]      Restrict to specific repos (default: all 15)
    --skip-conflict-resolution   Skip the agent swarm for conflicted PRs
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any

from hephaestus.github.gh_subprocess import _gh_call
from hephaestus.github.rate_limit import detect_rate_limit, wait_until
from hephaestus.logging.utils import get_logger

logger = get_logger(__name__)

ORG = "HomericIntelligence"

FLEET_NOREPLY = "4211002+mvillmow@users.noreply.github.com"

RESIGN_EXEC = f"git -c user.email={FLEET_NOREPLY} commit --amend --no-edit -S --reset-author"

ALL_REPOS: list[str] = [
    "Odysseus",
    "AchaeanFleet",
    "ProjectArgus",
    "ProjectHermes",
    "ProjectTelemachy",
    "ProjectKeystone",
    "Myrmidons",
    "ProjectProteus",
    "ProjectOdyssey",
    "ProjectScylla",
    "ProjectMnemosyne",
    "ProjectHephaestus",
    "ProjectAgamemnon",
    "ProjectNestor",
    "ProjectCharybdis",
]


class PRStatus(Enum):
    """Readiness classification for a pull request."""

    READY = auto()  # CI green, no conflicts → merge
    OUTDATED = auto()  # CI pending/green, behind base → rebase + re-sign
    CONFLICTED = auto()  # Has merge conflicts → agent resolution
    FAILING = auto()  # CI failing → skip
    UNKNOWN = auto()  # Can't determine → skip


@dataclass
class PRInfo:
    """All information needed to act on a single pull request."""

    repo: str
    number: int
    title: str
    head_ref: str
    base_ref: str
    head_sha: str
    mergeable: str  # MERGEABLE | CONFLICTING | UNKNOWN
    merge_state: str  # CLEAN | BEHIND | DIRTY | BLOCKED | UNKNOWN
    ci_state: str  # SUCCESS | FAILURE | PENDING | UNKNOWN
    status: PRStatus = PRStatus.UNKNOWN
    conflict_files: list[str] = field(default_factory=list)


def _gh(
    args: list[str],
    repo: str | None = None,
    check: bool = True,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run gh CLI, optionally scoped to a repo, with rate-limit retry."""
    full_args = args
    if repo and not any(a.startswith("--repo") or a == "-R" for a in args):
        full_args = ["--repo", f"{ORG}/{repo}", *args]

    if dry_run:
        logger.info("[dry-run] gh %s", " ".join(full_args))
        return subprocess.CompletedProcess(full_args, 0, stdout="[]", stderr="")

    for attempt in range(4):
        try:
            return subprocess.run(
                ["gh", *full_args],
                check=check,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.CalledProcessError as e:
            epoch = detect_rate_limit(e.stderr or "")
            if epoch is not None:
                wait_until(epoch)
                continue
            if attempt == 3:
                raise
            time.sleep(2**attempt)

    raise RuntimeError("gh call failed after retries")


def _git(
    args: list[str],
    cwd: Path,
    dry_run: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in a working directory."""
    if dry_run:
        logger.info("[dry-run] git %s (in %s)", " ".join(args), cwd)
        return subprocess.CompletedProcess(["git", *args], 0, stdout="", stderr="")
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def _ci_state(checks: list[dict[str, Any]]) -> str:
    """Reduce a statusCheckRollup list to a single state string."""
    if not checks:
        return "UNKNOWN"
    bad = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "ERROR", "failure", "error"}
    pending = {"PENDING", "IN_PROGRESS", "QUEUED", "WAITING", "pending"}
    conclusions = {c.get("conclusion") or c.get("state", "PENDING") for c in checks}
    if any(c is None for c in (c.get("conclusion") for c in checks)):
        return "PENDING"
    if conclusions & bad:
        return "FAILURE"
    if conclusions & pending:
        return "PENDING"
    return "SUCCESS"


def list_prs(repo: str) -> list[PRInfo]:
    """List all open PRs in a repo with their readiness status."""
    try:
        result = _gh(
            [
                "pr",
                "list",
                "--state",
                "open",
                "--json",
                (
                    "number,title,headRefName,baseRefName,headRefOid,"
                    "mergeable,mergeStateStatus,statusCheckRollup"
                ),
                "--limit",
                "100",
            ],
            repo=repo,
        )
    except subprocess.CalledProcessError as e:
        logger.warning("Could not list PRs for %s: %s", repo, e)
        return []

    prs_raw: list[dict[str, Any]] = json.loads(result.stdout)
    out: list[PRInfo] = []

    for p in prs_raw:
        ci = _ci_state(p.get("statusCheckRollup") or [])
        mergeable = p.get("mergeable", "UNKNOWN")
        merge_state = p.get("mergeStateStatus", "UNKNOWN")

        if mergeable == "CONFLICTING":
            status = PRStatus.CONFLICTED
        elif ci == "FAILURE":
            status = PRStatus.FAILING
        elif merge_state == "BEHIND":
            status = PRStatus.OUTDATED
        elif merge_state == "CLEAN" and ci == "SUCCESS":
            status = PRStatus.READY
        elif merge_state in ("BLOCKED", "DIRTY"):
            status = PRStatus.CONFLICTED if mergeable == "CONFLICTING" else PRStatus.OUTDATED
        else:
            status = PRStatus.OUTDATED

        out.append(
            PRInfo(
                repo=repo,
                number=p["number"],
                title=p["title"],
                head_ref=p["headRefName"],
                base_ref=p["baseRefName"],
                head_sha=p["headRefOid"],
                mergeable=mergeable,
                merge_state=merge_state,
                ci_state=ci,
                status=status,
            )
        )

    return out


def merge_pr(pr: PRInfo, dry_run: bool = False) -> bool:
    """Merge a ready PR via merge commit (GitHub signs the merge commit)."""
    logger.info("  Merging PR #%d via merge commit...", pr.number)
    try:
        _gh(
            ["pr", "merge", str(pr.number), "--merge", "--auto"],
            repo=pr.repo,
            dry_run=dry_run,
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.error("  Failed to merge PR #%d: %s", pr.number, e.stderr)
        return False


def rebase_and_resign(pr: PRInfo, clone_dir: Path, dry_run: bool = False) -> bool:
    """Fetch PR branch, rebase it on origin/base, re-sign all commits, push."""
    repo_url = f"https://github.com/{ORG}/{pr.repo}.git"
    branch = pr.head_ref
    base = pr.base_ref

    work = clone_dir / f"{pr.repo}-{pr.number}"
    work.mkdir(parents=True, exist_ok=True)

    logger.info("  Cloning %s into temp dir...", pr.repo)
    try:
        _git(["clone", "--filter=blob:none", repo_url, str(work)], cwd=clone_dir, dry_run=dry_run)
        _git(["fetch", "origin", branch], cwd=work, dry_run=dry_run)
        _git(["checkout", branch], cwd=work, dry_run=dry_run)
        _git(["fetch", "origin", base], cwd=work, dry_run=dry_run)

        result = _git(
            ["rebase", f"origin/{base}", "--exec", RESIGN_EXEC],
            cwd=work,
            dry_run=dry_run,
            check=False,
        )

        if result.returncode != 0:
            logger.warning("  Rebase failed for PR #%d — conflict detected", pr.number)
            _git(["rebase", "--abort"], cwd=work, dry_run=dry_run, check=False)
            return False

        _git(["push", "--force-with-lease", "origin", branch], cwd=work, dry_run=dry_run)
        logger.info("  ✓ Rebased and re-signed PR #%d", pr.number)
        return True

    except subprocess.CalledProcessError as e:
        logger.error("  Rebase/push failed for PR #%d: %s", pr.number, e.stderr or str(e))
        return False


def resolve_conflict_with_agent(pr: PRInfo, clone_dir: Path, dry_run: bool = False) -> bool:
    """Spawn a Claude agent to semantically resolve merge conflicts, then re-sign."""
    try:
        from claude_code_sdk import ClaudeCodeOptions, query
    except ImportError:
        logger.warning(
            "claude_code_sdk not available — skipping agent resolution for PR #%d. "
            "Install with: pip install claude-code-sdk",
            pr.number,
        )
        return False

    repo_url = f"https://github.com/{ORG}/{pr.repo}.git"
    branch = pr.head_ref
    base = pr.base_ref

    work = clone_dir / f"{pr.repo}-{pr.number}-conflict"
    work.mkdir(parents=True, exist_ok=True)

    logger.info("  Cloning %s for conflict resolution...", pr.repo)
    try:
        _git(["clone", "--filter=blob:none", repo_url, str(work)], cwd=clone_dir, dry_run=False)
        _git(["fetch", "origin", branch], cwd=work, dry_run=False)
        _git(["checkout", branch], cwd=work, dry_run=False)
        _git(["fetch", "origin", base], cwd=work, dry_run=False)

        # Start rebase — will stop at conflicts
        subprocess.run(
            ["git", "rebase", f"origin/{base}"],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
        )

        # Identify conflicted files
        status_result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=work,
            capture_output=True,
            text=True,
            check=True,
        )
        conflict_files = [f.strip() for f in status_result.stdout.splitlines() if f.strip()]

        if not conflict_files:
            _git(["rebase", "--continue"], cwd=work, dry_run=False, check=False)
        else:
            pr.conflict_files = conflict_files
            logger.info("  Conflicted files: %s", ", ".join(conflict_files))

            if dry_run:
                logger.info(
                    "  [dry-run] Would spawn agent to resolve conflicts in %s",
                    conflict_files,
                )
                _git(["rebase", "--abort"], cwd=work, dry_run=False, check=False)
                return False

            conflict_list = "\n".join(f"- {f}" for f in conflict_files)
            commit_count_result = subprocess.run(
                ["git", "rev-list", "--count", f"origin/{base}..HEAD"],
                cwd=work,
                capture_output=True,
                text=True,
                check=True,
            )
            commit_count = commit_count_result.stdout.strip()

            prompt = f"""You are resolving merge conflicts in a git rebase.

Repository: {ORG}/{pr.repo}
PR: #{pr.number} — "{pr.title}"
Branch `{branch}` is being rebased onto `origin/{base}`.
Working directory: {work}

Conflicted files:
{conflict_list}

For each conflicted file:
1. Read the file — it contains conflict markers (<<<<<<<, =======, >>>>>>>)
2. Understand BOTH sides semantically — do not simply pick one side
3. Write the correctly merged content preserving the intent of both sides
4. Stage the file: git add <file>

After ALL conflicts are resolved:
1. Continue the rebase: git -c user.email={FLEET_NOREPLY} rebase --continue
   (repeat if more conflicts appear)
2. Re-sign all commits:
   git rebase HEAD~{commit_count} --exec '{RESIGN_EXEC}'
3. Push: git push --force-with-lease origin {branch}

Rules:
- Never use `git rebase --skip` or discard either side without understanding it
- Never use `git checkout --ours/--theirs` without reading both sides first
- For generated/lock files, prefer the incoming (theirs) side
- All commits must be GPG-signed (-S flag)
"""
            logger.info(
                "  Spawning Claude agent to resolve %d conflict(s)...",
                len(conflict_files),
            )
            options = ClaudeCodeOptions(max_turns=30, cwd=str(work))
            for message in query(prompt=prompt, options=options):  # type: ignore[attr-defined]
                text = getattr(message, "text", None) or str(message)
                if text:
                    logger.debug("  agent: %s", text[:200])

        # Verify branch was pushed
        verify = subprocess.run(
            ["git", "ls-remote", "origin", branch],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
        )
        if branch in verify.stdout:
            logger.info("  ✓ Conflict resolved and pushed for PR #%d", pr.number)
            return True

        logger.warning("  Agent did not push branch for PR #%d", pr.number)
        return False

    except Exception as e:
        logger.error("  Conflict resolution failed for PR #%d: %s", pr.number, e)
        with contextlib.suppress(Exception):
            _git(["rebase", "--abort"], cwd=work, dry_run=False, check=False)
        return False


def process_repo(
    repo: str,
    args: argparse.Namespace,
    clone_dir: Path,
) -> dict[str, int]:
    """Process all open PRs in one repo. Returns counts by outcome."""
    counts: dict[str, int] = {
        "merged": 0,
        "rebased": 0,
        "conflict_resolved": 0,
        "skipped": 0,
        "failed": 0,
    }

    logger.info("\n══ %s ══", repo)
    prs = list_prs(repo)

    if not prs:
        logger.info("  No open PRs")
        return counts

    logger.info("  %d open PR(s)", len(prs))

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
            ok = merge_pr(pr, dry_run=args.dry_run)
            counts["merged" if ok else "failed"] += 1

        elif pr.status == PRStatus.OUTDATED:
            ok = rebase_and_resign(pr, clone_dir, dry_run=args.dry_run)
            counts["rebased" if ok else "failed"] += 1

        elif pr.status == PRStatus.CONFLICTED:
            if args.skip_conflict_resolution:
                logger.info("  → Skipping (--skip-conflict-resolution)")
                counts["skipped"] += 1
            else:
                ok = resolve_conflict_with_agent(pr, clone_dir, dry_run=args.dry_run)
                counts["conflict_resolved" if ok else "failed"] += 1

        else:
            logger.info("  → Skipping (CI failing or unknown state)")
            counts["skipped"] += 1

    return counts


def main() -> int:
    """Entry point for hephaestus-fleet-sync."""
    parser = argparse.ArgumentParser(
        description="Sync all PRs across the HomericIntelligence fleet",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    parser.add_argument(
        "--repos",
        nargs="+",
        metavar="REPO",
        help=f"Restrict to specific repos (default: all {len(ALL_REPOS)})",
        default=ALL_REPOS,
    )
    parser.add_argument(
        "--skip-conflict-resolution",
        action="store_true",
        help="Skip Claude agent swarm for conflicted PRs",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    repos = args.repos
    dry_tag = " [DRY RUN]" if args.dry_run else ""
    logger.info("Fleet sync — %d repo(s)%s", len(repos), dry_tag)

    totals: dict[str, int] = {
        "merged": 0,
        "rebased": 0,
        "conflict_resolved": 0,
        "skipped": 0,
        "failed": 0,
    }

    with tempfile.TemporaryDirectory(prefix="hephaestus-fleet-") as tmp:
        clone_dir = Path(tmp)
        for repo in repos:
            counts = process_repo(repo, args, clone_dir)
            for k, v in counts.items():
                totals[k] = totals.get(k, 0) + v

    logger.info("\n%s", "=" * 60)
    logger.info("Fleet sync complete")
    logger.info("  Merged:            %d", totals["merged"])
    logger.info("  Rebased+re-signed: %d", totals["rebased"])
    logger.info("  Conflicts resolved:%d", totals["conflict_resolved"])
    logger.info("  Skipped:           %d", totals["skipped"])
    logger.info("  Failed:            %d", totals["failed"])

    return 0 if totals["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
