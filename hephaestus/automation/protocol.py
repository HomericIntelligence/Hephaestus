"""Canonical comment-body markers used across the automation pipeline.

The planner, plan reviewer, and implementer locate automation-owned comments
on a GitHub issue through opaque canonical markers:

- :data:`PLAN_CANONICAL_MARKER` keys the editable current plan.
- :data:`PLAN_REVIEW_CANONICAL_MARKER` keys the editable current review.

The human-readable :data:`PLAN_COMMENT_MARKER` and
:data:`PLAN_REVIEW_PREFIX` headings remain display text on the following line.
Historical heading-only comments are inert audit data. All four strings are
wire protocol: changing them without a migration breaks journal
reconstruction.

Originally split across ``models.py`` and ``review_state.py``; consolidated
here per issue #801 (tracking #708).
"""

from __future__ import annotations

from typing import Any, Final, Protocol, runtime_checkable

PLAN_COMMENT_MARKER: Final[str] = "# Implementation Plan"
"""Human-readable heading in the planner's canonical plan comment."""

PLAN_CANONICAL_MARKER: Final[str] = "<!-- hephaestus-plan:canonical -->"
"""Opaque ownership/deduplication marker for the editable current plan."""

PLAN_REVIEW_PREFIX: Final[str] = "## 🔍 Plan Review"
"""Heading the plan reviewer writes at the top of each review comment."""

PLAN_REVIEW_CANONICAL_MARKER: Final[str] = "<!-- hephaestus-plan-review:canonical -->"
"""Opaque ownership/deduplication marker for the editable current review."""


@runtime_checkable
class ReviewerProtocol(Protocol):
    """Structural contract satisfied by reviewer entry points.

    Verified: AuditReviewer.run (audit_reviewer.py:197) and
              PlanReviewer.run (plan_reviewer.py:99).
    """

    def run(self) -> Any:
        """Execute the reviewer and return its result."""


__all__ = [
    "PLAN_CANONICAL_MARKER",
    "PLAN_COMMENT_MARKER",
    "PLAN_REVIEW_CANONICAL_MARKER",
    "PLAN_REVIEW_PREFIX",
    "ReviewerProtocol",
]
