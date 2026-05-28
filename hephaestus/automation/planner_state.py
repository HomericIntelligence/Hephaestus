"""State manager for :class:`hephaestus.automation.planner.Planner`.

Owns the cheap, idempotent state queries the planner runs against GitHub:

- ``filter()`` — drop closed issues from the working set (one batched GraphQL
  call per 100 issues via :func:`prefetch_issue_states`).
- ``prefetch_comments()`` — batch-fetch all issue comments in one aliased
  GraphQL call and store them in an internal cache (#616).
- ``has_existing_plan()`` — return ``True`` when an issue already carries one
  of the canonical plan-comment markers (uses the cache when available).

Extracted from ``planner.py`` (#598) so the coordinator class stays focused
on the worker-pool driver. No behavior change.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from .git_utils import issue_ref
from .github_api import _gh_call, prefetch_issue_states
from .models import PLAN_COMMENT_MARKERS
from .review_state import fetch_all_issue_comments_graphql

if TYPE_CHECKING:
    from .models import PlannerOptions

logger = logging.getLogger(__name__)


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

    def filter(self) -> list[int]:
        """Filter issues based on options.

        Only does the cheap, batched check here: skip closed issues using one
        GraphQL call per 100 via :func:`prefetch_issue_states`. The
        already-planned check happens per-issue inside
        :meth:`Planner._plan_issue` so it runs in parallel with the worker
        pool instead of blocking on N sequential ``gh issue view --comments``
        round-trips before any worker starts (#548).

        Returns:
            List of issue numbers to plan

        """
        cached_states = {}
        if self.options.skip_closed:
            cached_states = prefetch_issue_states(self.options.issues)

        issues_to_plan = []
        for issue_num in self.options.issues:
            if self.options.skip_closed:
                state = cached_states.get(issue_num)
                if state and state.value == "CLOSED":
                    logger.info("Issue #%s is closed, skipping", issue_num)
                    continue

            issues_to_plan.append(issue_num)

        return issues_to_plan

    def prefetch_comments(self, issue_numbers: list[int]) -> None:
        """Batch-fetch comments for all issues in one aliased GraphQL call.

        Stores results in the internal cache so subsequent calls to
        :meth:`has_existing_plan` and callers of
        :func:`~hephaestus.automation.review_state.is_plan_review_approved`
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
            for comment in cached:
                body = comment.get("body", "")
                if any(marker in body for marker in PLAN_COMMENT_MARKERS):
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

            for comment in comments:
                body = comment.get("body", "")
                if any(marker in body for marker in PLAN_COMMENT_MARKERS):
                    logger.debug("Found existing plan for %s", issue_ref(issue_number))
                    return True

            return False

        except Exception as e:
            logger.warning(
                "Failed to check for existing plan on %s: %s", issue_ref(issue_number), e
            )
            return False
