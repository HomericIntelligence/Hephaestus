"""State manager for :class:`hephaestus.automation.planner.Planner`.

Owns the cheap, idempotent state queries the planner runs against GitHub:

- ``filter()`` — drop closed issues from the working set (one batched GraphQL
  call per 100 issues via :func:`prefetch_issue_states`).
- ``prefetch_comments()`` — batch-fetch all issue comments in one aliased
  GraphQL call and store them in an internal cache (#616).
- ``has_existing_plan()`` — return ``True`` when an issue already carries the
  canonical plan-comment marker (uses the cache when available).

Extracted from ``planner.py`` (#598) so the coordinator class stays focused
on the worker-pool driver. No behavior change.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from .git_utils import issue_ref
from .github_api import _gh_call, prefetch_issue_states
from .models import PLAN_COMMENT_MARKER
from .review_state import (
    PLAN_REVIEW_PREFIX,
    fetch_all_issue_comments_graphql,
    fetch_all_issue_labels_graphql,
)
from .state_labels import STATE_PLAN_GO

if TYPE_CHECKING:
    from .models import PlannerOptions

logger = logging.getLogger(__name__)


def _comments_contain_plan(comments: list[dict[str, Any]]) -> bool:
    """Return True if any comment is a plan comment (not a review).

    Matches plan markers only at the START of a comment body, never as a free
    substring: a ``## 🔍 Plan Review`` comment quotes the plan (so it contains
    plan headings as substrings) and must NOT count as "has a plan" — that
    substring confusion caused the reviewer to review its own prior review
    (#455/#468/#484). Mirrors ``plan_reviewer._get_latest_plan``.
    """
    for comment in comments:
        stripped = comment.get("body", "").lstrip()
        if stripped.startswith(PLAN_REVIEW_PREFIX):
            continue
        if stripped.startswith(PLAN_COMMENT_MARKER):
            return True
    return False


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
        self._comments_cache: dict[int, list[dict[str, Any]]] | None = None
        self._labels_cache: dict[int, list[str]] | None = None

    def filter(self) -> list[int]:
        """Filter issues based on options.

        Two cheap, batched checks here, each one GraphQL call per 100 issues:

        1. Skip **closed** issues (:func:`prefetch_issue_states`).
        2. Skip issues already in ``state:plan-go`` (:func:`fetch_all_issue_labels_graphql`).

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

        issues_to_plan = []
        for issue_num in self.options.issues:
            if self.options.skip_closed:
                state = cached_states.get(issue_num)
                if state and state.value == "CLOSED":
                    logger.info("Issue #%s is closed, skipping", issue_num)
                    continue

            # Already-planned fast path: a state:plan-go label is the single
            # source of truth (#704). Drop it now without a per-issue round-trip.
            # ``force`` re-plans everything, so don't pre-filter then.
            if not self.options.force:
                labels = self._labels_cache.get(issue_num)
                if labels is not None and STATE_PLAN_GO in labels:
                    logger.info("Issue #%s already has a plan (state:plan-go), skipping", issue_num)
                    continue

            issues_to_plan.append(issue_num)

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

    def prefetch_comments(self, issue_numbers: list[int]) -> None:
        """Batch-fetch comments for all issues in one aliased GraphQL call.

        Stores results in the internal cache so subsequent calls to
        :meth:`has_existing_plan` and callers of
        :func:`~hephaestus.automation.review_state.is_plan_review_go`
        (which accept a pre-fetched ``comments`` list) can avoid per-issue
        round-trips.

        Calling this method before the worker pool starts converts N
        sequential ``gh issue view --comments`` calls into a single batched
        GraphQL request, cutting round-trips from O(N) to O(1) (#616).

        Args:
            issue_numbers: Issue numbers to pre-fetch.  Typically the list
                returned by :meth:`filter`.

        """
        if not issue_numbers:
            self._comments_cache = {}
            return
        logger.debug(
            "Batch-fetching comments for %d issue(s) via aliased GraphQL (#616)",
            len(issue_numbers),
        )
        self._comments_cache = fetch_all_issue_comments_graphql(issue_numbers)
        logger.debug(
            "Prefetched comments for %d issue(s)",
            len(self._comments_cache),
        )

    def get_cached_comments(self, issue_number: int) -> list[dict[str, Any]] | None:
        """Return cached comments for an issue, or None if cache is unpopulated.

        Args:
            issue_number: GitHub issue number.

        Returns:
            Cached comment list, or ``None`` when :meth:`prefetch_comments`
            has not been called yet.

        """
        if self._comments_cache is None:
            return None
        return self._comments_cache.get(issue_number, [])

    def has_existing_plan(self, issue_number: int) -> bool:
        """Check if an issue already has a plan in comments.

        Uses the batched comment cache when :meth:`prefetch_comments` has
        already been called (#616), falling back to an individual
        ``gh issue view --comments`` call otherwise.

        Args:
            issue_number: Issue number to check

        Returns:
            True if plan exists

        """
        # Fast path: use cached comments when available (#616).
        cached = self.get_cached_comments(issue_number)
        if cached is not None:
            if _comments_contain_plan(cached):
                logger.debug("Found existing plan for %s (cached)", issue_ref(issue_number))
                return True
            return False

        # Slow path: individual fetch (pre-#616 behaviour, kept as fallback).
        try:
            result = _gh_call(
                [
                    "issue",
                    "view",
                    str(issue_number),
                    "--comments",
                    "--json",
                    "comments",
                ],
            )

            data = json.loads(result.stdout)
            comments = data.get("comments", [])

            if _comments_contain_plan(comments):
                logger.debug("Found existing plan for %s", issue_ref(issue_number))
                return True

            return False

        except Exception as e:
            logger.warning(
                "Failed to check for existing plan on %s: %s", issue_ref(issue_number), e
            )
            return False
