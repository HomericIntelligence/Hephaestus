"""Keep review comments that point at a line GitHub shows in the diff."""

from typing import Any

from hephaestus.automation.review_anchors import place_findings


def _filter_comments_to_diff(
    comments: list[dict[str, Any]], diff_text: str
) -> list[dict[str, Any]]:
    """Return the comments that target a line in the diff.

    An empty diff means the diff could not be fetched, so every comment is kept:
    losing a comment is worse than a possible HTTP 422.
    """
    if not diff_text:
        return list(comments)
    inline, _ = place_findings(comments, diff_text)
    return [dict(comment) for comment in inline]
