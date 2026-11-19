"""Behavioral tests for epic-driven dependency loading."""

from __future__ import annotations

import pytest

from hephaestus.automation import dependency_resolver as resolver
from hephaestus.automation.models import IssueInfo

_PATCH = "hephaestus.automation.dependency_resolver"


class _FakeIssue:
    def __init__(
        self, number: int, body: str = "", is_done: bool = False, title: str = "t"
    ) -> None:
        self.number = number
        self.title = title
        self.body = body
        self.is_done = is_done
        self.url = ""


def _issue(number: int, body: str = "", is_done: bool = False) -> IssueInfo:
    return IssueInfo(
        number=number,
        title=f"issue-{number}",
        url=f"https://github.com/example/project/issues/{number}",
        labels=frozenset(),
        is_done=is_done,
        body=body,
    )


def test_parse_sub_issues_checkbox_markers() -> None:
    """Parse task-list and keyword-list entries from an epic body."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        f"{_PATCH}.gh_issue_json",
        lambda _: {"body": "- [ ] #11\n- [x] #12\n* #13\n1. #99\n"},
    )
    ep = resolver.DependencyResolver()
    assert sorted(
        ep._parse_sub_issues(
            IssueInfo(number=1, title="e", url="u", labels=frozenset(), is_done=False, body="")
        )
    ) == [11, 12, 13]
    monkeypatch.undo()


def test_parse_sub_issues_keywords() -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        f"{_PATCH}.gh_issue_json",
        lambda _: {"body": "sub-issues: #21\nincludes: #22\nother #99\n"},
    )
    ep = resolver.DependencyResolver()
    assert sorted(
        ep._parse_sub_issues(
            IssueInfo(number=1, title="e", url="u", labels=frozenset(), is_done=False, body="")
        )
    ) == [21, 22]
    monkeypatch.undo()


def test_parse_sub_issues_fetch_failure_falls_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{_PATCH}.gh_issue_json",
        lambda _: {"body": ""},
    )
    ep = resolver.DependencyResolver()
    assert (
        ep._parse_sub_issues(
            IssueInfo(number=1, title="e", url="u", labels=frozenset(), is_done=False, body="")
        )
        == []
    )


def test_load_epic_skips_done_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closed issues are marked completed and not loaded."""
    fetched: list[int] = []

    def _fetch(n):
        fetched.append(n)
        return _issue(n, body="#42")

    monkeypatch.setattr(f"{_PATCH}.fetch_issue_info", _fetch)
    monkeypatch.setattr(f"{_PATCH}.gh_issue_json", lambda _: {"body": "- [ ] #42\n"})
    monkeypatch.setattr(
        f"{_PATCH}.prefetch_issue_states", lambda numbers: {42: _FakeIssue(42, is_done=True)}
    )

    ep = resolver.DependencyResolver(skip_closed=True)
    ep.load_epic(1)
    assert 42 in ep.completed
    assert 42 not in ep.graph.issues


def test_load_epic_loads_open_issue_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{_PATCH}.fetch_issue_info", lambda n: _issue(n, body="", is_done=False))
    monkeypatch.setattr(f"{_PATCH}.gh_issue_json", lambda _: {"body": "- [ ] #42\n"})
    monkeypatch.setattr(
        f"{_PATCH}.prefetch_issue_states", lambda numbers: {42: _FakeIssue(42, is_done=False)}
    )

    ep = resolver.DependencyResolver(skip_closed=True)
    ep.load_epic(1)
    assert 42 in ep.graph.issues
