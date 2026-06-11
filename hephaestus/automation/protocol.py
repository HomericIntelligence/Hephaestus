"""Canonical comment-body markers used across the automation pipeline.

The planner, plan reviewer, and implementer all locate their comments on a
GitHub issue by ``body.startswith(...)`` against one of two markers:

- :data:`PLAN_COMMENT_MARKER` — the heading the planner writes at the top of
  the single plan comment. ``gh_issue_upsert_comment`` keys off this marker
  to find-and-replace the existing plan rather than appending a new one.
- :data:`PLAN_REVIEW_PREFIX` — the heading the plan reviewer writes at the top
  of each review comment; the verdict gate (:mod:`review_state`) iterates
  comments matching this prefix in chronological order.

Both strings are part of the pipeline's *wire protocol* — changing either
breaks the upsert key and causes duplicate comments. They live here together
so they cannot drift apart.

Originally split across ``models.py`` and ``review_state.py``; consolidated
here per issue #801 (tracking #708).
"""

from __future__ import annotations

from typing import Final

PLAN_COMMENT_MARKER: Final[str] = "# Implementation Plan"
"""Heading the planner writes at the top of the single plan comment."""

PLAN_REVIEW_PREFIX: Final[str] = "## 🔍 Plan Review"
"""Heading the plan reviewer writes at the top of each review comment."""

WONT_FIX_MARKER: Final[str] = "WONT-FIX: intentional design"
"""Prefix the validator (or a human) replies with to dismiss a review finding as
intentional-by-design (#1163). A resolved thread whose comments carry this prefix
is permanently skipped: never re-validated, re-opened, or re-raised — so an
intentional-design finding (e.g. an abstract method's ``NotImplementedError``)
cannot stack duplicate threads across runs. Part of the wire protocol — both the
validator's resolve-reply and the reviewer's dedup match on this exact string."""

__all__ = ["PLAN_COMMENT_MARKER", "PLAN_REVIEW_PREFIX", "WONT_FIX_MARKER"]
