"""Shared plan-review verdict gate for the automation pipeline.

Both :mod:`hephaestus.automation.plan_reviewer` (skip-on-APPROVED) and
:mod:`hephaestus.automation.implementer` (gate-on-APPROVED) need to know
whether a given GitHub issue's *latest* plan-review comment carries the
``**Verdict: APPROVED**`` marker. Until this module existed, the two
sides used independent logic — the reviewer used a regex on the last
verdict line, the implementer used a substring presence check — which
diverged in subtle ways (#551, #552). Putting the gate here makes
``plan_reviewer._latest_review_is_final`` and the new implementer gate
read from a single source of truth.

The module is deliberately small: a verdict regex, a verdict enum-ish
literal, and one helper. It deliberately accepts either an
``issue_number`` (in which case it does the GraphQL fetch itself, with
``last: 100`` pagination matching the reviewer) or a pre-fetched list of
comment dicts (so the reviewer can re-use its per-instance cache).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .git_utils import get_repo_info, get_repo_root, issue_ref
from .github_api import _gh_call

logger = logging.getLogger(__name__)

# Comment-body prefix used by ``PlanReviewer._post_review`` when posting
# review comments. We identify "plan review comments" by this prefix on
# ``body.startswith(...)`` — the reviewer uses the exact same constant.
PLAN_REVIEW_PREFIX = "## 🔍 Plan Review"

# Regex that extracts every well-formed verdict line in a review body.
# Per the prompt contract, ONLY the LAST matching line counts — Claude
# may discuss multiple verdict options in prose before settling, so a
# substring ``in`` check is unsafe (it would fire True on
# ``**Verdict: APPROVED**`` followed by ``**Verdict: BLOCK**``, or on a
# quoted marker inside discussion). See issues #551, #552.
VERDICT_LINE_RE = re.compile(
    r"^\*\*Verdict: (APPROVED|REVISE|BLOCK)\*\*\s*$",
    re.MULTILINE,
)

# Recognised verdict tokens.
VERDICT_APPROVED = "APPROVED"
VERDICT_REVISE = "REVISE"
VERDICT_BLOCK = "BLOCK"


def latest_verdict(review_body: str) -> str | None:
    """Return the LAST well-formed verdict token in ``review_body``.

    Args:
        review_body: Full text of a plan-review comment (starting with
            :data:`PLAN_REVIEW_PREFIX`).

    Returns:
        One of ``"APPROVED"``, ``"REVISE"``, ``"BLOCK"`` (the last
        matching verdict line), or ``None`` if no verdict line is
        present.

    """
    matches = VERDICT_LINE_RE.findall(review_body)
    return matches[-1] if matches else None


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
    # crashing every implementer-side APPROVED-gate check (#588).
    owner, name = get_repo_info(get_repo_root())
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "  repository(owner:$owner,name:$name){"
        "    issue(number:$number){"
        "      comments(last: 100, orderBy: {field: UPDATED_AT, direction: DESC}){"
        "        nodes{ body updatedAt }"
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


def is_plan_review_approved(
    issue_number: int,
    comments: list[dict[str, Any]] | None = None,
) -> bool:
    """Return True iff the LATEST plan-review on the issue is APPROVED.

    Single source of truth for the APPROVED-plan-review gate. Used by
    :meth:`PlanReviewer._latest_review_is_final` (so the reviewer skips
    issues whose plan was already approved) and by the implementer
    (so it never tries to implement a BLOCK/REVISE plan, or one with
    no review at all).

    Args:
        issue_number: GitHub issue number. Only used for logging and as
            input to the GraphQL fetch when ``comments`` is ``None``.
        comments: Pre-fetched list of issue comment dicts in
            chronological order, or ``None`` to fetch via GraphQL.
            Each dict must expose ``body``. The reviewer passes its
            per-instance cache; the implementer passes ``None``.

    Returns:
        ``True`` iff at least one plan-review comment exists *and* the
        last well-formed verdict line in the most-recent plan-review
        comment is ``APPROVED``. ``False`` for all other states:
        REVISE/BLOCK verdict, malformed verdict, missing review, or
        comment-fetch failure.

    """
    if comments is None:
        comments = _fetch_issue_comments_graphql(issue_number)

    latest_review_body: str | None = None
    for comment in comments:
        body: str = comment.get("body", "")
        if body.startswith(PLAN_REVIEW_PREFIX):
            latest_review_body = body

    if latest_review_body is None:
        logger.debug(
            "Issue %s: no plan-review comment found",
            issue_ref(issue_number),
        )
        return False

    verdict = latest_verdict(latest_review_body)
    is_approved = verdict == VERDICT_APPROVED
    if is_approved:
        logger.debug(
            "Issue %s: latest plan review is APPROVED",
            issue_ref(issue_number),
        )
    else:
        logger.debug(
            "Issue %s: latest plan review verdict is %s (not APPROVED)",
            issue_ref(issue_number),
            verdict or "<missing>",
        )
    return is_approved
