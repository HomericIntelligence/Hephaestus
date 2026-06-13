"""PR discovery collaborator extracted from CIDriver (refs #1179).

Owns viewer-login caching and all PR enumeration strategies:
- issue-driven (Closes #N links)
- bot-PR (Dependabot, github-actions)
- failing-PR (any open non-draft PR whose checks are red)
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from typing import Any

from .git_utils import get_repo_info
from .github_api import GitHubUnavailableError, _gh_call

logger = logging.getLogger(__name__)


class PRDiscovery:
    """Discovers open PRs via multiple strategies using narrow-callable injection.

    Receives provider callables instead of the full CIDriver to satisfy DIP and
    avoid bidirectional coupling (refs #1179 MAJOR finding 2).
    """

    def __init__(
        self,
        *,
        options_provider: Callable[[], Any],
        status_tracker_provider: Callable[[], Any],
        repo_root_provider: Callable[[], Any],
    ) -> None:
        """Initialise the collaborator with narrow provider callables.

        Args:
            options_provider: Returns the current CIDriverOptions.
            status_tracker_provider: Returns the current StatusTracker.
            repo_root_provider: Returns the repo root Path.

        """
        self._options = options_provider
        self._status = status_tracker_provider
        self._repo_root = repo_root_provider
        # Viewer-login cache owned here (#821). Empty string = not yet resolved.
        self._viewer_login: str = ""

    def resolve_viewer_login(self) -> str:
        """Return the authenticated ``gh api user`` login. Fail CLOSED on error.

        Lazy + cached: only called when the author filter is active. Raises
        ``RuntimeError`` with operator guidance on any failure so a broken
        ``gh`` auth never silently widens scope to all PRs (#821 POLA).

        Returns:
            Authenticated GitHub login string.

        Raises:
            RuntimeError: When ``gh api user`` fails or returns empty output.

        """
        if self._viewer_login:
            return self._viewer_login
        try:
            result = _gh_call(["api", "user", "-q", ".login"], check=True)
        except (
            subprocess.CalledProcessError,
            FileNotFoundError,
            GitHubUnavailableError,
        ) as exc:
            raise RuntimeError(
                "Could not resolve viewer login via `gh api user`: "
                f"{exc}. Re-authenticate with `gh auth login`, or pass "
                "--all to opt out of the @me filter (#821)."
            ) from exc
        login = (result.stdout or "").strip()
        if not login:
            raise RuntimeError(
                "Could not resolve viewer login via `gh api user`: "
                "empty response. Re-authenticate with `gh auth login`, "
                "or pass --all to opt out of the @me filter (#821)."
            )
        self._viewer_login = login
        return login

    def discover_bot_prs(self) -> dict[int, int]:
        """Enumerate every open ``is_bot=true`` PR on the repo (#848).

        Bot PRs (Dependabot, github-actions, etc.) carry NO ``Closes #N``
        link to an issue, so the issue-driven discovery path can never see
        them — they are architecturally invisible. Without this enumeration
        a repo can sit with dozens of stranded Dependabot PRs forever while
        the ecosystem script cheerfully reports "driven" because every
        listed issue had no matching PR.

        Returns a mapping where each bot PR's number is used both as the
        synthetic issue key AND the PR number. Downstream code is taught
        (``_is_bot_pr_mode``) to detect the equality and skip issue-data
        fetches that would 404 on a synthetic key.

        Returns:
            Mapping of ``pr_number -> pr_number`` for every open bot PR.
            Empty dict if the ``gh api`` pulls lookup fails or returns
            nothing — bot discovery must never abort the drive on a list
            failure.

        Raises:
            RuntimeError: When the default @me author filter is active
                (``--all`` not set) and viewer-login resolution fails. This
                fail-CLOSED abort is intentional per #821 (POLA): a broken
                ``gh auth`` must never silently widen scope to every author's
                PRs. Pass ``--all`` to opt out of the filter and bypass the
                resolver entirely.

        """
        options = self._options()
        repo_root = self._repo_root()
        try:
            owner, repo = get_repo_info(repo_root)
        except RuntimeError as exc:
            logger.info("Bot-PR discovery skipped: could not resolve owner/name (%s)", exc)
            return {}

        try:
            result = _gh_call(
                [
                    "api",
                    "--paginate",
                    f"/repos/{owner}/{repo}/pulls?state=open&per_page=100",
                ],
                check=False,
            )
            raw_pulls: list[dict[str, Any]] = json.loads(result.stdout or "[]")
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            logger.info("Bot-PR discovery skipped: gh api failed (%s)", exc)
            return {}

        viewer = "" if options.include_all_authors else self.resolve_viewer_login()
        bot_prs: dict[int, int] = {}
        for pr in raw_pulls:
            user = pr.get("user") or {}
            if user.get("type") != "Bot":
                continue
            if viewer and user.get("login") != viewer:
                if user.get("login") is None:
                    logger.warning(
                        "PR #%s has no user.login; skipping under author filter (#821)",
                        pr.get("number"),
                        extra={
                            "missing_field": "user.login",
                            "filter": "author",
                            "pr_number": pr.get("number"),
                        },
                    )
                continue  # #821: not viewer-owned and --all not set
            number = pr.get("number")
            if isinstance(number, int):
                bot_prs[number] = number

        if bot_prs:
            logger.info(
                "Discovered %s open bot-authored PR(s): %s",
                len(bot_prs),
                sorted(bot_prs),
            )
        return bot_prs

    def discover_failing_prs(
        self, _pr_is_failing: Callable[[dict[str, Any]], bool]
    ) -> dict[int, int]:
        """Enumerate open non-draft PRs whose checks failed or merge is BLOCKED.

        Symmetrical to ``discover_bot_prs``: the issue→PR direction (Closes #N)
        misses every PR with no Closes line and every PR linked to a closed
        issue. One CLI call, PR-keyed, synthetic-issue invariant (pr_number ==
        issue_number) so downstream ``is_bot_pr_mode`` short-circuits ``gh issue
        view`` identically to the bot path.

        Bounded by gh's --limit 1000 (its documented hard upper). A repo with
        more than 1000 failing open PRs is pathological — we log a WARNING
        so operators see the truncation rather than silently dropping work.

        Args:
            _pr_is_failing: Module-level predicate that determines if a PR row
                should be picked up for driving.

        Returns:
            Mapping pr_number -> pr_number for every failing open PR.
            Empty dict on any lookup failure — discovery must never abort
            the drive.

        """
        repo_root = self._repo_root()
        try:
            owner, repo = get_repo_info(repo_root)
        except RuntimeError as exc:
            logger.info("Failing-PR discovery skipped: could not resolve owner/name (%s)", exc)
            return {}
        try:
            result = _gh_call(
                [
                    "pr",
                    "list",
                    "--repo",
                    f"{owner}/{repo}",
                    "--state",
                    "open",
                    "--limit",
                    "1000",
                    "--json",
                    "number,isDraft,statusCheckRollup,mergeStateStatus",
                ],
            )
            pulls: list[dict[str, Any]] = json.loads(result.stdout or "[]")
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            logger.info("Failing-PR discovery skipped: gh pr list failed (%s)", exc)
            return {}
        if len(pulls) >= 1000:
            logger.warning(
                "Failing-PR discovery hit gh's 1000-PR cap on %s/%s — "
                "additional failing PRs may exist and are not visible to this run.",
                owner,
                repo,
            )
        failing: dict[int, int] = {}
        for pr in pulls:
            number = pr.get("number")
            if not isinstance(number, int):
                continue
            if _pr_is_failing(pr):
                failing[number] = number
        if failing:
            logger.info(
                "Discovered %s open failing PR(s): %s",
                len(failing),
                sorted(failing),
            )
        return failing

    def is_bot_pr_mode(self, issue_number: int, pr_number: int) -> bool:
        """Return True iff this work item is a synthetic-issue bot PR (#848).

        The bot-PR enumeration uses the PR number as a stand-in for an
        issue number because Dependabot PRs have no associated issue.
        Anywhere we would normally call ``gh issue view <issue_number>``
        we must instead short-circuit; this helper centralises the check
        so a single rule (issue == pr) keeps both ends honest.

        Args:
            issue_number: GitHub issue number (may be synthetic).
            pr_number: GitHub PR number.

        Returns:
            True when issue_number equals pr_number (synthetic-issue invariant).

        """
        return issue_number == pr_number
