"""Exact top-level Markdown marker recognition for automation identities."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from markdown_it import MarkdownIt

_MARKDOWN_LINE_END_RE: Final[re.Pattern[str]] = re.compile(r"\r\n|\r|\n")


def raw_markdown_lines(body: str) -> list[str]:
    """Return raw CommonMark lines without changing their line endings."""
    lines: list[str] = []
    line_start = 0
    for line_end in _MARKDOWN_LINE_END_RE.finditer(body):
        lines.append(body[line_start : line_end.end()])
        line_start = line_end.end()
    if line_start < len(body):
        lines.append(body[line_start:])
    return lines


def top_level_marker_line_indexes(
    body: str,
    markers: Sequence[str],
) -> tuple[tuple[int, str], ...]:
    """Return exact marker lines that CommonMark exposes at document level."""
    accepted_markers = frozenset(marker for marker in markers if marker)
    if not accepted_markers:
        return ()

    raw_lines = raw_markdown_lines(body)
    matches: list[tuple[int, str]] = []
    for token in MarkdownIt("commonmark").parse(body):
        if token.type != "html_block" or token.level != 0 or token.map is None:
            continue
        start_line, _end_line = token.map
        if start_line >= len(raw_lines):
            continue
        line = raw_lines[start_line]
        if line.endswith("\r\n"):
            line = line[:-2]
        elif line.endswith("\n"):
            line = line[:-1]
        if line in accepted_markers:
            matches.append((start_line, line))
    return tuple(matches)


def top_level_marker_occurrences(body: str, markers: Sequence[str]) -> tuple[str, ...]:
    """Return exact top-level marker occurrences in document order."""
    return tuple(marker for _line_index, marker in top_level_marker_line_indexes(body, markers))


def has_exact_top_level_marker(body: str, marker: str) -> bool:
    """Return whether *marker* is one exact top-level Markdown line in *body*."""
    return bool(top_level_marker_occurrences(body, (marker,)))
