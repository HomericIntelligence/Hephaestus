"""Canonical comment-body markers used across the automation pipeline.

The planner, plan reviewer, and implementer locate automation-owned comments
on a GitHub issue through opaque canonical markers:

- :data:`PLAN_CANONICAL_MARKER` keys the editable current plan.
- :data:`PLAN_REVIEW_CANONICAL_MARKER` keys the editable current review.

The human-readable :data:`PLAN_COMMENT_MARKER` and
:data:`PLAN_REVIEW_PREFIX` headings remain part of the display and migration
format. All four strings are wire protocol: changing them without a migration
breaks journal reconstruction.

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

WONT_FIX_MARKER: Final[str] = "WONT-FIX: intentional design"
"""Prefix the validator (or a human) replies with to dismiss a review finding as
intentional-by-design (#1163). A resolved thread whose comments carry this prefix
is permanently skipped: never re-validated, re-opened, or re-raised — so an
intentional-design finding (e.g. an abstract method's ``NotImplementedError``)
cannot stack duplicate threads across runs. Part of the wire protocol — both the
validator's resolve-reply and the reviewer's dedup match on this exact string."""


@runtime_checkable
class ReviewerProtocol(Protocol):
    """Structural contract satisfied by all four reviewer classes.

    Verified: PRReviewer.run (pr_reviewer.py:396),
              AddressReviewer.run (address_review.py:350),
              AuditReviewer.run (audit_reviewer.py:197),
              PlanReviewer.run (plan_reviewer.py:99).
    """

    def run(self) -> Any:
        """Execute the reviewer and return its result."""


__all__ = [
    "PLAN_CANONICAL_MARKER",
    "PLAN_COMMENT_MARKER",
    "PLAN_REVIEW_CANONICAL_MARKER",
    "PLAN_REVIEW_PREFIX",
    "WONT_FIX_MARKER",
    "ReviewerProtocol",
]
