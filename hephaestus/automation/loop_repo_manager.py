"""Repo-management helpers for the multi-repo automation loop.

Extracted from loop_runner.py (refs #1360 / umbrella #1179). This module
owns the cluster of functions that interact with GitHub's repo list API,
local git operations (clone, fetch, rebase), and open-issue/failing-PR
counting. All functions here shell out to ``gh`` or ``git``; their
pure-function helpers are unit-tested in
``tests/unit/automation/test_loop_repo_manager.py``.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from hephaestus.automation.ci_driver import _pr_is_failing
from hephaestus.automation.claude_timeouts import gh_cli_timeout
from hephaestus.resilience.subprocess_resilience import resilient_call
from hephaestus.utils.helpers import METADATA_TIMEOUT, NETWORK_TIMEOUT

LOG = logging.getLogger(__name__)


def _detect_cwd_repo() -> tuple[str | None, str | None]:
    """Return ``(org, repo_name)`` for the current working directory.

    Returns ``(None, None)`` when cwd is not inside a git repo or has no
    parseable github.com origin remote. ``org`` is parsed from
    ``git remote get-url origin``; ``repo_name`` is the basename of
    ``git rev-parse --show-toplevel``.
    """
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return (None, None)
    repo: str | None = Path(top).name or None

    org: str | None = None
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        url = ""

    host = ""
    path = ""
    parsed = urlparse(url)
    if parsed.scheme:
        host = (parsed.hostname or "").rstrip(".").lower()
        path = parsed.path.lstrip("/")
    elif "@" in url and ":" in url:
        # SCP-like git remote, e.g. git@github.com:org/repo.git
        after_at = url.split("@", 1)[1]
        host_part, path_part = after_at.split(":", 1)
        host = host_part.rstrip(".").lower()
        path = path_part.lstrip("/")

    if host == "github.com":
        parts = path.split("/", 1)
        if len(parts) == 2:
            org = parts[0] or None

    return (org, repo)


def _gh_list_repos(org: str) -> list[str]:
    """Return non-archived, non-fork repos for ``org``."""
    try:
        out = subprocess.run(
            [
                "gh",
                "repo",
                "list",
                org,
                "--no-archived",
                "--json",
                "name,isArchived,isFork",
                "--limit",
                "200",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=gh_cli_timeout(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"gh repo list {org} timed out after {exc.timeout}s") from exc
    if out.returncode != 0:
        raise SystemExit(f"gh repo list {org} failed (rc={out.returncode}): {out.stderr.strip()}")
    try:
        entries = json.loads(out.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"gh repo list returned invalid JSON: {exc}") from exc
    return [
        e["name"] for e in entries if not e.get("isArchived", False) and not e.get("isFork", False)
    ]


def _list_open_issue_numbers(org: str, repo: str) -> list[int]:
    """Return ALL open issue numbers in ``org/repo``, sorted ascending.

    This is the loop's single canonical issue-discovery call: the result is
    passed down to the plan/implement child phases via ``--issues`` so they do
    NOT each re-run their own ``gh issue list``. The scope is ALL open issues
    (no ``@me`` author/assignee filter) so it matches the child phases'
    ``gh_list_open_issues`` semantics exactly — the loop's convergence and
    failing-PR gates then agree with the work the phases actually do.

    Sorted ascending so the implementer phase processes oldest-first. Returns
    an empty list on any failure (rate limit, auth error, timeout) so callers
    fall back safely.
    """
    try:
        out = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                f"{org}/{repo}",
                "--state",
                "open",
                "--limit",
                "500",
                "--json",
                "number",
                "--jq",
                ".[].number",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=gh_cli_timeout(),
        )
    except subprocess.TimeoutExpired:
        return []
    if out.returncode != 0:
        return []
    return sorted(int(x) for x in out.stdout.split() if x.strip().isdigit())


def _count_open_issues(org: str, repo: str) -> int:
    """Return count of open issues in ``org/repo``."""
    return len(_list_open_issue_numbers(org, repo))


def _count_failing_prs(org: str, repo: str) -> int:
    """Return count of open PRs that need drive-green attention.

    Uses the same gh pr list shape and the same _pr_is_failing predicate
    that ci_driver._discover_failing_prs uses, so the loop runner's SKIP
    gate cannot drift from the driver's work list. Bounded by gh's
    --limit 1000; cap-hit is logged but still treated as "has work" since
    the actual driver discovery handles the same cap consistently.

    Returns 0 on any gh / parse / timeout failure so the SKIP gate is
    fail-closed (we don't run the driver when we can't confirm work).
    """
    try:
        out = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                f"{org}/{repo}",
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                "number,isDraft,statusCheckRollup,mergeStateStatus",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=gh_cli_timeout(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return 0
    if out.returncode != 0:
        return 0
    try:
        pulls = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return 0
    if len(pulls) >= 1000:
        LOG.warning(
            "[%s] _count_failing_prs hit gh's 1000-PR cap; gate may undercount",
            repo,
        )
    return sum(1 for pr in pulls if _pr_is_failing(pr))


def _sort_repos_by_open_count(org: str, repos: list[str]) -> list[str]:
    """Order repos ascending by open-issue count (smallest backlog first)."""
    counted: list[tuple[int, int, str]] = []
    for idx, repo in enumerate(repos):
        counted.append((_count_open_issues(org, repo), idx, repo))
    counted.sort()
    return [name for _, _, name in counted]


def _resolve_repo_dir(projects_dir: Path, repo: str) -> Path:
    """Return the local directory for ``repo`` under ``projects_dir``."""
    return projects_dir / repo


def _ensure_clone(org: str, repo: str, dest: Path) -> None:
    """Clone the repo into ``dest`` if not already present."""
    if (dest / ".git").exists():
        return
    LOG.info("Cloning %s/%s -> %s", org, repo, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Route the clone through resilient_call: a network blip retries with
    # backoff, while a true hang is bounded by NETWORK_TIMEOUT (#684).
    completed = resilient_call(
        subprocess.run,
        ["gh", "repo", "clone", f"{org}/{repo}", str(dest)],
        check=False,
        timeout=NETWORK_TIMEOUT,
        circuit_breaker_name="gh-repo-clone",
    )
    rc = completed.returncode
    if rc != 0:
        raise RuntimeError(f"gh repo clone {org}/{repo} failed (rc={rc})")


def _clone_missing_repos(org: str, repos: list[str], projects_dir: Path) -> None:
    """Sequentially clone any repos not already present.

    Done upfront — before any worker thread starts — so two threads with
    ``--parallel-repos > 1`` can never race on a missing clone. Matches
    the bash version's pre-loop clone pass at
    scripts/run_automation_loop.sh:326-336.
    """
    LOG.info("Cloning missing repos ...")
    for repo in repos:
        dest = projects_dir / repo
        if (dest / ".git").exists():
            LOG.debug("[%s] already cloned at %s", repo, dest)
            continue
        try:
            _ensure_clone(org, repo, dest)
        except Exception as exc:
            LOG.error("[%s] clone failed: %s — repo will be marked failed", repo, exc)


def _detect_remote_base_ref(repo: str, repo_dir: Path) -> str:
    """Return the remote default branch ref for ``repo_dir``."""
    try:
        symbolic = subprocess.run(
            ["git", "-C", str(repo_dir), "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
            capture_output=True,
            text=True,
            check=False,
            timeout=METADATA_TIMEOUT,
        )
        detected = symbolic.stdout.strip()
        if symbolic.returncode == 0 and detected:
            return detected
    except subprocess.TimeoutExpired:
        LOG.warning("[%s] default-branch detection timed out; trying fallback refs", repo)

    for candidate in ("origin/main", "origin/master"):
        try:
            verified = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "--verify", candidate],
                capture_output=True,
                text=True,
                check=False,
                timeout=METADATA_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            continue
        if verified.returncode == 0:
            LOG.warning("[%s] using fallback base ref %s", repo, candidate)
            return candidate
    LOG.warning("[%s] could not detect base ref; falling back to origin/main", repo)
    return "origin/main"


def _local_ahead_count(repo: str, repo_dir: Path, base_ref: str) -> int:
    """Return the number of commits on HEAD that are not in ``base_ref``."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-list", "--count", f"{base_ref}..HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=METADATA_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        LOG.warning("[%s] timed out checking local commits ahead of %s", repo, base_ref)
        return 0
    if result.returncode != 0:
        LOG.warning("[%s] could not check local commits ahead of %s", repo, base_ref)
        return 0
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        LOG.warning("[%s] invalid ahead count for %s: %r", repo, base_ref, result.stdout)
        return 0


