"""Tests for DependencyResolver."""

import pytest

from hephaestus.automation.dependency_resolver import (
    CyclicDependencyError,
    DependencyResolver,
)
from hephaestus.automation.models import IssueInfo, IssueState


class TestDependencyResolver:
    """Tests for DependencyResolver class."""

    def test_add_issue(self) -> None:
        """Test adding issues to resolver."""
        resolver = DependencyResolver()

        issue = IssueInfo(number=123, title="Test")
        resolver.add_issue(issue)

        assert 123 in resolver.graph.issues
        assert resolver.graph.issues[123] == issue

    def test_add_issue_with_dependencies(self) -> None:
        """Test adding issue with dependencies."""
        resolver = DependencyResolver()

        issue = IssueInfo(
            number=123,
            title="Test",
            dependencies=[100, 101],
        )
        resolver.add_issue(issue)

        assert resolver.graph.get_dependencies(123) == [100, 101]

    def test_add_issue_refuses_closed_issue(self) -> None:
        """add_issue is a belt-and-braces guard against admitting closed issues (#1832)."""
        resolver = DependencyResolver(skip_closed=True)
        closed = IssueInfo(number=99, title="Closed", state=IssueState.CLOSED)

        resolver.add_issue(closed)

        assert 99 not in resolver.graph.issues
        assert 99 in resolver.completed

    def test_add_issue_refuses_state_skip_issue(self) -> None:
        """add_issue also refuses state:skip issues directly, not just via BFS."""
        resolver = DependencyResolver(skip_closed=True)
        skipped = IssueInfo(number=98, title="Skipped", labels=["state:skip"])

        resolver.add_issue(skipped)

        assert 98 not in resolver.graph.issues
        assert 98 in resolver.completed

    def test_add_issue_admits_closed_issue_when_skip_closed_false(self) -> None:
        """--no-skip-closed preserves legacy behavior at the add_issue guard too."""
        resolver = DependencyResolver(skip_closed=False)
        closed = IssueInfo(number=97, title="Closed", state=IssueState.CLOSED)

        resolver.add_issue(closed)

        assert 97 in resolver.graph.issues
        assert 97 not in resolver.completed

    def test_add_dependency(self) -> None:
        """Test adding a dependency through the resolver public API."""
        resolver = DependencyResolver()

        resolver.add_issue(IssueInfo(number=123, title="Test"))
        resolver.add_dependency(123, 100)

        assert resolver.graph.get_dependencies(123) == [100]

    def test_detect_cycles_no_cycle(self) -> None:
        """Test cycle detection with no cycles."""
        resolver = DependencyResolver()

        # Linear chain: 3 -> 2 -> 1
        resolver.add_issue(IssueInfo(number=1, title="Base"))
        resolver.add_issue(IssueInfo(number=2, title="Middle", dependencies=[1]))
        resolver.add_issue(IssueInfo(number=3, title="Top", dependencies=[2]))

        cycles = resolver.detect_cycles()
        assert cycles == []

    def test_detect_cycles_with_cycle(self) -> None:
        """Test cycle detection with a cycle."""
        resolver = DependencyResolver()

        # Cycle: 1 -> 2 -> 3 -> 1
        resolver.add_issue(IssueInfo(number=1, title="A", dependencies=[3]))
        resolver.add_issue(IssueInfo(number=2, title="B", dependencies=[1]))
        resolver.add_issue(IssueInfo(number=3, title="C", dependencies=[2]))

        with pytest.raises(CyclicDependencyError):
            resolver.detect_cycles()

    def test_get_ready_issues_none_ready(self) -> None:
        """Test getting ready issues when none are ready."""
        resolver = DependencyResolver()

        # Both issues have dependencies
        resolver.add_issue(IssueInfo(number=2, title="B", dependencies=[1]))
        resolver.add_issue(IssueInfo(number=3, title="C", dependencies=[2]))

        ready = resolver.get_ready_issues()
        assert len(ready) == 0

    def test_get_ready_issues_some_ready(self) -> None:
        """Test getting ready issues when some are ready."""
        resolver = DependencyResolver()

        # Issue 1 has no dependencies (ready)
        # Issue 2 depends on 1 (not ready)
        resolver.add_issue(IssueInfo(number=1, title="A"))
        resolver.add_issue(IssueInfo(number=2, title="B", dependencies=[1]))

        ready = resolver.get_ready_issues()
        assert len(ready) == 1
        assert ready[0].number == 1

    def test_get_ready_issues_after_completion(self) -> None:
        """Test getting ready issues after marking one complete."""
        resolver = DependencyResolver()

        resolver.add_issue(IssueInfo(number=1, title="A"))
        resolver.add_issue(IssueInfo(number=2, title="B", dependencies=[1]))

        # Mark 1 as completed
        resolver.mark_completed(1)

        ready = resolver.get_ready_issues()
        assert len(ready) == 1
        assert ready[0].number == 2

    def test_get_ready_issues_priority_sorting(self) -> None:
        """Test that ready issues are sorted by priority."""
        resolver = DependencyResolver()

        resolver.add_issue(IssueInfo(number=1, title="Low", priority=1))
        resolver.add_issue(IssueInfo(number=2, title="High", priority=10))
        resolver.add_issue(IssueInfo(number=3, title="Medium", priority=5))

        ready = resolver.get_ready_issues()

        # Should be sorted by priority descending
        assert [i.number for i in ready] == [2, 3, 1]

    def test_topological_sort_linear(self) -> None:
        """Test topological sort with linear dependencies."""
        resolver = DependencyResolver()

        # Chain: 3 -> 2 -> 1
        resolver.add_issue(IssueInfo(number=1, title="Base"))
        resolver.add_issue(IssueInfo(number=2, title="Middle", dependencies=[1]))
        resolver.add_issue(IssueInfo(number=3, title="Top", dependencies=[2]))

        order = resolver.topological_sort()

        # Should be in dependency order
        assert order.index(1) < order.index(2)
        assert order.index(2) < order.index(3)

    def test_topological_sort_diamond(self) -> None:
        """Test topological sort with diamond pattern."""
        resolver = DependencyResolver()

        # Diamond: 4 -> {2, 3} -> 1
        resolver.add_issue(IssueInfo(number=1, title="Base"))
        resolver.add_issue(IssueInfo(number=2, title="Left", dependencies=[1]))
        resolver.add_issue(IssueInfo(number=3, title="Right", dependencies=[1]))
        resolver.add_issue(IssueInfo(number=4, title="Top", dependencies=[2, 3]))

        order = resolver.topological_sort()

        # 1 must come before 2 and 3
        assert order.index(1) < order.index(2)
        assert order.index(1) < order.index(3)
        # 2 and 3 must come before 4
        assert order.index(2) < order.index(4)
        assert order.index(3) < order.index(4)

    def test_topological_sort_with_cycle(self) -> None:
        """Test topological sort fails with cycle."""
        resolver = DependencyResolver()

        # Cycle: 1 -> 2 -> 1
        resolver.add_issue(IssueInfo(number=1, title="A", dependencies=[2]))
        resolver.add_issue(IssueInfo(number=2, title="B", dependencies=[1]))

        with pytest.raises(CyclicDependencyError):
            resolver.topological_sort()

    def test_get_stats(self) -> None:
        """Test getting resolver statistics."""
        resolver = DependencyResolver()

        resolver.add_issue(IssueInfo(number=1, title="A"))
        resolver.add_issue(IssueInfo(number=2, title="B", dependencies=[1]))
        resolver.add_issue(IssueInfo(number=3, title="C", dependencies=[2]))

        resolver.mark_completed(1)

        stats = resolver.get_stats()

        assert stats["total_issues"] == 3
        assert stats["completed_issues"] == 1
        assert stats["remaining_issues"] == 2
        assert stats["ready_issues"] == 1  # Issue 2 is now ready

    def test_mark_completed(self) -> None:
        """Test marking issues as completed."""
        resolver = DependencyResolver()

        resolver.add_issue(IssueInfo(number=1, title="A"))
        resolver.mark_completed(1)

        assert 1 in resolver.completed

        # Should not appear in ready issues
        ready = resolver.get_ready_issues()
        assert 1 not in [i.number for i in ready]


