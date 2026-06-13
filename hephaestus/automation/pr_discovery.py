"""PR discovery collaborator: viewer-login caching + PR enumeration.

Provides issue-driven, bot-authored, and failing-PR discovery.  Extracted from
:class:`~hephaestus.automation.ci_driver.CIDriver` as a narrow SRP collaborator
receiving ``Callable[[], T]`` providers for shared mutable state (#1289).
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from typing import Any

from hephaestus.automation._review_utils import find_pr_for_issue
from hephaestus.automation.git_utils import get_repo_info, get_repo_root
from hephaestus.automation.github_api import GitHubUnavailableError, _gh_call
from hephaestus.automation.models import CIDriverOptions

logger = logging.getLogger(__name__)

# Shared with ci_driver.FAILING_CHECK_CONCLUSIONS — must stay in sync.
# Kept here as a local constant to avoid a circular import (ci_driver
# imports PRDiscovery; PRDiscovery must not import ci_driver).
_FAILING_CHECK_CONCLUSIONS: frozenset[str] = frozenset({"FAILURE", "CANCELLED", "TIMED_OUT"})


def _pr_is_failing_local(pr: dict[str, Any]) -> bool:
    """Return True iff this open PR row is one the driver should pick up."""
    if pr.get("isDraft"):
        return False
    if pr.get("mergeStateStatus") == "BLOCKED":
        return True
    rollup = pr.get("statusCheckRollup") or []
    return any(c.get("conclusion") in _FAILING_CHECK_CONCLUSIONS for c in rollup)


class PRDiscovery:
    """Handles viewer-login caching and all PR-enumeration strategies.

    Args:
        options: CI driver configuration options.
        shared_pr_issues_setter: Callable that replaces the shared
            ``pr_number -> [issue_numbers]`` mapping contents.
        shared_pr_issues_getter: Callable that returns the current shared
            mapping (same dict object throughout the run).

    """

    def __init__(
        self,
        *,
        options: CIDriverOptions,
        shared_pr_issues_setter: Callable[[dict[int, list[int]]], None],
        shared_pr_issues_getter: Callable[[], dict[int, list[int]]],
    ) -> None:
        """Initialize the discoverer; wire auto-merge and list-prs slots after construction."""
        self.options = options
        self._shared_pr_issues_setter = shared_pr_issues_setter
        self._shared_pr_issues_getter = shared_pr_issues_getter
        self._viewer_login: str = ""
        self.repo_root = get_repo_root()

    # ------------------------------------------------------------------
    # Open-PR unarmed-arm pass
    # ------------------------------------------------------------------

    def _arm_all_unarmed_open_prs(self, open_prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Arm auto-merge on every implementation-GO un-armed open PR (#882).

        The per-issue drive only arms PRs it processed; a PR fixed-and-pushed by
        the driver (or one that arrived green from another actor) can end CLEAN
        but un-armed and never merge. This pass arms only PRs already marked
        ``state:implementation-go`` so review approval remains the merge gate.

        Returns the open-PR list with ``autoMergeRequest`` refreshed for any PR
        that armed, so the caller's final gate reports the true state.
        """
        from hephaestus.automation.pr_manager import pr_has_implementation_go_label

        # Import delegated back through the stub on CIDriver so patch.object works
        # from hephaestus.automation.ci_driver import CIDriver  # avoided: circular
        # Instead call the enable_auto_merge provider via the caller-supplied handle.
        # However, _arm_all_unarmed_open_prs requires _enable_auto_merge and
        # _list_open_prs_remaining — both remain on CIDriver. This method is therefore
        # called as a delegation stub from CIDriver, which passes self as the invoker.
        # The implementation here should NOT be called directly in normal usage.
        armed_now: list[int] = []
        for pr in open_prs:
            number = pr.get("number")
            if not isinstance(number, int) or number < 0:
                continue
            if pr.get("autoMergeRequest"):
                continue  # already armed
            if not pr_has_implementation_go_label(pr):
                logger.info(
                    "PR #%s lacks state:implementation-go; leaving auto-merge disabled",
                    number,
                )
                continue
            if self._enable_auto_merge_fn(number, is_bot_pr=bool(pr.get("isBot"))):
                armed_now.append(number)
        if not armed_now:
            return open_prs
        logger.info(
            "Armed auto-merge on %d previously-unarmed open PR(s): %s",
            len(armed_now),
            sorted(armed_now),
        )
        return self._list_open_prs_remaining_fn()

    # ------------------------------------------------------------------
    # Viewer-login resolution
    # ------------------------------------------------------------------

    def _resolve_viewer_login(self) -> str:
        """Return the authenticated ``gh api user`` login. Fail CLOSED on error.

        Lazy + cached: only called when the author filter is active. Raises
        ``RuntimeError`` with operator guidance on any failure so a broken
        ``gh auth`` never silently widens scope to all PRs (#821 POLA).
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

    # ------------------------------------------------------------------
    # Bot-PR discovery
    # ------------------------------------------------------------------

    def _discover_bot_prs(self) -> dict[int, int]:
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
        try:
            owner, repo = get_repo_info(self.repo_root)
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

        viewer = "" if self.options.include_all_authors else self._resolve_viewer_login()
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

    # ------------------------------------------------------------------
    # Failing-PR discovery
    # ------------------------------------------------------------------

    def _discover_failing_prs(self) -> dict[int, int]:
        """Enumerate open non-draft PRs whose checks failed or merge is BLOCKED.

        Symmetrical to ``_discover_bot_prs``: the issue→PR direction (Closes #N)
        misses every PR with no Closes line and every PR linked to a closed
        issue (issue body §1, §2). One CLI call, PR-keyed, synthetic-issue
        invariant (pr_number == issue_number) so downstream ``_is_bot_pr_mode``
        short-circuits ``gh issue view`` identically to the bot path.

        Bounded by gh's --limit 1000 (its documented hard upper). A repo with
        more than 1000 failing open PRs is pathological — we log a WARNING
        so operators see the truncation rather than silently dropping work.

        Returns:
            Mapping pr_number -> pr_number for every failing open PR.
            Empty dict on any lookup failure — discovery must never abort
            the drive.

        """
        try:
            owner, repo = get_repo_info(self.repo_root)
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
            if _pr_is_failing_local(pr):
                failing[number] = number
        if failing:
            logger.info(
                "Discovered %s open failing PR(s): %s",
                len(failing),
                sorted(failing),
            )
        return failing

    # ------------------------------------------------------------------
    # Bot-PR mode detection
    # ------------------------------------------------------------------

    def _is_bot_pr_mode(self, issue_number: int, pr_number: int) -> bool:
        """Return True iff this work item is a synthetic-issue bot PR (#848).

        The bot-PR enumeration uses the PR number as a stand-in for an
        issue number because Dependabot PRs have no associated issue.
        Anywhere we would normally call ``gh issue view <issue_number>``
        we must instead short-circuit; this helper centralises the check
        so a single rule (issue == pr) keeps both ends honest.
        """
        return issue_number == pr_number

    # ------------------------------------------------------------------
    # Main PR discovery orchestration
    # ------------------------------------------------------------------

    def _discover_prs(self, issue_numbers: list[int]) -> dict[int, int]:  # noqa: C901
        """Pre-discover open PRs for all issues, deduped by PR.

        When a single PR closes multiple issues (a legitimate ``pr-policy``
        configuration — audit rollups, dependency bumps that cover several
        CVEs, etc.), every one of those issues resolves to the same PR. The
        downstream worker loop would then race N threads to check the same
        branch out into N different worktree paths, and ``git worktree add``
        rejects all but the first because a branch can only be checked out
        once. The losers were marked CI-failed even though the PR was being
        driven correctly by the first issue (#834).

        We dedupe at discovery time: keep one canonical issue per PR (the
        lowest-numbered, for deterministic ordering and stable logs), and
        defer the others.

        When ``options.include_bot_prs`` is True (default), the result is
        unioned with every open ``is_bot=true`` PR on the repo (#848). Bot
        PRs lack ``Closes #N`` links and would otherwise be invisible. Each
        bot PR is keyed by its own number as the synthetic issue.

        Args:
            issue_numbers: Issue numbers to check

        Returns:
            Mapping of canonical_issue_number -> pr_number, with at most one
            entry per PR.

        """
        raw_map: dict[int, int] = {}
        for issue_num in issue_numbers:
            pr_number = self._find_pr_for_issue(issue_num)
            if pr_number is not None:
                raw_map[issue_num] = pr_number
            else:
                logger.info("Issue #%s: no open PR found, skipping", issue_num)

        pr_to_issues: dict[int, list[int]] = {}
        for issue_num, pr_num in raw_map.items():
            pr_to_issues.setdefault(pr_num, []).append(issue_num)

        # Stash the full PR→[issues] map so the success path (#840) can write
        # an arming record for *every* sibling issue when a shared-PR group
        # auto-merge-arms.
        self._shared_pr_issues_setter({pr: sorted(issues) for pr, issues in pr_to_issues.items()})

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

        # Direct PR mode (#918). Operator-supplied PR numbers bypass
        # find_pr_for_issue entirely.
        for pr_num in self.options.prs:
            if pr_num in deduped.values():
                logger.info(
                    "Direct PR #%s already discovered via --issues; skipping duplicate",
                    pr_num,
                )
                continue
            if not self._validate_pr_open(pr_num):
                logger.warning("Direct PR #%s is not OPEN or does not exist; skipping", pr_num)
                continue
            deduped[pr_num] = pr_num
            self._shared_pr_issues_getter().setdefault(pr_num, [pr_num])

        if self.options.include_bot_prs and not self.options.issues:
            bot_prs = self._discover_bot_prs()
            for pr_num, _ in bot_prs.items():
                if pr_num in deduped.values():
                    continue
                deduped[pr_num] = pr_num
                self._shared_pr_issues_getter().setdefault(pr_num, [pr_num])

        if not self.options.issues:
            already_known: set[int] = set(deduped.values())
            failing_prs = self._discover_failing_prs()
            for pr_num in failing_prs:
                if pr_num in already_known:
                    continue
                deduped[pr_num] = pr_num
                already_known.add(pr_num)
                self._shared_pr_issues_getter().setdefault(pr_num, [pr_num])
        return deduped

    # ------------------------------------------------------------------
    # Single-issue / single-PR helpers
    # ------------------------------------------------------------------

    def _find_pr_for_issue(self, issue_number: int) -> int | None:
        """Find the open PR for a single issue.

        Delegates to :func:`_review_utils.find_pr_for_issue` (two-strategy
        branch-name + body search).

        Args:
            issue_number: GitHub issue number.

        Returns:
            PR number if found, None otherwise.

        """
        return find_pr_for_issue(issue_number)

    def _validate_pr_open(self, pr_number: int) -> bool:
        """Return True iff ``pr_number`` exists and is in OPEN state.

        Args:
            pr_number: GitHub PR number.

        Returns:
            True if the PR exists and is OPEN, False otherwise.

        """
        try:
            result = _gh_call(
                ["pr", "view", str(pr_number), "--json", "number,state"],
                check=False,
            )
            data = json.loads(result.stdout or "{}")
            return str(data.get("state", "")).upper() == "OPEN"
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            logger.debug("PR #%s validation failed: %s", pr_number, exc)
            return False

    # ------------------------------------------------------------------
    # Slots wired by CIDriver after construction (to avoid circular deps)
    # ------------------------------------------------------------------

    def _enable_auto_merge_fn(self, pr_number: int, *, is_bot_pr: bool = False) -> bool:
        """Raise until CIDriver wires this slot after construction."""
        raise NotImplementedError("_enable_auto_merge_fn not wired")

    def _list_open_prs_remaining_fn(self) -> list[dict[str, Any]]:
        """Raise until CIDriver wires this slot after construction."""
        raise NotImplementedError("_list_open_prs_remaining_fn not wired")
