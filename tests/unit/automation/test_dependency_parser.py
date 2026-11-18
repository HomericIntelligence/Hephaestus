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


from hephaestus.automation.dependency_parser import DependencyFact, canonical_dependency_numbers


@pytest.mark.parametrize(
    "factory",
    [
        lambda: DependencyFact(0, "Issue", "OPEN"),
        lambda: DependencyFact(2, "Issue", "MERGED"),  # type: ignore[arg-type]
        lambda: DependencyFact(5, "Issue", "OPEN", merged=False),
        lambda: DependencyFact(3, "Issue", "MERGED"),  # type: ignore[arg-type]
        lambda: DependencyFact(1, "Issue", "BOGUS"),  # type: ignore[arg-type]
        lambda: DependencyFact(4, "PRs", "OPEN"),  # type: ignore[arg-type]
        lambda: DependencyFact(8, "PullRequest", "BOGUS"),  # type: ignore[arg-type]
        lambda: DependencyFact(7, "PullRequest", "MERGED", merged="yes"),  # type: ignore[arg-type]
        lambda: DependencyFact(9, "PullRequest", "MERGED", merged=False),
        lambda: DependencyFact(10, "PullRequest", "CLOSED", merged=True),
    ],
    ids=[
        "zero-number",
        "issue-state-merged",
        "issue-with-merged-set",
        "issue-state-merged-2",
        "issue-bogus-state",
        "pr-bogus-type",
        "pr-bogus-state",
        "pr-merged-not-bool",
        "pr-merged-state-mismatch-false",
        "pr-merged-state-mismatch-true",
    ],
)
def test_dependency_fact_rejects_contradictions(factory) -> None:  # type: ignore[no-untyped-def]
    """Contradictory dependency facts fail closed."""
    with pytest.raises(ValueError):
        factory()


def test_dependency_fact_satisfied_property() -> None:
    """Satisfied reflects merged (requester perspective) for PRs, closed for issues."""
    assert DependencyFact(1, "Issue", "CLOSED").satisfied is True
    assert DependencyFact(2, "Issue", "OPEN").satisfied is False
    assert DependencyFact(3, "PullRequest", "MERGED", merged=True).satisfied is True
    assert DependencyFact(4, "PullRequest", "CLOSED", merged=False).satisfied is False


def test_canonical_dependency_numbers_valid() -> None:
    """Valid orders are returned as tuples."""
    assert canonical_dependency_numbers((3, 7, 9)) == (3, 7, 9)
    assert canonical_dependency_numbers([]) == ()


def test_canonical_dependency_numbers_rejects_unsorted() -> None:
    """Out-of-order input violates canonicity."""
    with pytest.raises(ValueError, match="canonical order"):
        canonical_dependency_numbers((5, 1))


def test_canonical_dependency_numbers_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="unique"):
        canonical_dependency_numbers((4, 4))


def test_canonical_dependency_numbers_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        canonical_dependency_numbers((0, 7))


def test_canonical_dependency_numbers_rejects_too_many() -> None:
    from hephaestus.automation.dependency_parser import MAX_DEPENDENCY_FACTS

    with pytest.raises(ValueError, match="maximum"):
        canonical_dependency_numbers(tuple(range(1, MAX_DEPENDENCY_FACTS + 2)))
