"""CI check inspection collaborator extracted from CIDriver (refs #1179).

Owns the FAILING_CHECK_CONCLUSIONS constant and all methods that query
CI check state for a given PR.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from .github_api import _gh_call, gh_pr_checks

logger = logging.getLogger(__name__)

# Conclusion values that indicate a PR's check rollup is failing in a way
# drive-green can act on. SUCCESS / SKIPPED / NEUTRAL / PENDING are
# explicitly excluded. Shared with loop_runner._count_failing_prs so the
# SKIP gate and the actual work list never drift (#819).
FAILING_CHECK_CONCLUSIONS: frozenset[str] = frozenset({"FAILURE", "CANCELLED", "TIMED_OUT"})


class CICheckInspector:
    """Queries CI check state for a PR using narrow-callable injection.

    Receives ``get_pr_branch`` as a callable instead of the full CIDriver to
    satisfy DIP and avoid bidirectional coupling (refs #1179 MAJOR finding 2).
    """

    def __init__(
        self,
        *,
        get_pr_branch: Callable[[int], str],
        options_provider: Callable[[], Any],
    ) -> None:
        """Initialise the inspector with narrow provider callables.

        Args:
            get_pr_branch: Callable that resolves the head branch for a PR number.
            options_provider: Returns the current CIDriverOptions.

        """
        self._get_pr_branch = get_pr_branch
        self._options = options_provider

    def failing_required_check_names(self, pr_number: int) -> list[str]:
        """Return names of required checks that are currently failing.

        Used by the no-commit retry path (#846) to name the actual offenders
        verbatim in the force-engagement prompt. Returns an empty list if
        the lookup fails — the caller treats that as "cannot prove still
        red" and skips the retry rather than launching Claude blind.

        Args:
            pr_number: GitHub PR number.

        Returns:
            Names of required checks with ``conclusion == "failure"``.

        """
        options = self._options()
        try:
            checks = gh_pr_checks(pr_number, dry_run=options.dry_run)
        except Exception as exc:
            logger.info(
                "PR #%s: failed to re-check CI for no-commit retry decision (%s)",
                pr_number,
                exc,
            )
            return []
        if not checks:
            return []
        required = [c for c in checks if c.get("required")] or checks
        return [
            c.get("name", "")
            for c in required
            if c.get("status") == "completed" and c.get("conclusion") == "failure"
        ]

    def pending_required_check_names(self, pr_number: int) -> list[str]:
        """Return names of required checks that are still in flight (not completed).

        Used by the BLOCKED early-exit guard in ``_wait_for_pr_terminal`` to
        distinguish branch-protection blocks (all checks green but conversations
        unresolved) from pending-CI blocks (checks still running). Returns an
        empty list on lookup failure — the caller then conservatively assumes no
        checks are pending and exits the poll.

        Args:
            pr_number: GitHub PR number.

        Returns:
            Names of required checks whose ``status != "completed"``.

        """
        options = self._options()
        try:
            checks = gh_pr_checks(pr_number, dry_run=options.dry_run)
        except Exception as exc:
            logger.info(
                "PR #%s: failed to fetch CI checks for BLOCKED pending guard (%s)",
                pr_number,
                exc,
            )
            return []
        if not checks:
            return []
        required = [c for c in checks if c.get("required")] or checks
        return [c.get("name", "") for c in required if c.get("status") != "completed"]

    def get_failing_ci_logs(self, pr_number: int) -> str:
        """Fetch combined failure logs for recent failed CI runs on a PR.

        Scopes the ``gh run list`` query to the PR's head branch so we only
        see runs that belong to this PR rather than the most-recent repo-wide
        runs (the previous repo-wide query could return runs for other PRs
        and even other branches, making the logs useless for fixing *this* PR).

        Args:
            pr_number: GitHub PR number.

        Returns:
            Combined log string, truncated to 10 000 characters.

        """
        try:
            branch = self._get_pr_branch(pr_number)
            result2 = _gh_call(
                [
                    "run",
                    "list",
                    "--branch",
                    branch,
                    "--status",
                    "failure",
                    "--limit",
                    "10",
                    "--json",
                    "databaseId,conclusion,name,headSha",
                ],
                check=False,
            )
            runs: list[dict[str, Any]] = json.loads(result2.stdout or "[]")
            failed_runs = [r for r in runs if r.get("conclusion") == "failure"][:3]

            logs: list[str] = []
            for run_info in failed_runs:
                run_id = run_info.get("databaseId")
                run_name = run_info.get("name", str(run_id))
                if not run_id:
                    continue
                try:
                    log_result = _gh_call(
                        ["run", "view", str(run_id), "--log-failed"],
                        check=False,
                    )
                    logs.append(f"=== {run_name} ===\n{log_result.stdout[:3000]}")
                except Exception as log_err:
                    logger.debug("Could not fetch log for run %s: %s", run_id, log_err)

            return "\n\n".join(logs)[:10000]

        except Exception as e:
            logger.warning("Could not fetch CI logs for PR #%s: %s", pr_number, e)
            return ""
