"""Shared plan-review state helpers for the automation pipeline.

The GitHub issue label is the sole durable authorization gate. Plan-review
comments carry an exact final state token for audit and diagnostics, but their
prose cannot grant approval or repair labels.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..git_utils import get_repo_info, get_repo_root, issue_ref
from ..github_api import _gh_call, gh_issue_json
from ..protocol import PLAN_REVIEW_PREFIX as PLAN_REVIEW_PREFIX
from ..review_journal import is_plan_review_comment
from ..state_labels import is_plan_go as labels_are_plan_go

logger = logging.getLogger(__name__)

# Comment-body prefix used when posting plan-review comments. We identify
# "plan review comments" by this prefix on ``body.startswith(...)``. The
# canonical definition (alongside PLAN_COMMENT_MARKER) lives in
# :mod:`hephaestus.automation.protocol`; re-exported here for backward
# compatibility with the historical import path.

_STATE_RESULTS = {
    "state:plan-go": "GO",
    "state:plan-no-go": "NOGO",
    "state:plan-blocked": "BLOCKED",
}

# Maximum length for verdict context preview in logs (e.g., first verdict line or content).
_VERDICT_LOG_PREVIEW_CHARS = 200

# Maximum number of passes an issue may go through where a plan-review comment
# exists but its verdict cannot be parsed.  After this many unparseable-verdict
# passes the caller should surface the issue for human attention rather than
# requesting yet another review cycle.  See #615.
MAX_UNPARSEABLE_VERDICT_PASSES: int = 3


def latest_verdict(review_body: str) -> str | None:
    """Return the last exact state token in a posted plan-review body.

    This diagnostic parser accepts only ``state:plan-*`` lines. It is never an
    authorization source; :func:`is_plan_review_go` reads issue labels only.

    Args:
        review_body: Full text of a plan-review comment (starting with
            :data:`PLAN_REVIEW_PREFIX`).

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

    Scans all plan-review comments (those whose ``body`` starts with
    :data:`PLAN_REVIEW_PREFIX`) in chronological order and counts the ones
    where :func:`latest_verdict` returns ``None``.  This is the number of
    passes in which a reviewer posted a comment but :func:`parse_review_verdict`
    could not find a plan-state token.

    A non-zero count indicates the reviewer is producing malformed output.
    When the count reaches :data:`MAX_UNPARSEABLE_VERDICT_PASSES` the
    pipeline should stop re-triggering reviews and surface the issue for human
    attention (see :func:`exceeds_unparseable_verdict_cap`).

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

    Callers that would normally re-request a plan review should check this
    first.  If it returns ``True``, the caller should skip the re-review and
    surface the issue for human attention instead of looping indefinitely.

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
        Returns an empty list on any failure.

    """
    # get_repo_slug returns only the short repo name (e.g. "Mnemosyne");
    # GraphQL needs the (owner, name) pair, which get_repo_info supplies.
    # PR #575 fixed this in plan_reviewer.py but missed the identical bug here,
    # crashing every implementer-side GO-gate check (#588).
    owner, name = get_repo_info(get_repo_root())
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "  repository(owner:$owner,name:$name){"
        "    issue(number:$number){"
        "      comments(last: 100, orderBy: {field: UPDATED_AT, direction: DESC}){"
        "        nodes{ body updatedAt url }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    try:
        result = _gh_call(
            [
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"number={issue_number}",
            ],
        )
        data = json.loads(result.stdout)
        nodes = (
            data.get("data", {})
            .get("repository", {})
            .get("issue", {})
            .get("comments", {})
            .get("nodes", [])
        )
        return list(reversed(nodes))
    except Exception as exc:  # logged + treated as "no review"
        logger.warning(
            "Failed to fetch comments for issue %s: %s",
            issue_ref(issue_number),
            exc,
        )
        return []


def fetch_all_issue_comments_graphql(
    issue_numbers: list[int],
) -> dict[int, list[dict[str, Any]]]:
    """Batch-fetch comments for multiple issues in one aliased GraphQL call.

    Mirrors the aliased batching pattern used by
    :func:`hephaestus.automation.github_api._fetch_batch_states` for issue
    states.  Instead of ``N`` individual round-trips (one per issue), a single
    query aliases each issue as ``issue{idx}`` and retrieves up to 100
    comments per issue ordered by ``UPDATED_AT DESC``.  The results are
    reversed to chronological order so downstream "last match wins" semantics
    (e.g. :func:`latest_verdict`) work correctly.

    This function is the shared implementation backing both:

    - :class:`hephaestus.automation.planner_state.PlannerStateManager.has_existing_plan`
      (plan-detection during the planning phase), and
    - :func:`is_plan_review_go` (review-gate during the review phase).

    Falls back to an empty list per issue on any failure.

    Args:
        issue_numbers: List of GitHub issue numbers to fetch.

    Returns:
        Mapping of ``issue_number → list[comment_dict]`` in chronological
        order (oldest first).  Issues that could not be fetched map to ``[]``.

    """
    if not issue_numbers:
        return {}

    owner, name = get_repo_info(get_repo_root())

    # owner/name AND each issue number MUST be passed as GraphQL variables, not
    # interpolated. A {owner!r} repr produces single-quoted literals
    # (owner:'Owner'), which is invalid GraphQL (only double quotes are accepted)
    # and gh rejects with "Expected VALUE, actual: UNKNOWN_CHAR". Mirror
    # github_api._fetch_batch_states: declare $owner/$name/$nN and pass via -F.
    var_decls = ",".join(f"$n{idx}:Int!" for idx in range(len(issue_numbers)))
    fragments = " ".join(
        (
            f"issue{idx}: issue(number:$n{idx}){{"
            "comments(last: 100, orderBy: {field: UPDATED_AT, direction: DESC})"
            "{nodes{body updatedAt url}}"
            "}"
        )
        for idx in range(len(issue_numbers))
    )
    query = (
        f"query($owner:String!,$name:String!,{var_decls})"
        f"{{repository(owner:$owner,name:$name){{{fragments}}}}}"
    )

    # Map alias index back to issue number for result assembly.
    idx_to_num = dict(enumerate(issue_numbers))
    result_map: dict[int, list[dict[str, Any]]] = {num: [] for num in issue_numbers}

    args = [
        "api",
        "graphql",
        "-f",
        f"query={query}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
    ]
    for idx, issue_num in enumerate(issue_numbers):
        args.extend(["-F", f"n{idx}={int(issue_num)}"])

    try:
        result = _gh_call(args)
        data = json.loads(result.stdout)
        repo_data = data.get("data", {}).get("repository", {})
        for alias, issue_data in repo_data.items():
            if not alias.startswith("issue"):
                continue
            try:
                idx = int(alias[len("issue") :])
            except ValueError:
                continue
            num = idx_to_num.get(idx)
            if num is None or issue_data is None:
                continue
            nodes = issue_data.get("comments", {}).get("nodes", []) or []
            # GraphQL returns newest-first; reverse to chronological order.
            result_map[num] = list(reversed(nodes))
    except Exception as exc:  # logged, callers get empty lists
        logger.warning(
            "Failed to batch-fetch comments for issues %s: %s",
            issue_numbers,
            exc,
        )

    return result_map


def fetch_all_issue_labels_graphql(
    issue_numbers: list[int],
) -> dict[int, list[str]]:
    """Batch-fetch label names for multiple issues in one aliased GraphQL call.

    Mirrors :func:`fetch_all_issue_comments_graphql` but retrieves each issue's
    label names instead of comments. One aliased query replaces ``N`` per-issue
    ``gh issue view`` round-trips, so the planner can cheaply drop already-GO
    (``state:plan-go``) issues from its work set before the worker pool starts
    (avoids re-scanning every open issue every loop).

    Falls back to an empty list per issue on any failure (caller then treats the
    issue as "labels unknown" and re-evaluates it the slow way).

    Args:
        issue_numbers: List of GitHub issue numbers to fetch.

    Returns:
        Mapping of ``issue_number → list[label_name]``. Issues that could not be
        fetched map to ``[]``.

    """
    if not issue_numbers:
        return {}

    owner, name = get_repo_info(get_repo_root())

    # owner/name and each issue number as GraphQL variables (never interpolated);
    # see fetch_all_issue_comments_graphql and github_api._fetch_batch_states.
    var_decls = ",".join(f"$n{idx}:Int!" for idx in range(len(issue_numbers)))
    fragments = " ".join(
        f"issue{idx}: issue(number:$n{idx}){{labels(first: 50){{nodes{{name}}}}}}"
        for idx in range(len(issue_numbers))
    )
    query = (
        f"query($owner:String!,$name:String!,{var_decls})"
        f"{{repository(owner:$owner,name:$name){{{fragments}}}}}"
    )

    idx_to_num = dict(enumerate(issue_numbers))
    result_map: dict[int, list[str]] = {num: [] for num in issue_numbers}

    args = [
        "api",
        "graphql",
        "-f",
        f"query={query}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
    ]
    for idx, issue_num in enumerate(issue_numbers):
        args.extend(["-F", f"n{idx}={int(issue_num)}"])

    try:
        result = _gh_call(args)
        data = json.loads(result.stdout)
        repo_data = data.get("data", {}).get("repository", {})
        for alias, issue_data in repo_data.items():
            if not alias.startswith("issue"):
                continue
            try:
                idx = int(alias[len("issue") :])
            except ValueError:
                continue
            num = idx_to_num.get(idx)
            if num is None or issue_data is None:
                continue
            nodes = issue_data.get("labels", {}).get("nodes", []) or []
            result_map[num] = [n.get("name", "") for n in nodes if n.get("name")]
    except Exception as exc:  # logged, callers get empty lists
        logger.warning(
            "Failed to batch-fetch labels for issues %s: %s",
            issue_numbers,
            exc,
        )

    return result_map


def fetch_all_issue_titles_graphql(
    issue_numbers: list[int],
) -> dict[int, str]:
    """Batch-fetch issue titles in one aliased GraphQL call.

    Sibling of :func:`fetch_all_issue_labels_graphql`. The planner uses it
    alongside the labels fetch so :func:`~hephaestus.automation.state_labels.
    is_epic` can apply its title-based signal (catching epics/roadmaps that
    carry no label) without a per-issue ``gh issue view`` (#1669).

    Falls back to an empty string per issue on any failure (caller then treats
    the title as "unknown" and relies on labels alone).

    Args:
        issue_numbers: List of GitHub issue numbers to fetch.

    Returns:
        Mapping of ``issue_number → title``. Issues that could not be fetched
        map to ``""``.

    """
    if not issue_numbers:
        return {}

    owner, name = get_repo_info(get_repo_root())

    # owner/name and each issue number as GraphQL variables (never interpolated);
    # mirrors fetch_all_issue_labels_graphql.
    var_decls = ",".join(f"$n{idx}:Int!" for idx in range(len(issue_numbers)))
    fragments = " ".join(
        f"issue{idx}: issue(number:$n{idx}){{title}}" for idx in range(len(issue_numbers))
    )
    query = (
        f"query($owner:String!,$name:String!,{var_decls})"
        f"{{repository(owner:$owner,name:$name){{{fragments}}}}}"
    )

    idx_to_num = dict(enumerate(issue_numbers))
    result_map: dict[int, str] = dict.fromkeys(issue_numbers, "")

    args = [
        "api",
        "graphql",
        "-f",
        f"query={query}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
    ]
    for idx, issue_num in enumerate(issue_numbers):
        args.extend(["-F", f"n{idx}={int(issue_num)}"])

    try:
        result = _gh_call(args)
        data = json.loads(result.stdout)
        repo_data = data.get("data", {}).get("repository", {})
        for alias, issue_data in repo_data.items():
            if not alias.startswith("issue"):
                continue
            try:
                idx = int(alias[len("issue") :])
            except ValueError:
                continue
            num = idx_to_num.get(idx)
            if num is None or issue_data is None:
                continue
            result_map[num] = issue_data.get("title", "") or ""
    except Exception as exc:  # pragma: no cover - logged, callers get empty strings
        logger.warning(
            "Failed to batch-fetch titles for issues %s: %s",
            issue_numbers,
            exc,
        )

    return result_map


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
        ``True`` iff ``state:plan-go`` is present on the issue; otherwise
        ``False``, including label-fetch failures.

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
    is_go = labels_are_plan_go(issue_labels)
    logger.debug(
        "Issue %s: authoritative plan label is %s",
        issue_ref(issue_number),
        "GO" if is_go else "not GO",
    )
    return is_go