class TestLoadDependenciesIterative:
    """Tests for the iterative BFS _load_dependencies (A5-03)."""

    def test_visited_set_prevents_revisiting(self) -> None:
        """A dependency already in the graph is not loaded twice (A5-03)."""
        from unittest.mock import patch

        resolver = DependencyResolver(skip_closed=False)
        # Pre-populate issue 1 so the BFS should skip re-fetching it.
        resolver.add_issue(IssueInfo(number=1, title="Already loaded"))
        issue_with_dep = IssueInfo(number=2, title="Has dep", dependencies=[1])

        with patch("hephaestus.automation.dependency_resolver.fetch_issue_info") as mock_fetch:
            resolver._load_dependencies(issue_with_dep, {})

        # fetch_issue_info must NOT have been called for dep 1 (already in graph)
        mock_fetch.assert_not_called()

    def test_merged_pr_dependency_is_skipped(self) -> None:
        """A dependency that is a merged PR (state MERGED) is treated as done.

        Regression: before MERGED was a valid IssueState, such a dependency was
        fetched and crashed with ``'MERGED' is not a valid IssueState``. With
        skip_closed=True it should be marked complete and never fetched.
        """
        from unittest.mock import patch

        from hephaestus.automation.models import IssueState

        resolver = DependencyResolver(skip_closed=True)
        root = IssueInfo(number=10, title="Root", dependencies=[9])

        with patch("hephaestus.automation.dependency_resolver.fetch_issue_info") as mock_fetch:
            resolver._load_dependencies(root, {9: IssueState.MERGED})

        mock_fetch.assert_not_called()
        assert 9 in resolver.completed
        assert 9 not in resolver.graph.issues

    def test_depth_cap_raises_runtime_error(self) -> None:
        """Exceeding _MAX_DEPENDENCY_DEPTH raises RuntimeError (A5-03)."""
        from unittest.mock import patch

        resolver = DependencyResolver(skip_closed=False)
        root = IssueInfo(number=0, title="Root", dependencies=[1])

        # Each fetched issue depends on the next, creating a chain of depth > 100.
        def _make_issue(n: int) -> IssueInfo:
            return IssueInfo(number=n, title=f"Issue {n}", dependencies=[n + 1])

        with patch(
            "hephaestus.automation.dependency_resolver.fetch_issue_info",
            side_effect=lambda n: _make_issue(n),
        ):
            with pytest.raises(RuntimeError, match="exceeded"):
                resolver._load_dependencies(root, {})

    def test_normal_chain_loads_all(self) -> None:
        """A finite chain (depth < limit) loads all dependencies without error (A5-03)."""
        from unittest.mock import patch

        resolver = DependencyResolver(skip_closed=False)
        root = IssueInfo(number=10, title="Root", dependencies=[9])

        # 9 -> 8 -> 7 (depth 3, well within limit)
        dep_map = {
            9: IssueInfo(number=9, title="D9", dependencies=[8]),
            8: IssueInfo(number=8, title="D8", dependencies=[7]),
            7: IssueInfo(number=7, title="D7", dependencies=[]),
        }

        with patch(
            "hephaestus.automation.dependency_resolver.fetch_issue_info",
            side_effect=lambda n: dep_map[n],
        ):
            resolver._load_dependencies(root, {})

        # All three deps should now be in the graph
        assert 9 in resolver.graph.issues
        assert 8 in resolver.graph.issues
        assert 7 in resolver.graph.issues
