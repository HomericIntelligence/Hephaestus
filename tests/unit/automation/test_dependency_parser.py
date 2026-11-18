"""Behavior tests for dependency declarations in issue bodies (#3624)."""

from __future__ import annotations

import pytest

from hephaestus.automation.dependency_parser import parse_issue_dependencies


def test_part_of_clause_on_same_line_is_not_a_dependency() -> None:
    """The production signature from the issue must not leak the epic."""
    body = "Blocked by: #3326 and ADR PR #3331. Part of #3325."
    assert parse_issue_dependencies(body) == [3326, 3331]


def test_plain_comma_list_still_collected() -> None:
    """A comma list of dependencies is collected as before."""
    assert parse_issue_dependencies("Depends on #10, #20") == [10, 20]


def test_newline_terminates_keyword_scope() -> None:
    """A newline terminates the keyword scope."""
    body = "Blocked by #1\nPart of #99"
    assert parse_issue_dependencies(body) == [1]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Requires #7 (Part of #3), #8", [7, 8]),
        ("Blocked by #7 (parent: #8)", [7]),
        ("Blocked by #7, parent of epic #9", [7]),
        ("Depends on #1; Part of #9", [1]),
        ("Depends on #1; then #2", [1, 2]),
        ("dependencies: #42, #43", [42, 43]),
        ("No dependencies here", []),
    ],
    ids=[
        "paren-parent-ref",
        "paren-parent",
        "parent-clause-skip",
        "semicolon-part-of",
        "semicolon-plain",
        "yaml-style-keyword",
        "no-keywords",
    ],
)
def test_dependency_scenarios(body: str, expected: list[int]) -> None:
    """Dependency declarations survive; parent/epic prose never does."""
    assert parse_issue_dependencies(body) == expected


def test_dependencies_section_remains_honored() -> None:
    """The Dependencies heading section path is unchanged."""
    body = "Intro\n\n## Dependencies\n\n- #42\n- #43\n\n## Notes\n\n- #99\n"
    assert parse_issue_dependencies(body) == [42, 43]


def test_requires_version_with_periods_keeps_later_reference() -> None:
    """Periods inside numeric clauses do not end the keyword scope."""
    body = "Requires version 1.2.3 of the tool, see #55"
    assert parse_issue_dependencies(body) == [55]
