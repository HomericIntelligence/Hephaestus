"""Edit the ``## Testing`` section of a pull-request body.

The implementation stage composes the PR body once, when it opens the pull
request. A later remediation cycle runs the pre-PR test command again and
writes a new receipt. The reviewed pull request must show the receipt that
belongs to its head, so the stage rewrites one section.

This module holds the pure text edit. The stage owns the GitHub read and write.
Every other section stays byte for byte, including an operator edit.
"""

from __future__ import annotations

__all__ = ["TESTING_HEADING", "replace_testing_section"]

#: The section this module owns.
TESTING_HEADING = "## Testing"

_CLOSES_HEADING = "## Closes"


def _heading_index(lines: list[str], heading: str) -> int | None:
    """Return the index of one exact level-two heading line, or ``None``."""
    for index, line in enumerate(lines):
        if line.strip() == heading:
            return index
    return None


def _section_end(lines: list[str], start: int) -> int:
    """Return the index of the next level-two heading after *start*.

    Args:
        lines: Body lines without the line terminators.
        start: Index of the owned heading line.

    Returns:
        The next heading index, or ``len(lines)`` when the section runs to the
        end of the body.

    """
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.startswith("## ") and line.strip():
            return index
    return len(lines)


def _join(body: str, lines: list[str]) -> str:
    """Join edited lines with the line ending the body uses."""
    while lines and not lines[-1].strip():
        lines.pop()
    joined = "\n".join(lines)
    if body.endswith("\n"):
        return f"{joined}\n"
    return joined


def _insert_section(body: str, lines: list[str], text: list[str]) -> str:
    """Add the section before ``## Closes``, or at the end when it is absent."""
    closes = _heading_index(lines, _CLOSES_HEADING)
    block = [TESTING_HEADING, *text, ""]
    if closes is None:
        return _join(body, [*lines, "", *block])
    before = list(lines[:closes])
    while before and not before[-1].strip():
        before.pop()
    return _join(body, [*before, "", *block, *lines[closes:]])


def replace_testing_section(body: str, testing: str) -> str:
    """Return *body* with its ``## Testing`` text replaced by *testing*.

    The rewrite is idempotent: the same *testing* text yields the same body, so
    a caller can compare the result to skip a GitHub write.

    Args:
        body: The current pull-request body.
        testing: The receipt text that belongs to the current head.

    Returns:
        The edited body. An empty *testing* text returns *body* unchanged,
        because the caller has nothing to state.

    """
    text = testing.strip()
    if not text:
        return body
    lines = body.splitlines()
    start = _heading_index(lines, TESTING_HEADING)
    if start is None:
        return _insert_section(body, lines, text.splitlines())
    end = _section_end(lines, start)
    replacement = text.splitlines()
    if end < len(lines):
        replacement.append("")
    return _join(body, [*lines[: start + 1], *replacement, *lines[end:]])
