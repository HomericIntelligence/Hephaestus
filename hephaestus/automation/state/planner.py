"""State manager for the issue-planning phase.

Owns the cheap, idempotent state queries the planner runs against GitHub:

- ``filter()`` — drop closed issues from the working set (one batched GraphQL
  call per 100 issues via :func:`prefetch_issue_states`).
- ``prefetch_comments()`` — fetch and normalize all issue comments into an
  internal cache (#616).
- ``discover_plan()`` — return FOUND, ABSENT, or READ_ERROR using the cache
  when available and a complete REST lookup otherwise.

Extracted from ``planner.py`` (#598) so the coordinator class stays focused
on the worker-pool driver. No behavior change.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..git_utils import issue_ref
from ..github_api import (
    fetch_issue_comments_metadata,
    gh_current_login,
    prefetch_issue_states,
    skip_epics,
)
from ..review_journal import (
    CommentJournalReadError,
    IssueComment,
    PlanDiscoveryResult,
    PlanDiscoveryStatus,
    discover_plan_from_comments,
    normalize_issue_comments,
)
from ..state_labels import STATE_PLAN_GO, is_epic, is_exclusive_plan_state
from .review import (
    fetch_all_issue_labels_graphql,
    fetch_all_issue_titles_graphql,
)

if TYPE_CHECKING:
    from ..models import PlannerOptions

logger = logging.getLogger(__name__)


def _comments_contain_plan(comments: Sequence[IssueComment]) -> bool:
    """Return whether normalized comments contain an actor-owned plan."""
    return discover_plan_from_comments(comments).status is PlanDiscoveryStatus.FOUND


class PlannerStateManager:
    """Cheap GitHub state queries used by the planner.

    Attributes:
        options: The planner options driving filter behavior.
        _comments_cache: Per-issue comment list populated by
            :meth:`prefetch_comments`.  ``None`` means the cache has not been
            populated yet (fall back to individual fetches).

    """

    def __init__(self, options: PlannerOptions) -> None:
        """Bind to the planner options driving filter behavior.

        Args:
            options: The shared :class:`PlannerOptions` instance.

        """
        self.options = options
        self._comments_cache: dict[int, list[IssueComment]] | None = None
        self._comment_read_errors: dict[int, str] = {}
        self._labels_cache: dict[int, list[str]] | None = None

    def filter(self) -> list[int]:
        """Filter issues based on options.

        Cheap, batched checks here, each one GraphQL call per 100 issues:

        1. Skip **closed** issues (:func:`prefetch_issue_states`).
        2. Skip **epic/roadmap** tracking issues (:func:`is_epic`) and tag them
           ``state:skip`` (#1669) — they are checklists, not code tasks.
        3. Skip issues already in ``state:plan-go`` (:func:`fetch_all_issue_labels_graphql`).

        Dropping ``state:plan-go`` issues up front means the loop stops
        re-evaluating every open issue every pass — previously each surviving
        issue cost one ``gh issue view`` via ``is_plan_review_go`` inside the
        worker, even ones already converged. The batched label fetch replaces
        those N round-trips with one. Issues whose labels couldn't be fetched
        fall through and are re-checked the slow way inside the worker (no
        behavior loss, just no speed-up for that issue).

        Returns:
            List of issue numbers to plan.

        """
        cached_states = {}
        if self.options.skip_closed:
            cached_states = prefetch_issue_states(self.options.issues)

        # Batch-fetch labels so we can cheaply drop already-GO issues here and
        # also serve them to is_plan_review_go inside the worker (no extra call).
        self._labels_cache = fetch_all_issue_labels_graphql(self.options.issues)
        # Titles back the epic title-signal (catches epics carrying no label).
        titles = fetch_all_issue_titles_graphql(self.options.issues)

        skip_epics_meta: dict[int, list[str]] = {}
        issues_to_plan = []
        for issue_num in self.options.issues:
            if self.options.skip_closed:
                state = cached_states.get(issue_num)
                if state and state.value == "CLOSED":
                    logger.info("Issue #%s is closed, skipping", issue_num)
                    continue

            labels = self._labels_cache.get(issue_num) or []

            # Epic/roadmap tracking issues are never planned (#1669). Collect
            # them for one idempotent state:skip tagging pass after the loop.
            if is_epic(labels, titles.get(issue_num, "")):
                logger.info(
                    "Issue #%s is an epic/roadmap tracking issue; excluding from planning",
                    issue_num,
                )
                skip_epics_meta[issue_num] = labels
                continue

            # Already-planned fast path: a state:plan-go label is the single
            # source of truth (#704). Drop it now without a per-issue round-trip.
            # ``force`` re-plans everything, so don't pre-filter then.
            if (
                not self.options.force
                and self._labels_cache.get(issue_num) is not None
                and is_exclusive_plan_state(labels, STATE_PLAN_GO)
            ):
                logger.info("Issue #%s already has a plan (state:plan-go), skipping", issue_num)
                continue

            issues_to_plan.append(issue_num)

        # Best-effort skip-tagging of excluded epics; never block planning on it.
        if skip_epics_meta:
            try:
                skip_epics(skip_epics_meta)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Could not tag excluded epics state:skip: %s", exc)

        # Make a mis-scoped explicit run obvious. An explicit ``--issues`` set
        # that fully filters out (all closed and/or already-planned) otherwise
        # no-ops with only INFO-level per-issue "... skipping" lines. Stay quiet
        # for auto-discovery (issues_explicit=False): a converged repo
        # legitimately yields an empty set every pass and must not spam.
        if self.options.issues_explicit and self.options.issues and not issues_to_plan:
            logger.warning(
                "All %d explicitly-requested issue(s) were filtered out "
                "(closed or already planned); nothing to plan. Requested: %s",
                len(self.options.issues),
                self.options.issues,
            )

        return issues_to_plan

    def get_cached_labels(self, issue_number: int) -> list[str] | None:
        """Return cached label names for an issue, or None if unpopulated.

        Populated by :meth:`filter`. Callers pass the result into
        :func:`~hephaestus.automation.review_state.is_plan_review_go` as
        ``issue_labels=`` to avoid a per-issue ``gh issue view``.
        """
        if self._labels_cache is None:
            return None
        return self._labels_cache.get(issue_number, [])

    def _read_comments(self, issue_number: int) -> list[IssueComment]:
        """Read and normalize the complete issue comment journal."""
        try:
            return normalize_issue_comments(
                fetch_issue_comments_metadata(issue_number),
                viewer_login=gh_current_login() or "",
            )
        except CommentJournalReadError:
            raise
        except Exception as exc:
            raise CommentJournalReadError(
                f"failed to read issue #{issue_number} comments: {exc}"
            ) from exc

    def prefetch_comments(self, issue_numbers: list[int]) -> None:
        """Read complete comment journals and retain per-issue failures.

        Args:
            issue_numbers: Issue numbers to prefetch. Typically the list
                returned by :meth:`filter`.

        """
        self._comments_cache = {}
        self._comment_read_errors = {}
        for issue_number in issue_numbers:
            try:
                self._comments_cache[issue_number] = self._read_comments(issue_number)
            except CommentJournalReadError as exc:
                self._comment_read_errors[issue_number] = str(exc)
                logger.warning(
                    "Failed to prefetch comments for %s: %s",
                    issue_ref(issue_number),
                    exc,
                )

    def get_cached_comments(self, issue_number: int) -> list[IssueComment] | None:
        """Return cached comments for an issue, or None if cache is unpopulated.

        Args:
            issue_number: GitHub issue number.

        Returns:
            Cached comment list, or ``None`` when :meth:`prefetch_comments`
            has not been called yet.

        """
        if self._comments_cache is None:
            return None
        return self._comments_cache.get(issue_number)

    def discover_plan(self, issue_number: int) -> PlanDiscoveryResult:
        """Return FOUND, ABSENT, or READ_ERROR for an issue plan lookup."""
        if issue_number in self._comment_read_errors:
            return PlanDiscoveryResult.read_error(self._comment_read_errors[issue_number])

        comments = self.get_cached_comments(issue_number)
        if comments is None:
            try:
                comments = self._read_comments(issue_number)
            except CommentJournalReadError as exc:
                return PlanDiscoveryResult.read_error(exc)
        return discover_plan_from_comments(comments)
