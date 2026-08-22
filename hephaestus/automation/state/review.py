"""Shared plan-review state helpers for the automation pipeline.

The GitHub issue label is the sole durable plan-review routing gate. Plan-review
comments carry an exact final state token for audit and diagnostics, but their
prose cannot grant approval or repair labels.
"""

from __future__ import annotations

import logging
from typing import Any

import hephaestus.automation.github_api as _api

from ..git_utils import get_repo_info, get_repo_root, issue_ref
from ..github_api.graphql import GraphQLMutationSpec, GraphQLQuerySpec
from ..protocol import PLAN_REVIEW_PREFIX as PLAN_REVIEW_PREFIX
from ..review_journal import is_plan_review_comment
from ..state_labels import STATE_PLAN_GO, is_exclusive_plan_state

logger = logging.getLogger(__name__)

# Human-readable heading used when rendering plan-review comments. Identity is
# decided by :func:`is_plan_review_comment`, which requires the opaque marker
# at byte zero; this heading is display text only.

_STATE_RESULTS = {
    "state:plan-go": "GO",
    "state:plan-no-go": "NOGO",
    "state:plan-blocked": "BLOCKED",
}

# Compatibility seams retained while the response contract moves callers from
# raw subprocess fixtures to ``run_graphql``.  Tests and downstream helpers
# can still replace these names without reintroducing a second executor.
_gh_call = _api._gh_call
gh_issue_json = _api.gh_issue_json


def _run_graphql[T](
    spec: GraphQLQuerySpec[T] | GraphQLMutationSpec[T],
    variables: dict[str, int | str],
) -> T:
    """Execute through the centralized validator and this module's test seam."""
    if isinstance(spec, GraphQLQuerySpec):
        return _api.run_graphql(spec, variables, call=_gh_call)
    return _api.run_graphql(spec, call=_gh_call)


# Maximum length for verdict context preview in logs (e.g., first verdict line or content).
_VERDICT_LOG_PREVIEW_CHARS = 200

# Legacy diagnostic threshold retained for compatibility and telemetry only.
# It must never authorize or block a pipeline transition; live GitHub labels
# are the sole durable plan-review state source.
MAX_UNPARSEABLE_VERDICT_PASSES: int = 3


def latest_verdict(review_body: str) -> str | None:
    """Return the last exact state token in a posted plan-review body.

    This diagnostic parser accepts only ``state:plan-*`` lines. It is never an
    authorization source; :func:`is_plan_review_go` reads issue labels only.

    Args:
        review_body: Full text of a canonical plan-review comment.

    Returns:
        ``"GO"``, ``"NOGO"``, or ``"BLOCKED"`` (last matching line), or
        ``None`` when no verdict line is present.

    """
    for line in reversed(review_body.splitlines()):
        if result := _STATE_RESULTS.get(line.strip().lower()):
            return result
    return None


def _extract_verdict_context(review_body: str) -> str:
    """Extract a human-readable context line from a review body.

    Returns the last plan-state token if present, else the first non-empty
    non-marker line. The result is truncated for safe logging.

    Args:
        review_body: Full text of a plan-review comment.

    Returns:
        A preview string (may be empty if body is empty or all-prefix).

    """
    lines = review_body.split("\n")

    # Look for the current machine-readable state token.
    for line in reversed(lines):
        if line.strip().lower().startswith("state:plan-"):
            preview = line.strip()
            if preview:
                return preview[:_VERDICT_LOG_PREVIEW_CHARS]

    # Fall back to first non-prefix content line
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith((PLAN_REVIEW_PREFIX, "<!-- hephaestus-")):
            return stripped[:_VERDICT_LOG_PREVIEW_CHARS]

    return ""


