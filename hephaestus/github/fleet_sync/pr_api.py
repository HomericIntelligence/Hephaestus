"""GitHub PR API operations and readiness classification for fleet sync."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from hephaestus.github.client import gh_call
from hephaestus.github.fleet_sync.models import PRInfo, PRStatus
from hephaestus.logging.utils import get_logger
from hephaestus.utils.helpers import NETWORK_TIMEOUT

logger = get_logger(__name__)


def _gh(
    args: list[str],
    repo: str | None = None,
    org: str | None = None,
    check: bool = True,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run gh CLI, optionally scoped to a repo, routed through the shared adapter."""
    full_args = args
    if repo and not any(a.startswith("--repo") or a == "-R" for a in args):
        if not org:
            raise ValueError("org must be provided when repo is specified")
        repo_arg = f"{org}/{repo}"
        full_args = ["--repo", repo_arg, *args]

    if dry_run:
        logger.info("[dry-run] gh %s", " ".join(full_args))
        return subprocess.CompletedProcess(full_args, 0, stdout="[]", stderr="")

    return gh_call(full_args, check=check, timeout=NETWORK_TIMEOUT)


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
    """Fetch a single PR's statusCheckRollup and reduce it to a CI state."""
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
    """List all open PRs in a repo with their readiness status."""
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
        raise RuntimeError(f"fleet_sync: could not list PRs for {repo}: {e}") from e

    out: list[PRInfo] = []

    for p in prs_raw:
        ci = _fetch_pr_ci_state(repo, p["number"], org)
        mergeable = p.get("mergeable", "UNKNOWN")
        merge_state = p.get("mergeStateStatus", "UNKNOWN")

        if mergeable == "CONFLICTING":
            status = PRStatus.CONFLICTED
        elif ci == "FAILURE" and merge_state == "CLEAN":
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


def merge_pr(pr: PRInfo, org: str, dry_run: bool = False) -> bool:
    """Refuse fleet-sync automatic merging until the strict-review gate exists."""
    del org, dry_run
    logger.error(
        "  Refusing to merge PR #%d while the strict-review gate is unavailable", pr.number
    )
    return False
