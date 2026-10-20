"""Read the JSON objects that review agents emit.

The module has no transport dependency, so the review value modules can use
it without loading the GitHub client.
"""

from __future__ import annotations

import re

_JSON_BLOCK_RE = re.compile(
    r"^[ \t]*```json[ \t]*\r?\n(.*?)\r?\n^[ \t]*```[ \t]*\r?$",
    re.DOTALL | re.MULTILINE,
)
_JSON_INLINE_BLOCK_RE = re.compile(
    r"^[ \t]*```json[ \t]*([^\n]*?)[ \t]*```[ \t]*\r?$",
    re.MULTILINE,
)


def json_block_spans(text: str) -> list[tuple[int, int, str]]:
    """Return every fenced JSON object in the text, in document order.

    A model sometimes writes the fence and the object on one line. Both
    layouts are read, so a valid answer is not lost to its formatting.

    Args:
        text: The agent response to read.

    Returns:
        The start offset, the end offset, and the body of each fenced JSON
        object, in the order it appears.

    """
    spans: list[tuple[int, int, str]] = [
        (match.start(), match.end(), match.group(1)) for match in _JSON_BLOCK_RE.finditer(text)
    ]
    spans.extend(
        (match.start(), match.end(), match.group(1))
        for match in _JSON_INLINE_BLOCK_RE.finditer(text)
    )
    spans.sort(key=lambda item: item[0])
    return spans


def json_block_bodies(text: str) -> list[str]:
    """Return the body of every fenced JSON object, in document order."""
    return [body for _, _, body in json_block_spans(text)]