def count_unparseable_verdict_passes(comments: list[dict[str, Any]]) -> int:
    """Count how many plan-review comments lack a parseable verdict.

    Scans canonical plan-review comments in chronological order and counts the
    ones where :func:`latest_verdict` returns ``None``. This is the number of
    passes in which a reviewer posted a comment but :func:`latest_verdict`
    could not find a plan-state token. Heading-only historical text is inert.

    A non-zero count indicates malformed historical reviewer output. This is
    diagnostic information only and must never drive pipeline routing.

    Args:
        comments: Chronological list of comment dicts (each with at least a
            ``body`` key).  Typically the same list passed to
            :func:`is_plan_review_go`.

    Returns:
        Number of plan-review comments with an unparseable verdict (0 or more).

    """
    count = 0
    for comment in comments:
        body: str = comment.get("body", "")
        if is_plan_review_comment(body) and latest_verdict(body) is None:
            count += 1
    return count


def exceeds_unparseable_verdict_cap(
    comments: list[dict[str, Any]],
    cap: int = MAX_UNPARSEABLE_VERDICT_PASSES,
) -> bool:
    """Return True when an issue has exceeded the unparseable-verdict retry cap.

    This compatibility helper is diagnostic only. Its result must never drive
    pipeline routing; live GitHub labels are the sole durable plan-review state
    source.

    Args:
        comments: Chronological list of comment dicts.  Same list used by
            :func:`is_plan_review_go`.
        cap: Maximum number of unparseable-verdict passes to allow before
            returning ``True``.  Defaults to :data:`MAX_UNPARSEABLE_VERDICT_PASSES`.

    Returns:
        ``True`` if the number of plan-review comments with unparseable
        verdicts is greater than or equal to ``cap``; ``False`` otherwise.

    """
    return count_unparseable_verdict_passes(comments) >= cap


def _fetch_issue_comments_graphql(issue_number: int) -> list[dict[str, Any]]:
    """Fetch up to 100 most-recent comments on an issue via GraphQL.

    Mirrors :meth:`PlanReviewer._fetch_issue_comments` exactly so both
    callers see the same comment slice. GraphQL returns nodes
    newest-first (``UPDATED_AT DESC``); we reverse to chronological
    order so downstream "walk forward, last match wins" semantics work.

    Args:
        issue_number: GitHub issue number.

    Returns:
        List of comment dicts (each with at least a ``body`` key).

    Raises:
        GraphQLResponseError: If the response is unavailable, malformed, or
            fails the operation-specific contract.

    """
    owner, name = get_repo_info(get_repo_root())
    return _run_graphql(
        _api.issue_comments_query(owner, name, issue_number),
        {"owner": owner, "name": name, "number": issue_number},
    )


def fetch_all_issue_comments_graphql(
    issue_numbers: list[int],
) -> dict[int, list[dict[str, Any]]]:
    """Batch-fetch bounded comment context for legacy review-state consumers.

    This helper may return empty lists after a failed or capped GraphQL lookup.
    Plan discovery must use ``fetch_issue_comments_metadata`` and the tri-state
    discovery contract instead.

    Mirrors the aliased batching pattern used by
    :func:`hephaestus.automation.github_api._fetch_batch_states` for issue
    states.  Instead of ``N`` individual round-trips (one per issue), a single
    query aliases each issue as ``issue{idx}`` and retrieves up to 100
    comments per issue ordered by ``UPDATED_AT DESC``.  The results are
    reversed to chronological order so downstream "last match wins" semantics
    (e.g. :func:`latest_verdict`) work correctly.

    This function remains the shared implementation for bounded review-state
    context such as :func:`is_plan_review_go`; it is not an authoritative plan
    presence source.

    Raises the typed GraphQL response error on any failure; an empty result is
    valid only when GitHub returns every requested alias with a valid empty
    connection.

    Args:
        issue_numbers: List of GitHub issue numbers to fetch.

    Returns:
        Mapping of ``issue_number → list[comment_dict]`` in chronological
        order (oldest first).  Issues that could not be fetched map to ``[]``.

    """
    if not issue_numbers:
        return {}

    owner, name = get_repo_info(get_repo_root())
    variables: dict[str, int | str] = {"owner": owner, "name": name}
    for idx, issue_number in enumerate(issue_numbers):
        variables[f"n{idx}"] = int(issue_number)
    return _run_graphql(
        _api.batch_issue_comments_query(issue_numbers, owner, name),
        variables,
    )


