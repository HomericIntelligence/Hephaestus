"""Shared plan-review verdict gate for the automation pipeline.

The implementer needs to know whether a GitHub issue's *latest* plan-review
comment is a **GO** before it implements the plan. The whole pipeline uses a
single binary verdict vocabulary — ``Verdict: GO`` / ``Verdict: NOGO`` — so the
verdict line is purely a machine-readable gate; the review prose above it
explains *why*.

(Earlier the gate spoke a three-way ``APPROVED/REVISE/BLOCK`` vocabulary. REVISE
and BLOCK never had distinct runtime behavior — the gate only ever asked "is it
the pass verdict" — so the vocabulary was collapsed to a single GO/NOGO flag.)

Two parsers, deliberately: the in-loop reviewer uses
:func:`~hephaestus.automation.claude_invoke.parse_review_verdict` (first match;
its prompt contract is "exactly one verdict line") to decide loop termination.
This module's gate (:func:`latest_verdict`) scans a *posted* review comment for
the LAST verdict line — a persisted comment is longer-lived and may accumulate
discussion, and the reviewer's final word must win (failing toward NOGO is
safe; failing toward GO would implement an unreviewed plan).

The module deliberately accepts either an ``issue_number`` (in which case it
does the GraphQL fetch itself, with ``last: 100`` pagination matching the
reviewer) or a pre-fetched list of comment dicts (so callers can re-use a
per-instance cache).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .git_utils import get_repo_info, get_repo_root, issue_ref
from .github_api import _gh_call

logger = logging.getLogger(__name__)

# Comment-body prefix used when posting plan-review comments. We identify
# "plan review comments" by this prefix on ``body.startswith(...)``.
PLAN_REVIEW_PREFIX = "## 🔍 Plan Review"

# Verdict line in a *posted* plan-review comment. The gate scans for ALL
# matching lines and takes the LAST one (see ``latest_verdict``): a stored
# review may contain a reviewer's earlier draft verdict before the final one
# (e.g. "Verdict: GO … on reflection … Verdict: NOGO"), and the reviewer's
# FINAL word must win — failing toward NOGO is safe (re-review), failing toward
# GO would implement an unreviewed plan (the #455/#468/#484 class of bug). This
# is intentionally STRICTER than the loop's first-match ``parse_review_verdict``
# (whose contract is "exactly one verdict line"): the persisted comment is
# longer-lived and may accumulate discussion, so the gate must be robust to it.
# Matches the same surface as ``parse_review_verdict._VERDICT_RE``: optional
# bold, line-anchored, ``GO`` / ``NOGO`` / ``NO-GO`` / ``NO GO``, case-insensitive.
_GATE_VERDICT_RE = re.compile(
    r"^\s*\**\s*Verdict\s*:\s*\**\s*(GO|NO[\s-]?GO)\b",
    re.MULTILINE | re.IGNORECASE,
)

# Maximum length for verdict context preview in logs (e.g., first verdict line or content).
_VERDICT_LOG_PREVIEW_CHARS = 200

# Maximum number of passes an issue may go through where a plan-review comment
# exists but its verdict cannot be parsed.  After this many unparseable-verdict
# passes the caller should surface the issue for human attention rather than
# requesting yet another review cycle.  See #615.
MAX_UNPARSEABLE_VERDICT_PASSES: int = 3


def latest_verdict(review_body: str) -> str | None:
    """Return the LAST verdict token in a posted plan-review body.

    Scans for every well-formed ``Verdict: GO|NOGO`` line and returns the LAST
    one's normalized token. Taking the *last* line (not the first) means a
    review that discussed an earlier verdict before settling resolves to the
    reviewer's final word — and a malformed/absent verdict resolves to ``None``,
    which every gate treats as not-GO (fail safe).

    Args:
        review_body: Full text of a plan-review comment (starting with
            :data:`PLAN_REVIEW_PREFIX`).

    Returns:
        ``"GO"`` or ``"NOGO"`` (last matching line), or ``None`` when no verdict
        line is present (callers like :func:`count_unparseable_verdict_passes`
        treat ``None`` as "unparseable").

    """
    matches = _GATE_VERDICT_RE.findall(review_body)
    if not matches:
        return None
    raw = re.sub(r"[\s-]", "", matches[-1].upper())
    return "GO" if raw == "GO" else "NOGO"


def _extract_verdict_context(review_body: str) -> str:
    """Extract a human-readable context line from a review body.

    Returns the last line containing 'Verdict:' if present, else the first
    non-empty line that doesn't start with PLAN_REVIEW_PREFIX. Truncated to
    _VERDICT_LOG_PREVIEW_CHARS for logging. Useful for diagnosing missing or
    unexpected verdicts by showing actual content rather than just the token.

    Args:
        review_body: Full text of a plan-review comment.

    Returns:
        A preview string (may be empty if body is empty or all-prefix).

    """
    lines = review_body.split("\n")

    # Look for a line containing "Verdict:" (any case variation)
    for line in reversed(lines):
        if "Verdict:" in line:
            preview = line.strip()
            if preview:
                return preview[:_VERDICT_LOG_PREVIEW_CHARS]

    # Fall back to first non-prefix content line
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith(PLAN_REVIEW_PREFIX):
            return stripped[:_VERDICT_LOG_PREVIEW_CHARS]

    return ""


def count_unparseable_verdict_passes(comments: list[dict[str, Any]]) -> int:
    """Count how many plan-review comments lack a parseable verdict.

    Scans all plan-review comments (those whose ``body`` starts with
    :data:`PLAN_REVIEW_PREFIX`) in chronological order and counts the ones
    where :func:`latest_verdict` returns ``None``.  This is the number of
    passes in which a reviewer posted a comment but :func:`parse_review_verdict`
    could not find a ``Verdict: GO/NOGO`` line (returned ``AMBIGUOUS``).

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
        if body.startswith(PLAN_REVIEW_PREFIX) and latest_verdict(body) is None:
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
    # get_repo_slug returns only the short repo name (e.g. "ProjectMnemosyne");
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
    except Exception as exc:  # pragma: no cover - logged + treated as "no review"
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

    # Build one aliased fragment per issue: issueN: issue(number: <n>) { ... }
    fragments = [
        (
            f"issue{idx}: issue(number: {int(num)}){{"
            "comments(last: 100, orderBy: {field: UPDATED_AT, direction: DESC})"
            "{nodes{body updatedAt url}}"
            "}}"
        )
        for idx, num in enumerate(issue_numbers)
    ]
    query = f"query{{repository(owner:{owner!r},name:{name!r}){{{' '.join(fragments)}}}}}"

    # Map alias index back to issue number for result assembly.
    idx_to_num = dict(enumerate(issue_numbers))
    result_map: dict[int, list[dict[str, Any]]] = {num: [] for num in issue_numbers}

    try:
        result = _gh_call(["api", "graphql", "-f", f"query={query}"])
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
    except Exception as exc:  # pragma: no cover - logged, callers get empty lists
        logger.warning(
            "Failed to batch-fetch comments for issues %s: %s",
            issue_numbers,
            exc,
        )

    return result_map


