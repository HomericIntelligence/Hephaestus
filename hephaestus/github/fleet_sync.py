r"""Sync all PRs across a configurable GitHub org's fleet of repos.

For every open PR in every configured repo:
  - Ready (CI green, no conflicts) → merge via merge commit (GitHub signs it)
  - Outdated (behind base) → rebase on main, re-sign, push
  - Conflicted → spawn the selected coding agent to resolve conflicts semantically,
                  then re-sign and push

Signing uses the local GPG key configured in git's ``user.signingkey``.
All commits produced by this script are signed and DCO-signed with
``git commit -S -s``.

Usage:
    hephaestus-fleet-sync [--dry-run] [--org ORG] [--repos REPO ...] \
                          [--config PATH] [--skip-conflict-resolution]

Config resolution (highest priority first):
    1. ``--org`` / ``--repos`` CLI flags
    2. ``FLEET_ORG`` / ``FLEET_REPOS`` env vars
       (``FLEET_REPOS`` is comma-separated; whitespace is trimmed;
        values are always treated as strings — no int/float coercion)
    3. ``.fleet.yml`` at ``--config PATH`` if given, else ``./.fleet.yml``,
       else repo-root ``.fleet.yml``

For ProjectHephaestus's own fleet, a bundled ``.fleet.yml`` lives at the
repo root, so no configuration is required for the default operator flow.
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
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    add_agent_argument,
    direct_agent_model,
    resolve_agent,
    run_agent_text,
    uses_direct_agent_runner,
)
from hephaestus.cli.utils import (
    add_github_throttle_args,
    add_json_arg,
    add_version_arg,
    configure_github_throttle_from_args,
    emit_json_status,
)
from hephaestus.config.utils import load_config
from hephaestus.github.client import gh_call
from hephaestus.logging.utils import get_logger
from hephaestus.utils.helpers import METADATA_TIMEOUT, NETWORK_TIMEOUT

logger = get_logger(__name__)


@dataclass(frozen=True)
class Symbols:
    """Glyphs used in user-facing log output. Frozen for safe sharing across calls."""

    banner: str
    check: str
    arrow: str
    dash: str


UNICODE_SYMBOLS = Symbols(banner="══", check="✓", arrow="→", dash="—")
ASCII_SYMBOLS = Symbols(banner="==", check="*", arrow="->", dash="--")

DEFAULT_FLEET_CONFIG_FILENAME = ".fleet.yml"


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

    fleet_sync re-signs every rebased commit with ``git commit -S -s`` using the
    local GPG key. GitHub only marks a signature ``verified`` when the commit's
    committer email is one of the *verified emails on the account that owns the
    signing key* — in practice, one of the key's UID emails. If we re-sign with
    an email that is not on the key (e.g. an operator's bot/no-reply alias that
    was never added to the key), the commit signs fine locally yet GitHub reports
    ``{verified: false, reason: "no_user"}`` and the ``pr-policy`` checks reject
    the PR at merge. Catch that here so fleet_sync fails fast with an actionable
    message instead of producing commits that pr-policy will silently reject
    across the whole fleet.

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
    return f"git -c user.email={get_resign_email()} commit --amend --no-edit -S -s --reset-author"


def _parse_env_repos(env_repos_raw: str | None) -> list[str] | None:
    """Parse comma-separated FLEET_REPOS, returning None if empty after splitting.

    Args:
        env_repos_raw: Raw FLEET_REPOS value from environment

    Returns:
        List of repo names with whitespace trimmed, or None if input is None or empty after split

    """
    if env_repos_raw is None:
        return None
    env_repos = [r.strip() for r in env_repos_raw.split(",") if r.strip()]
    return env_repos if env_repos else None


def _find_default_config() -> Path | None:
    """Return the first existing .fleet.yml in CWD or repo-root, else None."""
    cwd_path = Path.cwd() / DEFAULT_FLEET_CONFIG_FILENAME
    if cwd_path.exists():
        return cwd_path

    repo_root_path = Path(__file__).resolve().parent.parent.parent / DEFAULT_FLEET_CONFIG_FILENAME
    if repo_root_path.exists():
        return repo_root_path

    return None


def _load_fleet_config(config_path: str | None) -> tuple[str | None, list[str] | None]:
    """Load org and repos from config file, auto-discovering if needed.

    Args:
        config_path: Explicit path to .fleet.yml, or None for auto-discovery

    Returns:
        Tuple of (org, repos) where either may be None if not found

    """
    if config_path is None:
        found_config = _find_default_config()
        if found_config is not None:
            config_path = str(found_config)

    file_org = None
    file_repos = None

    if config_path is not None:
        config_path_obj = Path(config_path)
        if config_path_obj.exists():
            try:
                cfg = load_config(config_path_obj)
                file_org = cfg.get("org")
                file_repos_raw = cfg.get("repos")
                if isinstance(file_repos_raw, list):
                    file_repos = file_repos_raw
            except (FileNotFoundError, ValueError, RuntimeError) as e:
                raise RuntimeError(f"Failed to load fleet config from {config_path}: {e}") from e

    return file_org, file_repos


def resolve_fleet_config(
    cli_org: str | None = None,
    cli_repos: list[str] | None = None,
    config_path: str | None = None,
) -> tuple[str, list[str]]:
    """Resolve fleet organization and repo list with layered config sources.

    Resolution order (highest to lowest priority):
    1. CLI flags (cli_org, cli_repos) — applied per-key, partial CLI args merge
    2. Environment variables (FLEET_ORG, FLEET_REPOS)
    3. Config file (.fleet.yml at config_path, or auto-discovered)

    Args:
        cli_org: Organization name from --org flag
        cli_repos: Repo names from --repos flag
        config_path: Explicit path to .fleet.yml, or None for auto-discovery

    Returns:
        Tuple of (org, repos) where repos is a list of bare repo names

    Raises:
        RuntimeError: If org or repos cannot be resolved from any source

    """
    # Step 1: Load environment variables (as strings, bypassing merge_with_env type coercion)
    env_org = os.environ.get("FLEET_ORG", "").strip()
    env_repos_raw = os.environ.get("FLEET_REPOS")
    env_repos = _parse_env_repos(env_repos_raw) if env_repos_raw is not None else None

    # Step 2: Load config file
    file_org, file_repos = _load_fleet_config(config_path)

    # Step 3: Merge per-key with CLI taking precedence, then env, then file
    final_org = cli_org or env_org or file_org
    if not final_org:
        raise RuntimeError("no fleet org configured. Set --org, FLEET_ORG, or org: in .fleet.yml")

    # Distinguish "FLEET_REPOS set but empty after comma-split" from "unset" so
    # operators can tell a typo'd value (e.g. " , , " or "") apart from simply
    # not having configured the env var. Only fires when no higher-priority CLI
    # value overrides it.
    if not cli_repos and env_repos_raw is not None and env_repos is None:
        raise RuntimeError(
            f"FLEET_REPOS is set but contains no valid entries after comma-split "
            f"(got {env_repos_raw!r})"
        )

    final_repos = cli_repos or env_repos or file_repos

    if not final_repos:
        raise RuntimeError(
            "no fleet repos configured. Set --repos, FLEET_REPOS, or repos: in .fleet.yml"
        )

    return final_org, final_repos


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
    org: str | None = None,
    check: bool = True,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run gh CLI, optionally scoped to a repo, routed through the shared adapter.

    Delegates to :func:`hephaestus.github.client.gh_call` so the call goes
    through the ``github-api`` circuit breaker, the per-thread throttle, and
    rate-limit detection — instead of the hand-rolled retry loop this function
    used to inline (which bypassed the breaker entirely).
    """
    full_args = args
    if repo and not any(a.startswith("--repo") or a == "-R" for a in args):
        if not org:
            raise ValueError("org must be provided when repo is specified")
        repo_arg = f"{org}/{repo}"
        full_args = ["--repo", repo_arg, *args]

    if dry_run:
        logger.info("[dry-run] gh %s", " ".join(full_args))
        return subprocess.CompletedProcess(full_args, 0, stdout="[]", stderr="")

    return gh_call(full_args, check=check)


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


