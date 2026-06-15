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
        pr_merge_state_provider: Callable[[Any], tuple[str, str]] | None = None,
    ) -> None:
        """Initialise the collaborator with narrow provider callables.

        Args:
            options_provider: Returns the current CIDriverOptions.
            status_tracker_provider: Returns the current StatusTracker.
            repo_root_provider: Returns the repo root Path.
            pr_merge_state_provider: Resolves ``(mergeStateStatus, mergeable)``
                for a single PR. Injected as a lambda wrapping the driver's
                ``_pr_merge_state`` delegation stub so ``patch.object(driver,
                "_pr_merge_state")`` intercepts at call time (#1357). Defaults
                to this collaborator's own :meth:`pr_merge_state` when not wired.

        """
        self._options = options_provider
        self._status = status_tracker_provider
        self._repo_root = repo_root_provider
        self._pr_merge_state_fn = pr_merge_state_provider
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

    def list_open_prs_remaining(self) -> list[dict[str, Any]]:
        """Return the list of open PRs left on the repo after the drive (#838).

        A repo is only truly "driven" when there are zero open PRs left. The
        per-issue ``_drive_issue`` loop's notion of success — every issue's
        PR moved to green and/or got auto-merge enabled — does NOT imply the
        repo is clean: PRs that have not yet merged (auto-merge waiting on
        CI), PRs from issues outside the input set, and PRs opened by
        humans/other-automation all leave open work behind.

        Uses ``gh api --paginate`` so the result is the FULL set of open PRs,
        not a capped prefix. A repo with hundreds of dependabot PRs would
        otherwise pass the done-check after looking at only 100 of them.

        Returns:
            One dict per open PR with keys ``number``, ``title``,
            ``headRefName``, ``autoMergeRequest`` (None or the auto-merge
            metadata blob), and ``mergeStateStatus`` / ``mergeable`` (the
            per-PR merge-state, fetched separately because the REST list
            endpoint does not populate ``mergeable`` reliably — see #1328).
            Empty list iff the repo is clean.

        """
        options = self._options()
        repo_root = self._repo_root()
        try:
            owner, repo = get_repo_info(repo_root)
        except RuntimeError as exc:
            logger.error("Could not resolve repo owner/name to list open PRs: %s", exc)
            # Unknown ownership ⇒ treat as not-done so operators investigate.
            return [{"number": -1, "title": "(unknown: cannot resolve repo)"}]

        # ``gh api --paginate`` walks ``Link: rel="next"`` headers and emits
        # a single concatenated JSON array across all pages. ``per_page=100``
        # is GitHub's max page size; we issue the minimum number of calls.
        # We use ``gh api`` directly (not ``gh pr list``) because the latter
        # caps at ``--limit`` even with paginate semantics; gh's REST proxy
        # paginates without an upper bound.
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
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            # If we cannot determine the open-PR count, the safest default is
            # to assume the repo is NOT done — surface the unknown state as a
            # failure so operators don't walk away on a false-green.
            logger.error("Could not list open PRs to verify repo done-state: %s", exc)
            return [{"number": -1, "title": "(unknown: gh api pulls failed)"}]

        # The REST shape exposes ``head.ref`` and ``auto_merge`` (snake_case);
        # normalise to the gh-CLI shape consumers downstream already use.
        viewer = "" if options.include_all_authors else self.resolve_viewer_login()
        normalised: list[dict[str, Any]] = []
        for pr in raw_pulls:
            user = pr.get("user") or {}
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
                continue  # #821: hide other-author PRs from the done-gate sweep
            labels = pr.get("labels") or []
            number = pr.get("number")
            # Route through the injected provider (lambda → driver stub) so
            # ``patch.object(driver, "_pr_merge_state")`` intercepts (#1357);
            # fall back to the local method when unwired.
            merge_state_fn = self._pr_merge_state_fn or self.pr_merge_state
            merge_state, mergeable = merge_state_fn(number)
            normalised.append(
                {
                    "number": number,
                    "title": pr.get("title", ""),
                    "headRefName": (pr.get("head") or {}).get("ref", ""),
                    "autoMergeRequest": pr.get("auto_merge"),
                    "mergeStateStatus": merge_state,
                    "mergeable": mergeable,
                    "labels": [
                        label.get("name", "") for label in labels if isinstance(label, dict)
                    ],
                    "isBot": user.get("type") == "Bot",
                }
            )
        return normalised

    def pr_merge_state(self, pr_number: Any) -> tuple[str, str]:
        """Return ``(mergeStateStatus, mergeable)`` for a single PR (#1328).

        The REST ``/pulls`` list endpoint does NOT populate ``mergeable`` /
        ``mergeable_state`` reliably (GitHub computes the merge-state lazily and
        omits it from list responses), so the done-gate cannot tell a
        permanently-CONFLICTING armed PR apart from one that is genuinely still
        merging. A per-PR ``gh pr view`` forces GitHub to compute the merge
        state, matching how the rest of the driver queries merge-state.

        Args:
            pr_number: PR number to query.

        Returns:
            ``(mergeStateStatus, mergeable)`` upper-cased. Empty strings when the
            number is the unknown-marker sentinel or the query fails — an unknown
            merge-state must never be misread as CONFLICTING.

        """
        if not isinstance(pr_number, int) or pr_number < 0:
            return "", ""
        try:
            result = _gh_call(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--json",
                    "mergeStateStatus,mergeable",
                ],
                check=False,
            )
            state = dict(json.loads(result.stdout or "{}"))
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            logger.warning(
                "Could not fetch PR #%s merge-state for done-gate; treating as unknown: %s",
                pr_number,
                exc,
            )
            return "", ""
        return (
            str(state.get("mergeStateStatus") or "").upper(),
            str(state.get("mergeable") or "").upper(),
        )
