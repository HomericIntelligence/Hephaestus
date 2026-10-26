"""Placing review comments on the diff."""

from __future__ import annotations

from hephaestus.automation.review_anchors import place_findings, valid_review_positions

_DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n x = 1\n+y = 2\n z = 3\n"


def test_new_side_lines_in_a_hunk_are_valid() -> None:
    """New side lines in a hunk are valid."""
    assert valid_review_positions(_DIFF) == {"a.py": {1, 2, 3}}


def test_findings_split_into_inline_and_body_only() -> None:
    """Findings split into inline and body only."""
    on_diff: dict[str, object] = {"path": "a.py", "line": 2, "body": "a"}
    off_diff: dict[str, object] = {"path": "a.py", "line": 9, "body": "b"}
    no_line: dict[str, object] = {"path": "a.py", "body": "c"}
    other_file: dict[str, object] = {"path": "b.py", "line": 1, "body": "d"}

    inline, body_only = place_findings([on_diff, off_diff, no_line, other_file], _DIFF)

    assert inline == [on_diff]
    assert body_only == [off_diff, no_line, other_file]


def test_no_diff_keeps_every_finding_in_the_body() -> None:
    """No diff keeps every finding in the body."""
    finding = {"path": "a.py", "line": 2, "body": "a"}

    assert place_findings([finding], "") == ([], [finding])