def _fetch_pr_ci_state(repo: str, number: int, org: str | None = None) -> str:
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
            org=org,
        )
        data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, RuntimeError, json.JSONDecodeError) as e:
        logger.warning("Could not fetch CI state for %s#%s: %s", repo, number, e)
        return "UNKNOWN"
    return _ci_state(data.get("statusCheckRollup") or [])


def list_prs(repo: str, org: str) -> list[PRInfo]:
    """List all open PRs in a repo with their readiness status.

    The bulk ``gh pr list`` deliberately omits ``statusCheckRollup`` — requesting
    it for every PR 504s at scale (see #1027). CI state is fetched per-PR via
    :func:`_fetch_pr_ci_state`. A genuine list failure is raised, never swallowed
    into an empty list (which would masquerade as "no open PRs" and silently skip
    the entire queue).

    Discovery is scoped to ``--author @me`` (#1070): fleet_sync rebases and
    re-signs every PR it returns (:func:`rebase_and_resign` runs
    ``commit --amend --reset-author -S -s`` then force-pushes). Run against a PR the
    current user did NOT author — most notably a Dependabot bump — that rewrite
    strips the native GitHub web-flow signature and stamps the local identity,
    and when the amend runs in a shell where gpg-agent was not warmed it silently
    produces an UNSIGNED commit that blocks merge. ``@me`` is resolved
    server-side by gh, so only the current user's PRs are ever surfaced and
    re-signed; bot and other contributors' PRs are left untouched.
    """
    try:
        result = _gh(
            [
                "pr",
                "list",
                "--state",
                "open",
                "--author",
                "@me",
                "--json",
                ("number,title,headRefName,baseRefName,headRefOid,mergeable,mergeStateStatus"),
                "--limit",
                "100",
            ],
            repo=repo,
            org=org,
        )
        prs_raw: list[dict[str, Any]] = json.loads(result.stdout)
    except (subprocess.CalledProcessError, RuntimeError, json.JSONDecodeError) as e:
        # Do NOT return [] here: an empty list is indistinguishable from "this
        # repo has no open PRs", which would make fleet_sync report success
        # while silently skipping every PR. Fail loudly instead.
        raise RuntimeError(f"fleet_sync: could not list PRs for {repo}: {e}") from e

    out: list[PRInfo] = []

    for p in prs_raw:
        ci = _fetch_pr_ci_state(repo, p["number"], org)
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


