"""Regression tests for implementer dependency expansion."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from hephaestus.automation.implementer import IssueImplementer
from hephaestus.automation.models import ImplementerOptions, IssueInfo, IssueState


def _implementer(tmp_path: Path, *, skip_closed: bool = True) -> IssueImplementer:
    """Build an IssueImplementer rooted at ``tmp_path`` for dependency tests."""
    with (
        patch("hephaestus.automation.implementer.get_repo_root", return_value=tmp_path),
        patch("hephaestus.automation.worktree_manager.get_repo_root", return_value=tmp_path),
    ):
        return IssueImplementer(ImplementerOptions(issues=[1819], skip_closed=skip_closed))


def _load_root_with_dependency(implementer: IssueImplementer, dependency: IssueInfo) -> None:
    """Load a synthetic root issue whose only dependency is ``dependency``."""
    root = IssueInfo(number=1819, title="Target", dependencies=[dependency.number])

    with (
        patch(
            "hephaestus.automation.github_api.prefetch_issue_states",
            return_value={1819: IssueState.OPEN},
        ),
        patch("hephaestus.automation.implementer.fetch_issue_info", return_value=root),
        patch(
            "hephaestus.automation.dependency_resolver.fetch_issue_info",
            return_value=dependency,
        ),
    ):
        implementer._load_issues([1819])


def test_load_issues_marks_closed_dependency_complete(tmp_path: Path) -> None:
    """Closed dependencies are marked complete and never added to the graph."""
    implementer = _implementer(tmp_path)
    dependency = IssueInfo(number=1818, title="Closed dependency", state=IssueState.CLOSED)

    _load_root_with_dependency(implementer, dependency)

    assert 1819 in implementer.resolver.graph.issues
    assert 1818 in implementer.resolver.completed
    assert 1818 not in implementer.resolver.graph.issues


def test_load_issues_marks_state_skip_dependency_complete(tmp_path: Path) -> None:
    """state:skip dependencies are treated as completed work, not graph nodes."""
    implementer = _implementer(tmp_path)
    dependency = IssueInfo(number=1817, title="Skipped dependency", labels=["state:skip"])

    _load_root_with_dependency(implementer, dependency)

    assert 1817 in implementer.resolver.completed
    assert 1817 not in implementer.resolver.graph.issues


def test_no_skip_closed_keeps_closed_dependency_in_graph(tmp_path: Path) -> None:
    """--no-skip-closed preserves the legacy behavior for closed dependencies."""
    implementer = _implementer(tmp_path, skip_closed=False)
    dependency = IssueInfo(number=1818, title="Closed dependency", state=IssueState.CLOSED)

    _load_root_with_dependency(implementer, dependency)

    assert 1818 not in implementer.resolver.completed
    assert 1818 in implementer.resolver.graph.issues