def is_plan_review_go(
    issue_number: int,
    comments: list[dict[str, Any]] | None = None,
) -> bool:
    """Return True iff the LATEST plan-review on the issue is a GO.

    Single source of truth for the plan-review gate. Used by
    :meth:`PlanReviewer._latest_review_is_final` (so the reviewer skips issues
    whose plan was already cleared) and by the implementer (so it never
    implements a NOGO plan, or one with no review at all). The verdict is read
    by :func:`latest_verdict` (LAST verdict line wins — the reviewer's final
    word gates).

    Args:
        issue_number: GitHub issue number. Only used for logging and as
            input to the GraphQL fetch when ``comments`` is ``None``.
        comments: Pre-fetched list of issue comment dicts in
            chronological order, or ``None`` to fetch via GraphQL.
            Each dict must expose ``body``. Callers may pass a
            per-instance cache; the implementer passes ``None``.

    Returns:
        ``True`` iff at least one plan-review comment exists *and* the most
        recent one parses to an unambiguous ``GO``. ``False`` for all other
        states: NOGO verdict, unparseable/ambiguous verdict, missing review, or
        comment-fetch failure.

    """
    if comments is None:
        comments = _fetch_issue_comments_graphql(issue_number)

    latest_review_body: str | None = None
    latest_review_url: str | None = None
    for comment in comments:
        body: str = comment.get("body", "")
        if body.startswith(PLAN_REVIEW_PREFIX):
            latest_review_body = body
            latest_review_url = comment.get("url")

    if latest_review_body is None:
        logger.debug(
            "Issue %s: no plan-review comment found",
            issue_ref(issue_number),
        )
        return False

    # Last-verdict-wins (see latest_verdict): the reviewer's FINAL word gates.
    verdict = latest_verdict(latest_review_body)
    if verdict == "GO":
        logger.debug(
            "Issue %s: latest plan review is GO",
            issue_ref(issue_number),
        )
    elif verdict is None:
        # No parseable verdict line — log at WARNING with the first line of the
        # offending body and its URL so the malformed output can be inspected
        # without digging through raw GitHub comments (root cause of #615).
        first_line = latest_review_body.split("\n", 1)[0].strip()
        url_part = latest_review_url or "<no url>"
        logger.warning(
            "Issue %s: plan-review comment has no parseable Verdict: GO/NOGO line "
            "— first line: %r | url: %s",
            issue_ref(issue_number),
            first_line[:_VERDICT_LOG_PREVIEW_CHARS],
            url_part,
        )
    else:
        context = _extract_verdict_context(latest_review_body)
        url_part = f" {latest_review_url}" if latest_review_url else " <no url>"
        logger.debug(
            "Issue %s: latest plan review verdict is %s (not GO) | %s%s",
            issue_ref(issue_number),
            verdict,
            context,
            url_part,
        )
    return verdict == "GO"