def ensure_repo_clone(repo: str, org: str, clone_dir: Path, dry_run: bool = False) -> Path:
    """Return a single reusable clone of ``repo``, cloning once or fetching if present.

    fleet_sync used to ``git clone`` the whole repo afresh for every PR (#1044).
    Instead, keep one clone per repo under ``clone_dir/<repo>`` and reuse it for
    all of that repo's PRs: clone on first use, ``git fetch`` (prune) on reuse.
    Per-PR work then happens in cheap ``git worktree`` checkouts off this clone.

    Args:
        repo: Repository name (under ``org``).
        org: GitHub organization owning ``repo``.
        clone_dir: Directory that holds per-repo clones for this run.
        dry_run: If True, log intent without running git.

    Returns:
        Path to the reusable repo clone (``clone_dir/<repo>``).

    """
    repo_url = f"https://github.com/{org}/{repo}.git"
    clone_path = clone_dir / repo
    git_dir = clone_path / ".git"

    if git_dir.exists():
        logger.info("  Reusing existing clone of %s; fetching latest...", repo)
        _git(["fetch", "--prune", "origin"], cwd=clone_path, dry_run=dry_run, check=False)
        return clone_path

    logger.info("  Cloning %s (once, reused for all its PRs)...", repo)
    _git(["clone", "--filter=blob:none", repo_url, str(clone_path)], cwd=clone_dir, dry_run=dry_run)
    return clone_path


