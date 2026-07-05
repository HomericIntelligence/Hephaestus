"""Admission control for implementation queue: file-overlap serialization and dependency ordering.

Part of epic #1809. Provides:
- File-overlap detection via greedy first-fit partitioning
- Dependency-based execution ordering via topological sort
- Per-repo in-flight work-item cap helpers

The file-overlap guard (#1623) prevents concurrent plan execution on the same
source files, which would lead to merge conflicts when the first PR lands.
"""

from __future__ import annotations

import logging
import re

from hephaestus.automation.github_api import (
    _fetch_issue_comment_ids,
    is_issue_closed,
    prefetch_issue_states,
)
from hephaestus.automation.protocol import PLAN_COMMENT_MARKER

LOG = logging.getLogger(__name__)

# Backticked repo-relative path inside a plan's Files sections, e.g.
# `hephaestus/automation/address_review.py`. Requires a slash so bare tokens
# like `pyproject.toml` or symbol refs like `os.replace` are not treated as
# in-tree paths (over-match → needless deferral; the slash requirement keeps
# the key tight to actual source paths).
# NOTE: Bare top-level file paths without a directory prefix (e.g., `errors.py`)
# are intentionally NOT captured — overlap goes undetected and both plans dispatch
# concurrently, falling back to pre-#1623 behavior (acceptable tradeoff for regex tightness).
_PLAN_FILE_RE = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*/[A-Za-z0-9_./-]+\.[A-Za-z0-9_]+)`")
_PLAN_FILE_SECTION_RE = re.compile(r"^#{2,}\s+Files to (Modify|Create)\b", re.IGNORECASE)


def _parse_planned_files(plan_body: str) -> set[str]:
    """Return the repo-relative paths a plan intends to touch.

    Scans the ``## Files to Modify`` and ``## Files to Create`` sections of an
    ``# Implementation Plan`` comment (either or both may be present) and
    collects every backticked in-tree path until the next top-level ``## ``
    heading. Empty set when neither section exists.

    Args:
            plan_body: The full body of the plan comment.

    Returns:
            The set of backticked repo-relative paths found in the Files sections.

    """
    files: set[str] = set()
    in_section = False
    for line in plan_body.splitlines():
        if _PLAN_FILE_SECTION_RE.match(line):
            in_section = True
            continue
        # A new top-level ``## `` heading (not a ``### `` sub-header inside the
        # section) ends the scan region.
        if line.startswith("## "):
            in_section = False
        if in_section:
            files.update(_PLAN_FILE_RE.findall(line))
    return files


def _fetch_planned_files(issue: int) -> set[str] | None:
    """Return the file set an issue's plan claims, or None if unknown.

    None (no plan comment / empty fetch) → caller fails OPEN and dispatches the
    issue this round. :func:`_fetch_issue_comment_ids` already swallows all
    errors and returns ``[]`` on failure, so NO try/except is needed here — the
    "no plan" signal is simply an empty/no-match list.

    Args:
            issue: GitHub issue number.

    Returns:
            The parsed plan file set, or None when no plan comment is present.

    """
    for comment in _fetch_issue_comment_ids(issue):
        body = str(comment.get("body", ""))
        if body.startswith(PLAN_COMMENT_MARKER):
            return _parse_planned_files(body)
    return None


def _select_non_overlapping(issues: list[int]) -> tuple[list[int], list[int]]:
    """Partition *issues* into (dispatch_now, defer_next_round).

    Greedy first-fit in the given order: an issue whose parsed plan file set
    intersects the union of already-claimed files is deferred. Unknown file set
    (no plan / parse failure) claims NO files and is always dispatched
    (fail-open). The first issue always dispatches, so a whole batch can never
    be deferred (liveness). Performs one serial GraphQL comment fetch per issue;
    only invoked in multi-worker rounds (guarded at the call site), so the cost
    is bounded by the issue count already being processed that round.

    Args:
            issues: The issue numbers to partition, in dispatch-priority order.

    Returns:
            A ``(dispatch, defer)`` tuple of issue-number lists (order preserved).

    """
    claimed: set[str] = set()
    dispatch: list[int] = []
    defer: list[int] = []
    for issue in issues:
        planned = _fetch_planned_files(issue)
        if planned and (planned & claimed):
            LOG.info(
                "issue #%s deferred: plan files %s overlap in-flight peers",
                issue,
                sorted(planned & claimed),
            )
            defer.append(issue)
            continue
        if planned:
            claimed |= planned
        dispatch.append(issue)
    return dispatch, defer


def _filter_open_issues(repo: str, issue_numbers: list[int]) -> list[int]:
    """Drop CLOSED issues from an explicit ``--issues`` list (#1576).

    An operator-pinned ``cfg.issues`` list bypasses the ``--state open`` filter
    that auto-discovery applies, so a closed issue would otherwise be driven
    every loop and wrongly tagged ``state:skip`` by drive-green. States are
    fetched once via :func:`prefetch_issue_states` and checked with
    :func:`is_issue_closed`. On any lookup failure an issue is KEPT (fail-open:
    never silently drop work over a transient API blip).

    Args:
            repo: Repository name (for logging).
            issue_numbers: The explicit issue list.

    Returns:
            The subset that is not closed (order preserved).

    """
    try:
        cached_states = prefetch_issue_states(issue_numbers)
    except Exception as exc:  # transient API failure → keep all, don't drop work
        LOG.warning("[%s] could not prefetch issue states for closed-filter: %s", repo, exc)
        return issue_numbers
    kept: list[int] = []
    for num in issue_numbers:
        if is_issue_closed(num, cached_states):
            LOG.info("[%s] issue #%s is closed — excluding from phase loop", repo, num)
            continue
        kept.append(num)
    return kept


__all__ = [
    "_filter_open_issues",
    "_parse_planned_files",
    "_select_non_overlapping",
]
