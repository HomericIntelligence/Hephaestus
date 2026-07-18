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
from dataclasses import dataclass
from typing import Any

from ._review_utils import _discover_prs_simple, find_pr_for_issue
from .ci_check_inspector import FAILING_CHECK_CONCLUSIONS
from .git_utils import get_repo_info
from .github_api import GitHubUnavailableError, _gh_call

logger = logging.getLogger(__name__)


def _resolve_viewer_login() -> str:
    """Resolve the authenticated GitHub login, failing closed on errors."""
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
    return login


def _is_bot_pr_author(pr: dict[str, Any]) -> bool:
    """Return whether a pull-request row was authored by a GitHub bot."""
    user = pr.get("user")
    return isinstance(user, dict) and user.get("type") == "Bot"


def _is_viewer_authored(pr: dict[str, Any], viewer_login: str) -> bool:
    """Return whether a pull-request row belongs to the selected author scope."""
    user = pr.get("user")
    return not viewer_login or (isinstance(user, dict) and user.get("login") == viewer_login)


def pr_needs_loop_review(pr: dict[str, Any]) -> bool:
    """Return whether an open non-draft PR is eligible for loop review.

    This intentionally does not read a check, workflow, status, or merge
    state. The loop's own review and approval label are its entire input.
    """
    state = str(pr.get("state", "OPEN")).upper()
    is_draft = bool(pr.get("isDraft", pr.get("draft", False)))
    return not is_draft and state == "OPEN"


@dataclass(frozen=True)
class PRWorkset:
    """Resolved PR workset for a drive-green run."""

    pr_map: dict[int, int]
    shared_pr_issues: dict[int, list[int]]


def _dedupe_issue_prs(raw_map: dict[int, int]) -> PRWorkset:
    """Collapse many issues that resolve to the same PR into one work item."""
    pr_to_issues: dict[int, list[int]] = {}
    for issue_num, pr_num in raw_map.items():
        pr_to_issues.setdefault(pr_num, []).append(issue_num)

    shared = {pr: sorted(issues) for pr, issues in pr_to_issues.items()}
    deduped: dict[int, int] = {}
    for pr_num, issues in pr_to_issues.items():
        canonical = min(issues)
        deduped[canonical] = pr_num
        if len(issues) > 1:
            deferred = sorted(i for i in issues if i != canonical)
            logger.info(
                "PR #%s closes multiple issues %s; driving via issue #%s, "
                "deferring %s (single PR cannot be checked out into multiple "
                "worktrees concurrently)",
                pr_num,
                sorted(issues),
                canonical,
                deferred,
            )
    return PRWorkset(pr_map=deduped, shared_pr_issues=shared)