def add_pr_worktree(
    repo_clone: Path,
    work: Path,
    branch: str,
    base: str,
    dry_run: bool = False,
) -> None:
    """Create a worktree for ``branch`` off the shared repo clone.

    Fetches the PR head and base refs into the shared clone, then adds a
    worktree at ``work`` checked out to ``branch`` tracking ``origin/branch``.
    Any stale worktree at ``work`` is removed first so reruns are idempotent.

    Args:
        repo_clone: Path to the reusable repo clone (from :func:`ensure_repo_clone`).
        work: Destination worktree path (per-PR, outside the clone).
        branch: PR head branch name.
        base: PR base branch name.
        dry_run: If True, log intent without running git.

    """
    _git(["fetch", "origin", branch], cwd=repo_clone, dry_run=dry_run)
    _git(["fetch", "origin", base], cwd=repo_clone, dry_run=dry_run)

    # Idempotent: drop any leftover worktree at this path before re-adding.
    remove_worktree(repo_clone, work, dry_run=dry_run)
    _git(
        ["worktree", "add", "--force", "-B", branch, str(work), f"origin/{branch}"],
        cwd=repo_clone,
        dry_run=dry_run,
    )


def remove_worktree(repo_clone: Path, work: Path, dry_run: bool = False) -> None:
    """Remove a per-PR worktree, leaving the shared clone intact.

    Best-effort: a missing or already-removed worktree is not an error.

    Args:
        repo_clone: Path to the reusable repo clone.
        work: Worktree path to remove.
        dry_run: If True, log intent without running git.

    """
    if not work.exists():
        return
    _git(
        ["worktree", "remove", "--force", str(work)],
        cwd=repo_clone,
        dry_run=dry_run,
        check=False,
    )


