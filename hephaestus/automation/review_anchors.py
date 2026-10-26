"""Place review comments on the diff GitHub shows.

A comment with a path and a line on the new side of the diff becomes an inline
comment. Every other comment stays in the review body, so no comment is lost.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def valid_review_positions(diff_text: str) -> dict[str, set[int]]:
    """Map each changed file to the new-side lines GitHub accepts for a comment.

    GitHub answers HTTP 422 for an inline comment on a line that is not in the
    diff. An added or unchanged line in a hunk is accepted.

    Args:
        diff_text: Unified diff, the output of ``gh pr diff``.

    Returns:
        ``{path: {line, ...}}`` for every changed file.

    """
    positions: dict[str, set[int]] = {}
    path: str | None = None
    new_line = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else target.removeprefix("b/")
            if path is not None:
                positions.setdefault(path, set())
            continue
        if raw.startswith("--- "):
            continue
        header = _HUNK_HEADER_RE.match(raw)
        if header:
            new_line = int(header.group(2))
            continue
        if path is None or not raw:
            continue
        marker = raw[0]
        if marker in "+ ":
            positions[path].add(new_line)
            new_line += 1
    return positions


def place_findings(
    findings: Sequence[dict[str, object]], diff_text: str
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Split findings into inline comments and findings the diff cannot place.

    Args:
        findings: Findings with a ``body`` and, when known, a ``path`` and a ``line``.
        diff_text: The unified diff of the pull request.

    Returns:
        The inline findings and the findings that stay in the review body.

    """
    positions = valid_review_positions(diff_text)
    inline: list[dict[str, object]] = []
    body_only: list[dict[str, object]] = []
    for finding in findings:
        path = finding.get("path")
        line = finding.get("line")
        if isinstance(path, str) and isinstance(line, int) and line in positions.get(path, ()):
            inline.append(finding)
        else:
            body_only.append(finding)
    return inline, body_only
