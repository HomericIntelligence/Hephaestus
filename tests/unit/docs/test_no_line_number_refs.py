"""Guard: living docs must not embed volatile ``path:LINE`` code refs (issue #2122).

A doc that cites ``file.py:LINE`` re-breaks on every edit above that line and,
under concurrent merging, can only be "correct" at the merge instant — the root
cause of PR #2056's stranding. Living docs must reference ``path function``
instead. ADRs and release notes are excluded: they are point-in-time records
(per ``docs/adr/README.md``) whose line refs are historical citations, not
navigable claims that must track HEAD.
"""

from __future__ import annotations

import re
from pathlib import Path

_DOCS_DIR = Path(__file__).resolve().parents[3] / "docs"
# Point-in-time record dirs whose line refs are historical, not navigable.
_EXCLUDED_DIRS = ("adr", "release-notes")
_LINE_REF_RE = re.compile(r"\.py:\d+")


def _prose_lines(text: str) -> list[tuple[int, str]]:
    """Return ``(lineno, line)`` for lines outside fenced code blocks.

    Example tool output inside fences (``file.py:12: error``) is legitimate and
    must not false-positive.
    """
    out: list[tuple[int, str]] = []
    in_fence = False
    for i, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append((i, line))
    return out


def test_living_docs_have_no_line_number_refs() -> None:
    """Living docs reference ``path function``, never ``path:LINE``."""
    violations: list[str] = []
    for md in sorted(_DOCS_DIR.rglob("*.md")):
        rel = md.relative_to(_DOCS_DIR)
        if rel.parts[0] in _EXCLUDED_DIRS:
            continue
        for lineno, line in _prose_lines(md.read_text(encoding="utf-8")):
            if _LINE_REF_RE.search(line):
                violations.append(f"docs/{rel}:{lineno}: {line.strip()[:80]}")
    assert not violations, (
        "Docs must reference `path function`, not `path:LINE` (issue #2122):\n"
        + "\n".join(violations)
    )


def test_guard_detects_synthetic_line_ref() -> None:
    """A prose ``path:LINE`` ref trips; the same pattern inside a fence does not."""
    text = "See `hephaestus/io/utils.py:142` for details.\n```\nfile.py:1: error\n```\n"
    hits = [ln for ln, line in _prose_lines(text) if _LINE_REF_RE.search(line)]
    assert hits == [1]