def _fetch_open_pulls(repo_root: Any, *, purpose: str) -> tuple[list[dict[str, Any]], bool] | None:
    """Fetch open REST rows plus whether malformed rows were discarded."""
    try:
        owner, repo = get_repo_info(repo_root)
    except RuntimeError as exc:
        logger.info("%s skipped: could not resolve owner/name (%s)", purpose, exc)
        return None
    try:
        result = _gh_call(
            [
                "api",
                "--paginate",
                f"/repos/{owner}/{repo}/pulls?state=open&per_page=100",
            ],
            check=False,
        )
        if result.returncode != 0:
            logger.error(
                "Could not list open PRs for %s: gh api exited %s: %s",
                purpose,
                result.returncode,
                (result.stderr or "").strip(),
            )
            return None
        stdout = result.stdout
        if not isinstance(stdout, str) or not stdout.strip():
            logger.error("Could not list open PRs for %s: empty response", purpose)
            return None
        raw_pulls = json.loads(stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        logger.error("Could not list open PRs for %s: %s", purpose, exc)
        return None
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.info("%s skipped: gh api failed (%s)", purpose, exc)
        return None
    if not isinstance(raw_pulls, list):
        logger.error("Could not list open PRs for %s: invalid response shape", purpose)
        return None
    pulls = [pr for pr in raw_pulls if isinstance(pr, dict)]
    malformed_rows = len(pulls) != len(raw_pulls)
    if malformed_rows:
        logger.error("Could not fully list open PRs for %s: malformed pull row", purpose)
    return pulls, malformed_rows


def _normalise_open_pr(
    pr: dict[str, Any],
    *,
    merge_state_fn: Callable[[Any], tuple[str, str]],
) -> dict[str, Any]:
    """Normalise a REST pull row to the gh-CLI shape consumed downstream."""
    labels_value = pr.get("labels")
    labels = labels_value if isinstance(labels_value, list) else []
    head_value = pr.get("head")
    head = head_value if isinstance(head_value, dict) else {}
    head_ref = head.get("ref")
    user_value = pr.get("user")
    user = user_value if isinstance(user_value, dict) else {}
    number = pr.get("number")
    merge_state, mergeable = merge_state_fn(number)
    return {
        "number": number,
        "title": pr.get("title", ""),
        "headRefName": head_ref if isinstance(head_ref, str) else "",
        "autoMergeRequest": pr.get("auto_merge"),
        "mergeStateStatus": merge_state,
        "mergeable": mergeable,
        "labels": [label.get("name", "") for label in labels if isinstance(label, dict)],
        "isBot": user.get("type") == "Bot",
    }


def _pr_is_failing_row(pr: dict[str, Any]) -> bool:
    """Return True iff a PR row should be picked up by failing-PR discovery."""
    if not pr_needs_loop_review(pr):
        return False
    if pr.get("mergeStateStatus") == "BLOCKED":
        return True
    rollup = pr.get("statusCheckRollup") or []
    return any(c.get("conclusion") in FAILING_CHECK_CONCLUSIONS for c in rollup)


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

    def discover_workset(self, issue_numbers: list[int]) -> PRWorkset:
        """Pre-discover open PRs from issue, direct-PR, bot, and failing sources."""
        raw_map = _discover_prs_simple(
            issue_numbers,
            find_pr_for_issue,
            on_missing=lambda issue_num: logger.info(
                "Issue #%s: no open PR found, skipping", issue_num
            ),
        )
        workset = _dedupe_issue_prs(raw_map)
        deduped = dict(workset.pr_map)
        shared = {pr: list(issues) for pr, issues in workset.shared_pr_issues.items()}

        for pr_num in self._options().prs:
            if pr_num in deduped.values():
                logger.info(
                    "Direct PR #%s already discovered via --issues; skipping duplicate",
                    pr_num,
                )
                continue
            if not self.validate_pr_open(pr_num):
                logger.warning("Direct PR #%s is not OPEN or does not exist; skipping", pr_num)
                continue
            deduped[pr_num] = pr_num
            shared.setdefault(pr_num, [pr_num])

        if self._options().include_bot_prs and not self._options().issues:
            for pr_num in self.discover_bot_prs():
                if pr_num not in deduped.values():
                    deduped[pr_num] = pr_num
                    shared.setdefault(pr_num, [pr_num])

        if not self._options().issues:
            known = set(deduped.values())
            for pr_num in self.discover_failing_prs(_pr_is_failing_row):
                if pr_num not in known:
                    deduped[pr_num] = pr_num
                    known.add(pr_num)
                    shared.setdefault(pr_num, [pr_num])
        return PRWorkset(pr_map=deduped, shared_pr_issues=shared)

    def validate_pr_open(self, pr_number: int) -> bool:
        """Return True iff ``pr_number`` exists and is in OPEN state."""
        try:
            result = _gh_call(
                ["pr", "view", str(pr_number), "--json", "number,state"],
                check=False,
            )
            if result.returncode != 0:
                return False
            data = json.loads(result.stdout or "{}")
            if not isinstance(data, dict):
                return False
            return str(data.get("state", "")).upper() == "OPEN"
        except (
            AttributeError,
            subprocess.CalledProcessError,
            json.JSONDecodeError,
            TypeError,
        ) as exc:
            logger.debug("PR #%s validation failed: %s", pr_number, exc)
            return False

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
        self._viewer_login = _resolve_viewer_login()
        return self._viewer_login

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
        fetched_pulls = _fetch_open_pulls(self._repo_root(), purpose="Bot-PR discovery")
        if fetched_pulls is None:
            return {}
        raw_pulls, _malformed_rows = fetched_pulls
        viewer = "" if self._options().include_all_authors else self.resolve_viewer_login()
        bot_prs: dict[int, int] = {}
        for pr in raw_pulls:
            if not _is_bot_pr_author(pr):
                continue
            if not _is_viewer_authored(pr, viewer):
                if (pr.get("user") or {}).get("login") is None:
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
        per-issue ``_drive_issue`` loop's notion of success does NOT imply the
        repo is clean: PRs awaiting manual strict review, PRs from issues
        outside the input set, and PRs opened by humans/other-automation all
        leave open work behind.

        Uses ``gh api --paginate`` so the result is the FULL set of open PRs,
        not a capped prefix. A repo with hundreds of dependabot PRs would
        otherwise pass the done-check after looking at only 100 of them.

        Returns:
            One dict per open PR with keys ``number``, ``title``,
            ``headRefName``, ``autoMergeRequest`` (None or the auto-merge
            metadata blob), and ``mergeStateStatus`` / ``mergeable`` (the
            per-PR merge-state, fetched separately because the REST list
            endpoint does not populate ``mergeable`` reliably — see #1328).
            Empty list iff the repo is clean. A lookup failure returns an
            unknown-record sentinel so the caller fails closed rather than
            treating an unverified repository as clean.

        """
        fetched_pulls = _fetch_open_pulls(self._repo_root(), purpose="open-PR done-state")
        if fetched_pulls is None:
            return [{"number": -1, "title": "(unknown: gh api pulls failed)"}]
        raw_pulls, malformed_rows = fetched_pulls
        if not raw_pulls and not malformed_rows:
            return []

        options = self._options()
        viewer = "" if options.include_all_authors or options.prs else self.resolve_viewer_login()
        normalised: list[dict[str, Any]] = []
        for pr in raw_pulls:
            user_value = pr.get("user")
            user = user_value if isinstance(user_value, dict) else {}
            login = user.get("login")
            if viewer and login != viewer:
                if login is None:
                    logger.warning(
                        "PR #%s has no usable user.login; retaining it for containment",
                        pr.get("number"),
                        extra={
                            "missing_field": "user.login",
                            "filter": "author",
                            "pr_number": pr.get("number"),
                        },
                    )
                else:
                    continue  # #821: hide known other-author PRs from the done-gate sweep
            # Route through the injected provider (lambda → driver stub) so
            # ``patch.object(driver, "_pr_merge_state")`` intercepts (#1357);
            # fall back to the local method when unwired.
            merge_state_fn = self._pr_merge_state_fn or self.pr_merge_state
            normalised.append(_normalise_open_pr(pr, merge_state_fn=merge_state_fn))
        if malformed_rows:
            normalised.append({"number": -1, "title": "(unknown: malformed gh api pull row)"})
        return normalised

    def pr_merge_state(self, pr_number: Any) -> tuple[str, str]:
        """Return ``(mergeStateStatus, mergeable)`` for a single PR (#1328).

        The REST ``/pulls`` list endpoint does NOT populate ``mergeable`` /
        ``mergeable_state`` reliably (GitHub computes the merge-state lazily and
        omits it from list responses), so the done-gate cannot tell a
        permanently-CONFLICTING open PR apart from one that is genuinely still
        pending. A per-PR ``gh pr view`` forces GitHub to compute the merge
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
            state = json.loads(result.stdout or "{}")
            if not isinstance(state, dict):
                raise ValueError("merge-state response was not an object")
        except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError, ValueError) as exc:
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