def fetch_all_issue_labels_graphql(
    issue_numbers: list[int],
) -> dict[int, list[str]]:
    """Batch-fetch label names for multiple issues in one aliased GraphQL call.

    Mirrors :func:`fetch_all_issue_comments_graphql` but retrieves each issue's
    label names instead of comments. One aliased query replaces ``N`` per-issue
    ``gh issue view`` round-trips, so the planner can cheaply drop already-GO
    (``state:plan-go``) issues from its work set before the worker pool starts
    (avoids re-scanning every open issue every loop).

    Raises the typed GraphQL response error on any failure; missing labels are
    represented only by a validated empty connection.

    Args:
        issue_numbers: List of GitHub issue numbers to fetch.

    Returns:
        Mapping of ``issue_number → list[label_name]``. Issues that could not be
        fetched map to ``[]``.

    """
    if not issue_numbers:
        return {}

    owner, name = get_repo_info(get_repo_root())
    variables: dict[str, int | str] = {"owner": owner, "name": name}
    for idx, issue_number in enumerate(issue_numbers):
        variables[f"n{idx}"] = int(issue_number)
    return _run_graphql(
        _api.batch_issue_labels_query(issue_numbers, owner, name),
        variables,
    )


def fetch_all_issue_titles_graphql(
    issue_numbers: list[int],
) -> dict[int, str]:
    """Batch-fetch issue titles in one aliased GraphQL call.

    Sibling of :func:`fetch_all_issue_labels_graphql`. The planner uses it
    alongside the labels fetch so :func:`~hephaestus.automation.state_labels.
    is_epic` can apply its title-based signal (catching epics/roadmaps that
    carry no label) without a per-issue ``gh issue view`` (#1669).

    Raises the typed GraphQL response error on any failure; an empty title is
    not a substitute for an unreadable response.

    Args:
        issue_numbers: List of GitHub issue numbers to fetch.

    Returns:
        Mapping of ``issue_number → title``. Issues that could not be fetched
        map to ``""``.

    """
    if not issue_numbers:
        return {}

    owner, name = get_repo_info(get_repo_root())
    variables: dict[str, int | str] = {"owner": owner, "name": name}
    for idx, issue_number in enumerate(issue_numbers):
        variables[f"n{idx}"] = int(issue_number)
    return _run_graphql(
        _api.batch_issue_titles_query(issue_numbers, owner, name),
        variables,
    )


def is_plan_review_go(
    issue_number: int,
    comments: list[dict[str, Any]] | None = None,
    issue_labels: list[str] | None = None,
) -> bool:
    """Return True iff the issue carries the authoritative ``state:plan-go`` label.

    ``comments`` remains in the compatibility signature but is deliberately
    ignored. Historical review prose and state-looking comment text cannot
    grant approval or backfill labels.

    Args:
        issue_number: GitHub issue number. Used for logging and lazy label
            fetch when ``issue_labels`` is ``None``.
        comments: Ignored compatibility argument. Comment text is not an
            authorization source.
        issue_labels: Pre-fetched list of label names currently on the issue,
            or ``None`` to fetch lazily via :func:`gh_issue_json`. Callers
            that already have the labels in hand (e.g. the implementer's
            per-issue load) should pass them to avoid an extra round-trip.

    Returns:
        ``True`` iff ``state:plan-go`` is the issue's only plan-state label;
        otherwise ``False``, including contradictory labels and fetch failures.

    """
    del comments
    if issue_labels is None:
        try:
            issue_data = gh_issue_json(issue_number)
            issue_labels = [
                label.get("name", "") for label in issue_data.get("labels", []) if label.get("name")
            ]
        except Exception as e:
            logger.warning(
                "Issue %s: could not fetch labels for plan-go gate (%s)",
                issue_ref(issue_number),
                e,
            )
            return False
    is_go = is_exclusive_plan_state(issue_labels, STATE_PLAN_GO)
    logger.debug(
        "Issue %s: authoritative plan label is %s",
        issue_ref(issue_number),
        "GO" if is_go else "not GO",
    )
    return is_go