def _rebase_main(repo: str, repo_dir: Path) -> tuple[str, bool]:
    """Fetch + rebase the remote default branch.

    Returns ``(short_sha, fetch_ok)`` — a 7-char SHA and a flag indicating
    whether the network refresh succeeded. When ``fetch_ok`` is False the
    rebase ran against whatever the local clone already had; callers should
    surface the staleness in operator-facing logs but the SHA value itself
    remains a clean git hash (no suffix) because it is exported to phase
    subprocesses as ``HEPH_TRUNK_GITHASH`` and used for session naming
    (``hephaestus/automation/session_naming.py:181``). Adding a suffix
    would propagate the "stale" marker into every child session label and
    would also break any future caller that consumed the env var as a git
    ref. The staleness is conveyed via the second return value instead.
    """
    fetch_ok = True
    try:
        fetch_result = resilient_call(
            subprocess.run,
            ["git", "-C", str(repo_dir), "fetch", "origin", "--quiet"],
            check=False,
            capture_output=True,
            text=True,
            timeout=NETWORK_TIMEOUT,
            circuit_breaker_name="git-fetch",
        )
    except subprocess.TimeoutExpired:
        LOG.warning("[%s] git fetch timed out; rebasing against stale remote base", repo)
        fetch_ok = False
    else:
        # subprocess.run(check=False) does NOT raise on non-zero rc. macOS
        # sandbox denials surface here as rc=1 with stderr "cannot open
        # .git/FETCH_HEAD: Operation not permitted" (#993). Without this
        # inspection the loop logs the resulting trunk SHA as if the refresh
        # succeeded, masking the permission problem.
        rc = getattr(fetch_result, "returncode", 0)
        if rc != 0:
            stderr = (getattr(fetch_result, "stderr", "") or "").strip()
            LOG.warning(
                "[%s] git fetch failed (rc=%s); rebasing against stale remote base: %s",
                repo,
                rc,
                stderr or "<no stderr>",
            )
            fetch_ok = False
    base_ref = _detect_remote_base_ref(repo, repo_dir)
    local_ahead = _local_ahead_count(repo, repo_dir, base_ref)
    rb = subprocess.run(
        ["git", "-C", str(repo_dir), "rebase", base_ref, "--quiet"],
        capture_output=True,
        text=True,
        check=False,
        timeout=METADATA_TIMEOUT,
    )
    if rb.returncode != 0:
        subprocess.run(
            ["git", "-C", str(repo_dir), "rebase", "--abort"],
            capture_output=True,
            check=False,
            timeout=METADATA_TIMEOUT,
        )
        if local_ahead > 0:
            LOG.warning(
                "[%s] rebase failed with %s local commit(s) ahead of %s; preserving HEAD",
                repo,
                local_ahead,
                base_ref,
            )
        else:
            # No local commits are at risk, so restore a clean remote-base trunk
            # and keep the loop moving.
            LOG.warning("[%s] rebase failed, hard-resetting to %s", repo, base_ref)
            subprocess.run(
                ["git", "-C", str(repo_dir), "reset", "--hard", base_ref, "--quiet"],
                capture_output=True,
                check=False,
                timeout=METADATA_TIMEOUT,
            )
    sha = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--short=7", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=METADATA_TIMEOUT,
    )
    return (sha.stdout.strip() or "unknown", fetch_ok)
