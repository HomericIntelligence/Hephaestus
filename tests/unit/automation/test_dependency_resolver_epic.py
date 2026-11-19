"""Behavioral tests for the epic loader and sub-issue parser.

Both surfaces take their GitHub reads from the same module-level symbols, so
they are mocked with ``mock.patch`` to keep the tests hermetic.
"""

from __future__ import annotations

from unittest.mock import patch

from hephaestus.automation.dependency_resolver import DependencyResolver
from hephaestus.automation.models import IssueInfo, IssueState

_PATCH = "hephaestus.automation.dependency_resolver"


def _issue(
    number: int,
    body: str = "",
    labels: list[str] | None = None,
    state: IssueState = IssueState.OPEN,
) -> IssueInfo:
    return IssueInfo(
        number=number,
        title=f"issue-{number}",
        body=body,
        labels=list(labels or []),
        state=state,
    )


def _empty_payload() -> dict[str, str]:
    return {"body": ""}


def test_parse_sub_issues_checkbox_markers() -> None:
    """Checkbox task items in the epic body are parsed regardless of state."""
    body = "- [ ] #11\n- [x] #12\n* #13\n1. #99\n"
    with patch(f"{_PATCH}.gh_issue_json", return_value={"body": body}):
        result = DependencyResolver()._parse_sub_issues(_issue(1))
    assert sorted(result) == [11, 12, 13]


def test_parse_sub_issues_keyword_lines() -> None:
    """Keyword markers ``sub-issues:`` / ``includes:`` are parsed."""
    with patch(
        f"{_PATCH}.gh_issue_json",
        return_value={"body": "sub-issues: #21\nincludes: #22\nother #99\n"},
    ):
        result = DependencyResolver()._parse_sub_issues(_issue(1))
    assert sorted(result) == [21, 22]


def test_parse_sub_issues_empty_body_returns_empty() -> None:
    """An epic body with no sub-issue references returns an empty list."""
    """An epic body with no sub-issue references returns an empty list."""
    with patch(f"{_PATCH}.gh_issue_json", return_value=_empty_payload()):
        assert DependencyResolver()._parse_sub_issues(_issue(1)) == []


def test_load_epic_loads_open_sub_issues() -> None:
    """An epic with an OPEN child issue admits it to the graph."""
    """An epic for an OPEN child issue adds that issue to the graph."""
    calls = {"n": 0}

    def _fake_fetch(num: int) -> IssueInfo:
        calls["n"] += 1
        return _issue(num)

    with (
        patch(f"{_PATCH}.fetch_issue_info", side_effect=_fake_fetch),
        patch(f"{_PATCH}.gh_issue_json", return_value={"body": "- [ ] #42\n"}),
        patch(f"{_PATCH}.prefetch_issue_states", return_value={42: IssueState.OPEN}),
    ):
        resolver = DependencyResolver()
        resolver.load_epic(1)

    assert 42 in resolver.graph.issues
    assert 1 in resolver.graph.issues
    assert calls["n"] >= 2


def test_load_epic_skips_closed_sub_issues() -> None:
    """A CLOSED sub-issue is completed and excluded from the graph."""
    """A sub-issue whose state is CLOSED is marked complete, not admitted."""

    def _fake_fetch(num: int) -> IssueInfo:
        if num == 1:
            return _issue(1, body="- [ ] #42\n")
        return _issue(num)

    with (
        patch(f"{_PATCH}.fetch_issue_info", side_effect=_fake_fetch),
        patch(f"{_PATCH}.gh_issue_json", return_value={"body": "- [ ] #42\n"}),
        patch(f"{_PATCH}.prefetch_issue_states", return_value={42: IssueState.CLOSED}),
    ):
        resolver = DependencyResolver(skip_closed=True)
        resolver.load_epic(1)

    assert 42 in resolver.completed
    assert 42 not in resolver.graph.issues
