"""Canonical comment-body markers used across the automation pipeline.

The planner, plan reviewer, and implementer locate automation-owned comments
on a GitHub issue through opaque canonical markers:

- :data:`PLAN_CANONICAL_MARKER` keys the editable current plan.
- :data:`PLAN_REVIEW_CANONICAL_MARKER` keys the editable current review.

The human-readable :data:`PLAN_COMMENT_MARKER` and
:data:`PLAN_REVIEW_PREFIX` headings remain display text on the following line.
Historical heading-only comments are inert audit data. The primary markers are
a shared ``HomericIntelligence`` wire protocol; legacy aliases are read only
so existing actor-owned comments can be upgraded in place.

Originally split across ``models.py`` and ``review_state.py``; consolidated
here per issue #801 (tracking #708).
"""

from __future__ import annotations

from typing import Any, Final, Protocol, runtime_checkable

PLAN_COMMENT_MARKER: Final[str] = "# Implementation Plan"
"""Human-readable heading in the planner's canonical plan comment."""

HOMERIC_INTELLIGENCE_COMMENT_PREFIX: Final[str] = "<!-- HomericIntelligence:"
"""Shared namespace for cross-tool issue-planning comment metadata."""

PLAN_CANONICAL_MARKER: Final[str] = f"{HOMERIC_INTELLIGENCE_COMMENT_PREFIX}plan-issue -->"
"""Shared opaque marker for the editable current implementation plan."""

LEGACY_PLAN_CANONICAL_MARKERS: Final[tuple[str, ...]] = (
    "<!-- hephaestus-plan:canonical -->",
    "<!-- athena:plan-issue -->",
)
"""Read-only plan aliases emitted before the shared marker migration."""

PLAN_CANONICAL_MARKERS: Final[tuple[str, ...]] = (
    PLAN_CANONICAL_MARKER,
    *LEGACY_PLAN_CANONICAL_MARKERS,
)
"""Current plan marker followed by accepted read-only migration aliases."""

PLAN_REVIEW_PREFIX: Final[str] = "## 🔍 Plan Review"
"""Heading the plan reviewer writes at the top of each review comment."""

PLAN_REVIEW_CANONICAL_MARKER: Final[str] = f"{HOMERIC_INTELLIGENCE_COMMENT_PREFIX}issue-review -->"
"""Shared opaque marker for the editable current plan review."""

LEGACY_PLAN_REVIEW_CANONICAL_MARKERS: Final[tuple[str, ...]] = (
    "<!-- hephaestus-plan-review:canonical -->",
    "<!-- athena:issue-review -->",
)
"""Read-only review aliases emitted before the shared marker migration."""

PLAN_REVIEW_CANONICAL_MARKERS: Final[tuple[str, ...]] = (
    PLAN_REVIEW_CANONICAL_MARKER,
    *LEGACY_PLAN_REVIEW_CANONICAL_MARKERS,
)
"""Current review marker followed by accepted read-only migration aliases."""

FINALIZED_PLAN_PREFIX: Final[str] = f"{HOMERIC_INTELLIGENCE_COMMENT_PREFIX}finalize-plan "
"""Shared prefix for the self-verifying finalized-plan issue-body marker."""

LEGACY_FINALIZED_PLAN_PREFIXES: Final[tuple[str, ...]] = ("<!-- athena:finalize-plan ",)
"""Read-only finalized-plan prefixes emitted before the shared migration."""

FINALIZED_PLAN_PREFIXES: Final[tuple[str, ...]] = (
    FINALIZED_PLAN_PREFIX,
    *LEGACY_FINALIZED_PLAN_PREFIXES,
)
"""Current finalized-plan prefix followed by accepted migration aliases."""


def comment_marker_aliases(marker: str) -> tuple[str, ...]:
    """Return all exact markers equivalent to one current planning artifact.

    New writes always use the first marker. Readers and actor-owned upserts
    accept a legacy alias only when it is the one unambiguous owned artifact.
    Multiple aliases or comments are an identity conflict that needs manual
    recovery; they are never selected by recency or deleted automatically.
    """
    if marker in PLAN_CANONICAL_MARKERS:
        return PLAN_CANONICAL_MARKERS
    if marker in PLAN_REVIEW_CANONICAL_MARKERS:
        return PLAN_REVIEW_CANONICAL_MARKERS
    return (marker,)


@runtime_checkable
class ReviewerProtocol(Protocol):
    """Structural contract satisfied by reviewer entry points.

    Verified: AuditReviewer.run (audit_reviewer.py:197) and
              PlanReviewer.run (plan_reviewer.py:99).
    """

    def run(self) -> Any:
        """Execute the reviewer and return its result."""


__all__ = [
    "FINALIZED_PLAN_PREFIX",
    "FINALIZED_PLAN_PREFIXES",
    "HOMERIC_INTELLIGENCE_COMMENT_PREFIX",
    "LEGACY_FINALIZED_PLAN_PREFIXES",
    "LEGACY_PLAN_CANONICAL_MARKERS",
    "LEGACY_PLAN_REVIEW_CANONICAL_MARKERS",
    "PLAN_CANONICAL_MARKER",
    "PLAN_CANONICAL_MARKERS",
    "PLAN_COMMENT_MARKER",
    "PLAN_REVIEW_CANONICAL_MARKER",
    "PLAN_REVIEW_CANONICAL_MARKERS",
    "PLAN_REVIEW_PREFIX",
    "ReviewerProtocol",
    "comment_marker_aliases",
]
