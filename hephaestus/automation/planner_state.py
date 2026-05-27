"""State manager for :class:`hephaestus.automation.planner.Planner`.

Owns the cheap, idempotent state queries the planner runs against GitHub:

- ``filter()`` — drop closed issues from the working set (one batched GraphQL
  call per 100 issues via :func:`prefetch_issue_states`).
- ``has_existing_plan()`` — return ``True`` when an issue already carries one
  of the canonical plan-comment markers.

Extracted from ``planner.py`` (#598) so the coordinator class stays focused
on the worker-pool driver. No behavior change.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from .git_utils import issue_ref
from .github_api import _gh_call, prefetch_issue_states
from .models import PLAN_COMMENT_MARKERS

if TYPE_CHECKING:
    from .models import PlannerOptions

logger = logging.getLogger(__name__)


class PlannerStateManager:
    """Cheap GitHub state queries used by the planner.

    Attributes:
        options: The planner options driving filter behavior.

    """

    def __init__(self, options: PlannerOptions) -> None:
        """Bind to the planner options driving filter behavior.

        Args:
            options: The shared :class:`PlannerOptions` instance.

        """
        self.options = options

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

    def has_existing_plan(self, issue_number: int) -> bool:
        """Check if an issue already has a plan in comments.

        Args:
            issue_number: Issue number to check

        Returns:
            True if plan exists

        """
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