def merge_pr(pr: PRInfo, org: str, dry_run: bool = False) -> bool:
    """Merge a ready PR via merge commit (GitHub signs the merge commit)."""
    logger.info("  Merging PR #%d via merge commit...", pr.number)
    try:
        _gh(
            ["pr", "merge", str(pr.number), "--merge", "--auto"],
            repo=pr.repo,
            org=org,
            dry_run=dry_run,
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.error("  Failed to merge PR #%d: %s", pr.number, e.stderr)
        return False


def rebase_and_resign(
    pr: PRInfo,
    repo_clone: Path,
    dry_run: bool = False,
    *,
    symbols: Symbols = UNICODE_SYMBOLS,
) -> bool:
    """Fetch PR branch, rebase it on origin/base, re-sign all commits, push.

    Operates in a per-PR worktree off the shared ``repo_clone`` (#1044) rather
    than cloning the whole repo again.
    """
    branch = pr.head_ref
    base = pr.base_ref
    work = repo_clone.parent / f"{pr.repo}-{pr.number}"

    try:
        add_pr_worktree(repo_clone, work, branch, base, dry_run=dry_run)

        result = _git(
            ["rebase", f"origin/{base}", "--exec", get_resign_exec()],
            cwd=work,
            dry_run=dry_run,
            check=False,
        )

        if result.returncode != 0:
            logger.warning(
                "  Rebase failed for PR #%d %s conflict detected", pr.number, symbols.dash
            )
            _git(["rebase", "--abort"], cwd=work, dry_run=dry_run, check=False)
            return False

        _git(["push", "--force-with-lease", "origin", branch], cwd=work, dry_run=dry_run)
        logger.info("  %s Rebased and re-signed PR #%d", symbols.check, pr.number)
        return True

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.error("  Rebase/push failed for PR #%d: %s", pr.number, e.stderr or str(e))
        return False
    finally:
        remove_worktree(repo_clone, work, dry_run=dry_run)


def _run_conflict_agent(agent: str, prompt: str, work: Path, pr_number: int) -> bool:
    """Run the selected conflict-resolution agent."""
    if uses_direct_agent_runner(agent):
        result = run_agent_text(
            agent=agent,
            prompt=prompt,
            cwd=work,
            timeout=2400,
            model=direct_agent_model(agent, "HEPH_IMPLEMENTER_MODEL"),
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
    org: str,
    repo_clone: Path,
    dry_run: bool = False,
    agent: str = "claude",
    *,
    symbols: Symbols = UNICODE_SYMBOLS,
) -> bool:
    """Spawn the selected agent to semantically resolve merge conflicts, then re-sign.

    Operates in a per-PR worktree off the shared ``repo_clone`` (#1044) rather
    than cloning the whole repo again.
    """
    branch = pr.head_ref
    base = pr.base_ref
    work = repo_clone.parent / f"{pr.repo}-{pr.number}-conflict"

    try:
        # Conflict inspection needs a real checkout even under --dry-run (the
        # agent spawn is what's gated, not the rebase that surfaces conflicts).
        # ensure_repo_clone is idempotent, so this reuses the shared clone if
        # already present and only forces a real clone when dry-run skipped it.
        repo_clone = ensure_repo_clone(pr.repo, org, repo_clone.parent, dry_run=False)
        add_pr_worktree(repo_clone, work, branch, base, dry_run=False)

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

Repository: {org}/{pr.repo}
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
            logger.info("  %s Conflict resolved and pushed for PR #%d", symbols.check, pr.number)
            return True

        logger.warning("  Agent did not push branch for PR #%d", pr.number)
        return False

    except Exception as e:
        logger.error("  Conflict resolution failed for PR #%d: %s", pr.number, e)
        with contextlib.suppress(Exception):
            _git(["rebase", "--abort"], cwd=work, dry_run=False, check=False)
        return False
    finally:
        remove_worktree(repo_clone, work, dry_run=dry_run)


def process_repo(
    repo: str,
    org: str,
    args: argparse.Namespace,
    clone_dir: Path,
    *,
    symbols: Symbols = UNICODE_SYMBOLS,
) -> dict[str, int]:
    """Process all open PRs in one repo. Returns counts by outcome."""
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

    # Clone the repo at most once per run; PR handlers use worktrees off it (#1044).
    # Only repos with PRs that actually need a checkout pay the clone cost — lazily
    # created on first OUTDATED/CONFLICTED PR below.
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


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for hephaestus-fleet-sync.

    Extracted from ``main`` so the production parser (including the
    ``--ascii`` flag) can be inspected directly by unit tests.

    Returns:
        The fully configured :class:`argparse.ArgumentParser`.

    """
    parser = argparse.ArgumentParser(
        description="Sync all PRs across a configurable GitHub organization's fleet",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    parser.add_argument(
        "--org",
        metavar="ORG",
        default=None,
        help="GitHub organization (overrides FLEET_ORG and .fleet.yml)",
    )
    parser.add_argument(
        "--repos",
        nargs="+",
        metavar="REPO",
        default=None,
        help="Restrict to specific repos (overrides FLEET_REPOS and .fleet.yml)",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        type=str,
        default=None,
        help="Path to fleet config YAML (default: ./.fleet.yml then repo-root .fleet.yml)",
    )
    parser.add_argument(
        "--skip-conflict-resolution",
        action="store_true",
        help="Skip agent conflict resolution for conflicted PRs",
    )
    add_agent_argument(parser)
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    parser.add_argument(
        "--ascii",
        action="store_true",
        help=(
            "Use ASCII fallbacks (==, *, ->, --) instead of Unicode "
            "box/check/arrow/dash glyphs in log output; use when piping "
            "stdout to ASCII-only consumers."
        ),
    )
    add_github_throttle_args(parser)
    add_json_arg(parser)
    add_version_arg(parser)
    return parser


def main() -> int:
    """Entry point for hephaestus-fleet-sync."""
    parser = _build_parser()
    args = parser.parse_args()
    configure_github_throttle_from_args(args)
    args.agent = resolve_agent(args.agent)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        org, repos = resolve_fleet_config(args.org, args.repos, args.config)
    except RuntimeError as e:
        logger.error("%s", e)
        if args.json:
            emit_json_status(2, str(e))
        return 2

    args.org = org
    args.repos = repos
    dry_tag = " [DRY RUN]" if args.dry_run else ""
    symbols = ASCII_SYMBOLS if args.ascii else UNICODE_SYMBOLS
    logger.info("Fleet sync %s org=%s, %d repo(s)%s", symbols.dash, org, len(repos), dry_tag)

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
            counts = process_repo(repo, org, args, clone_dir, symbols=symbols)
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
