"""Pull-request CI check helpers."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import hephaestus.automation.github_api as _api

_PR_CHECK_BUCKET_MAP: dict[str, tuple[str, str | None]] = {
    "pass": ("completed", "success"),
    "fail": ("completed", "failure"),
    "cancel": ("completed", "failure"),
    "skipping": ("completed", "skipped"),
    "pending": ("in_progress", None),
}


def _map_pr_check(item: dict[str, Any]) -> dict[str, Any]:
    """Map one raw ``gh pr checks --json`` entry onto the status/conclusion contract."""
    bucket = str(item.get("bucket", "")).lower()
    status, conclusion = _PR_CHECK_BUCKET_MAP.get(bucket, ("in_progress", None))
    return {
        "name": item.get("name", ""),
        "status": status,
        "conclusion": conclusion,
        "required": False,
    }


_GH_PR_CHECKS_NO_CHECKS_FRAGMENT: str = "no checks reported"


def _is_gh_pr_checks_no_checks_error(exc: subprocess.CalledProcessError) -> bool:
    """Return True iff a failed ``gh pr checks`` is the no-checks-yet case."""
    blob = (exc.stderr or "") + (exc.stdout or "")
    return _GH_PR_CHECKS_NO_CHECKS_FRAGMENT in blob


def gh_pr_checks(
    pr_number: int,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Get CI check results for a PR.

    Args:
        pr_number: PR number
        dry_run: If True, return empty list

    Returns:
        List of check dicts with keys: name (str), status (str), conclusion (str | None),
        required (bool). Empty list if the PR has no check runs yet (``gh pr checks``
        treats this as an error but the driver treats it as the empty case).

        ``gh pr checks --json`` does not expose ``status``/``conclusion``/``required`` — it
        exposes ``state`` (e.g. ``SUCCESS``/``FAILURE``/``PENDING``) and ``bucket``
        (``pass``/``fail``/``pending``/``skipping``/``cancel``). Those are mapped here onto the
        ``status``/``conclusion`` keys this module's consumers expect. ``required`` is not in the
        schema, so it defaults to ``False`` (callers treat "no required checks" as "all required").

    """
    if dry_run:
        _api.logger.info("[dry_run] Would fetch CI checks for PR #%s", pr_number)
        return []

    try:
        # #1587: "no checks reported" is the expected empty state right after a
        # push. ``log_on_error=False`` suppresses the spurious ERROR log for that
        # case; the "no checks reported" non-transient pattern (github.client)
        # makes _gh_call fail FAST (no exponential-backoff retry) so we reach the
        # except below immediately. A genuine failure still raises after retries.
        result = _api._gh_call(
            ["pr", "checks", str(pr_number), "--json", "name,state,bucket,workflow"],
            log_on_error=False,
        )
    except subprocess.CalledProcessError as exc:
        if _api._is_gh_pr_checks_no_checks_error(exc):
            _api.logger.info(
                "PR #%s has no check runs registered yet (gh: %s); treating as empty",
                pr_number,
                _GH_PR_CHECKS_NO_CHECKS_FRAGMENT,
            )
            return []
        raise
    raw: list[dict[str, Any]] = json.loads(result.stdout)

    checks: list[dict[str, Any]] = [_api._map_pr_check(item) for item in raw]

    _api.logger.debug("Fetched %s CI check(s) for PR #%s", len(checks), pr_number)
    return checks
