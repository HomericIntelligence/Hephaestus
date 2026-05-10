"""Shared helpers for the PR / plan reviewer trio.

Extracts utilities that were previously duplicated across
``pr_reviewer.py`` and ``address_review.py``.

Provides:
- ``parse_json_block``: Extract the last ```json``` block from Claude output.
- ``find_pr_for_issue``: Locate the open PR for a GitHub issue (two or three
  lookup strategies depending on the caller's needs).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .github_api import _gh_call

logger = logging.getLogger(__name__)


def parse_json_block(text: str) -> dict[str, Any]:
    """Extract the last ```json ... ``` block from Claude's response.

    Args:
        text: Claude's full response text.

    Returns:
        Parsed dict, or a default dict with empty collections on failure.

    """
    matches = re.findall(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if not matches:
        return {"comments": [], "summary": "No structured output from analysis"}
    try:
        return dict(json.loads(matches[-1]))
    except json.JSONDecodeError:
        return {"comments": [], "summary": "Failed to parse structured output from analysis"}


def find_pr_for_issue(
    issue_number: int,
    *,
    extra_strategies: bool = False,
    _load_review_state_fn: Any = None,
) -> int | None:
    """Find the open PR for a single issue.

    Always tries two strategies:

    1. Branch name lookup (``{issue}-auto-impl``).
    2. PR-body text search (``#{issue} in:body``).

    When ``extra_strategies=True`` a third strategy is attempted between 1
    and 2: the stored ``pr_number`` from the on-disk review state is
    checked via ``gh pr view``.  The caller supplies ``_load_review_state_fn``
    (a zero-arg callable that returns a ``ReviewState | None``) to keep this
    module free of circular imports.

    Args:
        issue_number: GitHub issue number.
        extra_strategies: When True, also check the on-disk review state.
        _load_review_state_fn: Callable ``() -> ReviewState | None`` used
            when ``extra_strategies=True``.

    Returns:
        PR number if found, ``None`` otherwise.

    """
    # Strategy 1: branch-name lookup
    branch_name = f"{issue_number}-auto-impl"
    try:
        result = _gh_call(
            [
                "pr",
                "list",
                "--head",
                branch_name,
                "--state",
                "open",
                "--json",
                "number",
                "--limit",
                "1",
            ],
            check=False,
        )
        pr_data = json.loads(result.stdout or "[]")
        if pr_data:
            pr_number = int(pr_data[0]["number"])
            logger.info("Found PR #%d for issue #%d via branch name", pr_number, issue_number)
            return pr_number
    except Exception as e:
        logger.debug("Branch-name lookup failed for issue #%d: %s", issue_number, e)

    # Strategy 2 (optional): on-disk review state
    if extra_strategies and _load_review_state_fn is not None:
        review_state = _load_review_state_fn()
        if review_state is not None and review_state.pr_number:
            try:
                result = _gh_call(
                    [
                        "pr",
                        "view",
                        str(review_state.pr_number),
                        "--json",
                        "number,state",
                    ],
                    check=False,
                )
                pr_data = json.loads(result.stdout or "{}")
                if pr_data.get("state", "").upper() == "OPEN":
                    pr_number = int(review_state.pr_number)
                    logger.info(
                        "Found PR #%d for issue #%d via review state",
                        pr_number,
                        issue_number,
                    )
                    return pr_number
            except Exception as e:
                logger.debug("Review state PR lookup failed for issue #%d: %s", issue_number, e)

    # Strategy 3: PR-body text search
    try:
        result = _gh_call(
            [
                "pr",
                "list",
                "--state",
                "open",
                "--search",
                f"#{issue_number} in:body",
                "--json",
                "number",
                "--limit",
                "5",
            ],
            check=False,
        )
        pr_data = json.loads(result.stdout or "[]")
        if pr_data:
            pr_number = int(pr_data[0]["number"])
            logger.info("Found PR #%d for issue #%d via body search", pr_number, issue_number)
            return pr_number
    except Exception as e:
        logger.debug("Body search failed for issue #%d: %s", issue_number, e)

    return None
