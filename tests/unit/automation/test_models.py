"""Test the issue and dependency models used by the queue."""

import pytest

from hephaestus.automation.models import DependencyGraph, IssueInfo, IssueState


class TestIssueState:
    """Tests for the IssueState enum."""

    def test_merged_is_a_valid_member(self) -> None:
        """A merged-PR dependency yields ``"MERGED"``; it must parse without error.

        Regression: ``dependency_resolver`` crashed with
        ``'MERGED' is not a valid IssueState`` when a ``Depends on #N`` reference
        pointed at a merged PR.
        """
        assert IssueState("MERGED") is IssueState.MERGED

    def test_is_done_terminal_states(self) -> None:
        assert IssueState.CLOSED.is_done is True
        assert IssueState.MERGED.is_done is True
        assert IssueState.OPEN.is_done is False


class TestIssueInfo:
    """Tests for IssueInfo model."""

    def test_basic_creation(self) -> None:
        """Test creating a basic IssueInfo."""
        issue = IssueInfo(
            number=123,
            title="Test issue",
        )

        assert issue.number == 123
        assert issue.title == "Test issue"
        assert issue.body == ""
        assert issue.state == IssueState.OPEN
        assert issue.labels == []
        assert issue.dependencies == []
        assert issue.priority == 0

    def test_with_dependencies(self) -> None:
        """Test IssueInfo with dependencies."""
        issue = IssueInfo(
            number=123,
            title="Test issue",
            dependencies=[100, 101, 102],
        )

        assert issue.dependencies == [100, 101, 102]

    def test_hashable(self) -> None:
        """Test IssueInfo is hashable."""
        issue1 = IssueInfo(number=123, title="Test")
        issue2 = IssueInfo(number=123, title="Different title")
        issue3 = IssueInfo(number=456, title="Test")

        # Same number should hash to same value
        assert hash(issue1) == hash(issue2)
        # Different number should hash differently (usually)
        assert hash(issue1) != hash(issue3)

        # Can be used in sets
        issues = {issue1, issue2, issue3}
        assert len(issues) == 2  # issue1 and issue2 are considered equal

    def test_equality(self) -> None:
        """Test IssueInfo equality."""
        issue1 = IssueInfo(number=123, title="Test")
        issue2 = IssueInfo(number=123, title="Different title")
        issue3 = IssueInfo(number=456, title="Test")

        assert issue1 == issue2  # Same number
        assert issue1 != issue3  # Different number


class TestDependencyGraph:
    """Tests for DependencyGraph model."""

    def test_add_issue(self) -> None:
        """Test adding issues to graph."""
        graph = DependencyGraph()
        issue = IssueInfo(number=123, title="Test")

        graph.add_issue(issue)

        assert 123 in graph.issues
        assert graph.issues[123] == issue
        assert 123 in graph.edges

    def test_add_dependency(self) -> None:
        """Test adding dependency edges."""
        graph = DependencyGraph()

        # Add issues first (now required)
        graph.add_issue(IssueInfo(number=123, title="Main"))
        graph.add_issue(IssueInfo(number=100, title="Dep 1"))
        graph.add_issue(IssueInfo(number=101, title="Dep 2"))

        graph.add_dependency(123, 100)
        graph.add_dependency(123, 101)

        assert graph.get_dependencies(123) == [100, 101]

    def test_get_all_dependencies(self) -> None:
        """Test transitive dependency resolution."""
        graph = DependencyGraph()

        # Add issues first
        graph.add_issue(IssueInfo(number=123, title="Main"))
        graph.add_issue(IssueInfo(number=100, title="Mid"))
        graph.add_issue(IssueInfo(number=50, title="Base"))

        # Create chain: 123 -> 100 -> 50
        graph.add_dependency(123, 100)
        graph.add_dependency(100, 50)

        deps = graph.get_all_dependencies(123)

        assert deps == {100, 50}

    def test_get_all_dependencies_diamond(self) -> None:
        """Test transitive dependencies with diamond pattern."""
        graph = DependencyGraph()

        # Add issues first
        graph.add_issue(IssueInfo(number=123, title="Main"))
        graph.add_issue(IssueInfo(number=100, title="Mid 1"))
        graph.add_issue(IssueInfo(number=101, title="Mid 2"))
        graph.add_issue(IssueInfo(number=50, title="Base"))

        # Create diamond: 123 -> {100, 101} -> 50
        graph.add_dependency(123, 100)
        graph.add_dependency(123, 101)
        graph.add_dependency(100, 50)
        graph.add_dependency(101, 50)

        deps = graph.get_all_dependencies(123)

        assert deps == {100, 101, 50}


def test_issueinfo_eq_with_non_issueinfo_returns_notimplemented() -> None:
    """__eq__ against a non-IssueInfo returns NotImplemented (models.py:74)."""
    issue = IssueInfo(number=1, title="t")
    assert issue.__eq__("not-an-issue") is NotImplemented
    assert (issue == 1) is False


def test_add_issue_idempotent_when_edges_already_present() -> None:
    """Re-adding an issue whose edges key exists skips re-init (models.py:327->exit)."""
    graph = DependencyGraph()
    a = IssueInfo(number=1, title="a")
    graph.add_issue(a)
    graph.add_dependency(1, 2)  # populate edges[1] = [2]
    graph.add_issue(a)  # edges[1] already present -> false branch, no reset
    assert graph.edges[1] == [2]


def test_add_dependency_raises_when_issue_not_in_graph() -> None:
    """add_dependency raises ValueError for an unknown source issue (models.py:346)."""
    graph = DependencyGraph()
    with pytest.raises(ValueError, match=r"Issue #99 not in graph"):
        graph.add_dependency(99, 1)  # issue 99 absent -> raise ValueError


def test_add_dependency_when_edges_key_missing_initializes_list() -> None:
    """add_dependency seeds edges[] when issue present but edges key absent (models.py:349)."""
    graph = DependencyGraph()
    graph.issues[1] = IssueInfo(number=1, title="a")  # issue present, edges key absent
    graph.add_dependency(1, 2)
    assert graph.edges[1] == [2]


def test_add_dependency_skips_duplicate_edge() -> None:
    """Adding an existing edge is a no-op (models.py:350->exit, duplicate-edge guard false side)."""
    graph = DependencyGraph()
    graph.add_issue(IssueInfo(number=1, title="a"))  # add_issue seeds edges[1]=[]
    graph.add_dependency(1, 2)
    graph.add_dependency(1, 2)  # depends_on already in edges[1] -> false branch, no re-append
    assert graph.edges[1] == [2]
