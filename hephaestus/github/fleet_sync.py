#!/usr/bin/env python3
"""Sync all PRs across the HomericIntelligence fleet.

For every open PR in every configured repo:
  - Ready (CI green, no conflicts) → merge via merge commit (GitHub signs it)
  - Outdated (behind base) → rebase on main, re-sign, push
  - Conflicted → spawn the selected coding agent to resolve conflicts semantically,
                  then re-sign and push

Signing uses the local GPG key configured in git's ``user.signingkey``.
All commits produced by this script are signed with ``git commit -S``.

Usage:
    hephaestus-fleet-sync [--dry-run] [--repos REPO ...] [--skip-conflict-resolution]

Options:
    --dry-run                    Print actions without executing
    --repos REPO [REPO ...]      Restrict to specific repos (default: all 15)
    --skip-conflict-resolution   Skip agent conflict resolution for conflicted PRs
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import add_agent_argument, is_codex, resolve_agent, run_codex_text
from hephaestus.cli.utils import add_json_arg, emit_json_status
from hephaestus.github.rate_limit import detect_rate_limit, wait_until
from hephaestus.logging.utils import get_logger
from hephaestus.utils.helpers import METADATA_TIMEOUT, NETWORK_TIMEOUT

logger = get_logger(__name__)

ORG = "HomericIntelligence"


def _signing_key_uid_emails() -> list[str] | None:
    """Return the email addresses on the configured GPG signing key, lowercased.

    Reads ``git config user.signingkey`` and lists the UID emails on that key
    via ``gpg --list-keys --with-colons``. Returns ``None`` (meaning "cannot
    determine — skip the check") when:

    - no ``user.signingkey`` is configured,
    - ``gpg`` is not installed / not on PATH,
    - the key cannot be read, or
    - the lookup times out.

    Returns an empty list only when the key exists but exposes no UID emails.
    """
    try:
        key_result = subprocess.run(
            ["git", "config", "--get", "user.signingkey"],
            capture_output=True,
            text=True,
            check=False,
            timeout=METADATA_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    signing_key = key_result.stdout.strip()
    if key_result.returncode != 0 or not signing_key:
        return None

    try:
        gpg_result = subprocess.run(
            ["gpg", "--list-keys", "--with-colons", signing_key],
            capture_output=True,
            text=True,
            check=False,
            timeout=METADATA_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if gpg_result.returncode != 0:
        return None

    emails: list[str] = []
    # ``uid`` records put the user-id string in field 10 (1-indexed); the email
    # is the part inside angle brackets, e.g. "Name <addr@example.com>".
    for line in gpg_result.stdout.splitlines():
        fields = line.split(":")
        if not fields or fields[0] != "uid" or len(fields) < 10:
            continue
        uid = fields[9]
        start = uid.find("<")
        end = uid.find(">", start + 1)
        if start != -1 and end != -1:
            emails.append(uid[start + 1 : end].strip().lower())
    return emails


def _validate_resign_email(email: str) -> str:
    """Validate ``email`` matches the GPG signing key, then return it.

    fleet_sync re-signs every rebased commit with ``git commit -S`` using the
    local GPG key. GitHub only marks a signature ``verified`` when the commit's
    committer email is one of the *verified emails on the account that owns the
    signing key* — in practice, one of the key's UID emails. If we re-sign with
    an email that is not on the key (e.g. an operator's bot/no-reply alias that
    was never added to the key), the commit signs fine locally yet GitHub reports
    ``{verified: false, reason: "no_user"}`` and the ``pr-policy`` "every commit
    is signed" check rejects the PR at merge. Catch that here so fleet_sync fails
    fast with an actionable message instead of producing commits that pr-policy
    will silently reject across the whole fleet.

    Set ``FLEET_SKIP_EMAIL_KEY_CHECK=1`` to bypass (e.g. signing format other than
    OpenPGP, or a deliberately unusual setup).
    """
    if os.environ.get("FLEET_SKIP_EMAIL_KEY_CHECK", "").strip():
        return email
    key_emails = _signing_key_uid_emails()
    if key_emails is None:
        # Cannot determine the key's identities (no signingkey, gpg absent,
        # unreadable key); don't block — the operator may sign by other means.
        return email
    if email.lower() not in key_emails:
        raise RuntimeError(
            f"fleet_sync: resign email {email!r} is not a UID on the configured "
            f"GPG signing key (key UIDs: {key_emails or 'none'}). Re-signing with "
            "this email would produce commits GitHub marks unverified, failing the "
            "pr-policy 'every commit is signed' check at merge. Set FLEET_GIT_EMAIL "
            "(or git config user.email) to an address on the signing key, or set "
            "FLEET_SKIP_EMAIL_KEY_CHECK=1 to bypass."
        )
    return email


def get_resign_email() -> str:
    """Return the email address used to re-sign rebased commits.

    Resolution order:

    1. ``$FLEET_GIT_EMAIL`` if set and non-empty.
    2. ``git config --global --get user.email``.
    3. ``git config --get user.email`` (any scope).

    The resolved email is validated against the configured GPG signing key's
    UID emails (see :func:`_validate_resign_email`); a mismatch raises
    :class:`RuntimeError` because re-signing with it would produce commits
    GitHub marks unverified, failing ``pr-policy`` at merge.

    Raises :class:`RuntimeError` if none is configured — fleet_sync must never
    silently re-sign with a fallback identity that doesn't belong to the
    operator.
    """
    env = os.environ.get("FLEET_GIT_EMAIL", "").strip()
    if env:
        return _validate_resign_email(env)
    for args in (["--global"], []):
        try:
            result = subprocess.run(
                ["git", "config", *args, "--get", "user.email"],
                capture_output=True,
                text=True,
                check=False,
                timeout=METADATA_TIMEOUT,
            )
            email = result.stdout.strip()
            if result.returncode == 0 and email:
                return _validate_resign_email(email)
        except subprocess.TimeoutExpired:
            continue
    raise RuntimeError(
        "fleet_sync: no resign email configured. Set FLEET_GIT_EMAIL=<address> "
        "or `git config --global user.email <address>` before running."
    )


def get_resign_exec() -> str:
    """Return the ``git commit --amend`` shell command used as ``rebase --exec``.

    The email is resolved lazily so changes to ``$FLEET_GIT_EMAIL`` or git config
    between calls take effect without restarting the process.
    """
    return f"git -c user.email={get_resign_email()} commit --amend --no-edit -S --reset-author"


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
                timeout=NETWORK_TIMEOUT,
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
    """Run a git command in a working directory.

    Uses NETWORK_TIMEOUT for operations involving network I/O (clone, fetch, push).
    """
    if dry_run:
        logger.info("[dry-run] git %s (in %s)", " ".join(args), cwd)
        return subprocess.CompletedProcess(["git", *args], 0, stdout="", stderr="")
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        timeout=NETWORK_TIMEOUT,
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


def _fetch_pr_ci_state(repo: str, number: int) -> str:
    """Fetch a single PR's statusCheckRollup and reduce it to a CI state.

    Fetched per-PR rather than in the bulk list: requesting statusCheckRollup for
    every open PR in one ``gh pr list`` call makes GitHub GraphQL return HTTP 504
    at scale (~50+ PRs), because that field aggregates every check on every PR.
    One PR per call stays well within limits. On any failure we return
    "UNKNOWN" so a single flaky check fetch downgrades that PR's readiness
    (it falls through to a rebase) rather than aborting the whole run.
    """
    try:
        result = _gh(
            ["pr", "view", str(number), "--json", "statusCheckRollup"],
            repo=repo,
        )
        data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, RuntimeError, json.JSONDecodeError) as e:
        logger.warning("Could not fetch CI state for %s#%s: %s", repo, number, e)
        return "UNKNOWN"
    return _ci_state(data.get("statusCheckRollup") or [])


def list_prs(repo: str) -> list[PRInfo]:
    """List all open PRs in a repo with their readiness status.

    The bulk ``gh pr list`` deliberately omits ``statusCheckRollup`` — requesting
    it for every PR 504s at scale (see #1027). CI state is fetched per-PR via
    :func:`_fetch_pr_ci_state`. A genuine list failure is raised, never swallowed
    into an empty list (which would masquerade as "no open PRs" and silently skip
    the entire queue).
    """
    try:
        result = _gh(
            [
                "pr",
                "list",
                "--state",
                "open",
                "--json",
                ("number,title,headRefName,baseRefName,headRefOid,mergeable,mergeStateStatus"),
                "--limit",
                "100",
            ],
            repo=repo,
        )
        prs_raw: list[dict[str, Any]] = json.loads(result.stdout)
    except (subprocess.CalledProcessError, RuntimeError, json.JSONDecodeError) as e:
        # Do NOT return [] here: an empty list is indistinguishable from "this
        # repo has no open PRs", which would make fleet_sync report success
        # while silently skipping every PR. Fail loudly instead.
        raise RuntimeError(f"fleet_sync: could not list PRs for {repo}: {e}") from e

    out: list[PRInfo] = []

    for p in prs_raw:
        ci = _fetch_pr_ci_state(repo, p["number"])
        mergeable = p.get("mergeable", "UNKNOWN")
        merge_state = p.get("mergeStateStatus", "UNKNOWN")

        if mergeable == "CONFLICTING":
            status = PRStatus.CONFLICTED
        elif ci == "FAILURE" and merge_state == "CLEAN":
            # FAILING means a genuine, PR-specific failure: the branch is already
            # up to date with its base (CLEAN) yet CI is red. Skip — a rebase
            # wouldn't change the outcome. We require CLEAN here so that a PR
            # which is merely BEHIND/BLOCKED with a STALE red result (its checks
            # ran against an old base, commonly a failure already fixed on main)
            # is NOT skipped — it falls through to OUTDATED and gets rebased,
            # which re-runs CI fresh. Without the CLEAN guard, a fix landing on
            # main strands the entire queue as "FAILING".
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
            ["rebase", f"origin/{base}", "--exec", get_resign_exec()],
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

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.error("  Rebase/push failed for PR #%d: %s", pr.number, e.stderr or str(e))
        return False


def _run_conflict_agent(agent: str, prompt: str, work: Path, pr_number: int) -> bool:
    """Run the selected conflict-resolution agent."""
    if is_codex(agent):
        result = run_codex_text(
            prompt,
            cwd=work,
            timeout=2400,
            sandbox="workspace-write",
        )
        if result.stdout:
            logger.debug("  agent: %s", result.stdout[:200])
        return True

    try:
        from claude_code_sdk import ClaudeCodeOptions, query
    except ImportError:
        logger.warning(
            "claude_code_sdk not available — skipping agent resolution for PR #%d. "
            "Install with: pip install claude-code-sdk",
            pr_number,
        )
        return False

    options = ClaudeCodeOptions(max_turns=30, cwd=str(work))

    async def _drain() -> None:
        async for message in query(prompt=prompt, options=options):
            text = getattr(message, "text", None) or str(message)
            if text:
                logger.debug("  agent: %s", text[:200])

    import asyncio

    asyncio.run(_drain())
    return True


def resolve_conflict_with_agent(
    pr: PRInfo,
    clone_dir: Path,
    dry_run: bool = False,
    agent: str = "claude",
) -> bool:
    """Spawn the selected agent to semantically resolve merge conflicts, then re-sign."""
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
            timeout=NETWORK_TIMEOUT,
        )

        # Identify conflicted files
        status_result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=work,
            capture_output=True,
            text=True,
            check=True,
            timeout=METADATA_TIMEOUT,
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
                timeout=METADATA_TIMEOUT,
            )
            commit_count = commit_count_result.stdout.strip()
            resign_email = get_resign_email()
            resign_exec = get_resign_exec()

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
1. Continue the rebase: git -c user.email={resign_email} rebase --continue
   (repeat if more conflicts appear)
2. Re-sign all commits:
   git rebase HEAD~{commit_count} --exec '{resign_exec}'
3. Push: git push --force-with-lease origin {branch}

Rules:
- Never use `git rebase --skip` or discard either side without understanding it
- Never use `git checkout --ours/--theirs` without reading both sides first
- For generated/lock files, prefer the incoming (theirs) side
- All commits must be GPG-signed (-S flag)
"""
            logger.info(
                "  Spawning %s agent to resolve %d conflict(s)...",
                agent,
                len(conflict_files),
            )
            if not _run_conflict_agent(agent, prompt, work, pr.number):
                return False

        # Verify branch was pushed
        verify = subprocess.run(
            ["git", "ls-remote", "origin", branch],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
            timeout=NETWORK_TIMEOUT,
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
    try:
        prs = list_prs(repo)
    except RuntimeError as e:
        # A list failure must NOT be silently treated as "no PRs" — that would
        # skip the whole repo while reporting success. Count it as a failure so
        # the run's exit status reflects the unprocessed queue, and continue to
        # the next repo.
        logger.error("  %s", e)
        counts["failed"] += 1
        return counts

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
                ok = resolve_conflict_with_agent(
                    pr,
                    clone_dir,
                    dry_run=args.dry_run,
                    agent=args.agent,
                )
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
        help="Skip agent conflict resolution for conflicted PRs",
    )
    add_agent_argument(parser)
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    add_json_arg(parser)
    args = parser.parse_args()
    agent = resolve_agent(args.agent)
    args.agent = agent

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

    exit_code = 0 if totals["failed"] == 0 else 1
    if args.json:
        emit_json_status(exit_code, None, repos=len(repos), totals=totals)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
