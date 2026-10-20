"""Shared utilities for GitHub issue comment body rendering and validation."""

from __future__ import annotations

import html
import json
from typing import Any


METADATA_RECORD_SUMMARY = "Hephaestus metadata record."


def visible_issue_comment_text(body: str) -> str:
    """Return the visible text after removing complete HTML comments.

    HTML comments use the form <!-- ... -->. Unmatched delimiters are
    rejected. Empty or whitespace-only visible text is rejected.

    Args:
        body: The raw comment body string.

    Returns:
        The visible text after stripping HTML comments.

    Raises:
        ValueError: If the body contains unmatched comment delimiters or no
        visible text remains.
    """
    if not isinstance(body, str):
        raise ValueError("body must be a string")

    visible = remove_html_comments(body)
    # Strip whitespace from both ends for visibility check
    stripped = visible.strip()
    if not stripped:
        raise ValueError("comment body has no visible text")

    return visible


def remove_html_comments(body: str) -> str:
    """Remove complete HTML comments <!-- ... --> from the body.

    Delimiters are checked for balance. Only complete comments are removed.
    Unmatched <!-- or --> raises ValueError.

    Args:
        body: The input string.

    Returns:
        The string with all complete HTML comments removed.

    Raises:
        ValueError: If there is an unmatched delimiter.
    """
    if not isinstance(body, str):
        raise ValueError("body must be a string")

    result: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        # Check for start of comment <!--
        if i + 4 <= n and body[i : i + 4] == "<!--":
            # Find end of comment -->
            j = body.find("-->", i + 4)
            if j == -1:
                raise ValueError("unmatched HTML comment delimiter <!--")
            # Skip the entire comment
            i = j + 3
            continue
        # Check for end of comment without start (unmatched -->)
        if i + 3 <= n and body[i : i + 3] == "-->":
            raise ValueError("unmatched HTML comment delimiter -->")
        result.append(body[i])
        i += 1

    return "".join(result)


def encode_hidden_comment_json(value: Any) -> str:
    """Encode a JSON value for embedding in a hidden HTML comment.

    The JSON is encoded canonically with ensure_ascii=True, compact
    separators, and sorted keys. Characters that could be interpreted
    as HTML are escaped in the encoded string representation.
    """
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    # Escape HTML special characters
    return html.escape(encoded, quote=False)


def decode_hidden_comment_json(text: str) -> Any:
    """Decode JSON that was encoded with encode_hidden_comment_json.

    The JSON must round-trip canonically - re-encoding must produce
    the same canonical form. This prevents unsafe literal delimiters
    and noncanonical data.
    """
    if not isinstance(text, str):
        raise ValueError("text must be a string")

    try:
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("journal JSON is malformed or has duplicate members") from error

    # Verify canonical round-trip
    canonical = json.dumps(
        parsed,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    if canonical != text:
        raise ValueError("journal JSON is not canonical")

    return parsed


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a dict rejecting duplicate member names."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result
