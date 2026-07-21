"""File-overlap serialization and admission control for the implementation queue.

Tests the within-round file-overlap guard that defers issues whose planned file
sets intersect an in-flight peer's, the topological-order gating for the
implementation queue, and the filtered-open-issues helper.
"""

from __future__ import annotations

import logging
from typing import ClassVar
from unittest.mock import patch

import pytest

from hephaestus.automation.models import IssueInfo
from hephaestus.automation.pipeline import admission
from hephaestus.automation.pipeline.admission import (
    _filter_open_issues,
    _parse_planned_files,
    _select_non_overlapping,
    order_for_implementation,
)


def _info(number: int, dependencies: list[int] | None = None) -> IssueInfo:
    """Build a minimal IssueInfo for dependency-ordering tests."""
    return IssueInfo(number=number, title=f"Issue {number}", dependencies=dependencies or [])


class TestParsePlannedFiles:
    """Plan-body parser: extract repo-relative paths from Files sections."""

    def test_parse_planned_files_modify_section(self) -> None:
        """A ``## Files to Modify`` body yields its backticked in-tree paths."""
        body = (
            "# Implementation Plan\n\n"
            "## Files to Modify\n\n"
            "### `hephaestus/automation/address_review.py`\n"
            "Do a thing.\n"
            "- `hephaestus/automation/ci_driver.py`\n"
        )
        assert _parse_planned_files(body) == {
            "hephaestus/automation/address_review.py",
            "hephaestus/automation/ci_driver.py",
        }

    def test_parse_planned_files_create_section(self) -> None:
        """A ``## Files to Create`` body is scanned too (both headings)."""
        body = (
            "# Implementation Plan\n\n## Files to Create\n\n"
            "### `tests/unit/automation/test_new.py`\n"
        )
        assert _parse_planned_files(body) == {"tests/unit/automation/test_new.py"}

    def test_parse_planned_files_no_section_returns_empty(self) -> None:
        """A plan with neither Files heading yields an empty set."""
        body = "# Implementation Plan\n\n## Objective\n\nJust do `x/y.py` inline."
        assert _parse_planned_files(body) == set()

    def test_parse_planned_files_stops_at_next_heading(self) -> None:
        """Backticked paths after the section's closing ``## `` heading are ignored."""
        body = (
            "# Implementation Plan\n\n"
            "## Files to Modify\n\n"
            "- `hephaestus/automation/ci_driver.py`\n\n"
            "## Verification\n\n"
            "- `hephaestus/automation/should_not_count.py`\n"
        )
        assert _parse_planned_files(body) == {"hephaestus/automation/ci_driver.py"}

    def test_parse_planned_files_bare_filenames_not_captured(self) -> None:
        """Bare filenames without directory (e.g., `pyproject.toml`) are NOT captured."""
        body = "# Implementation Plan\n\n## Files to Modify\n\n- `pyproject.toml`\n"
        assert _parse_planned_files(body) == set()

    def test_parse_planned_files_case_insensitive_heading(self) -> None:
        """## Files to Modify/Create headings are case-insensitive."""
        body = "# Implementation Plan\n\n## FILES TO MODIFY\n\n- `hephaestus/automation/test.py`\n"
        assert _parse_planned_files(body) == {"hephaestus/automation/test.py"}


class TestCoordinatorCapOwnership:
    """Traceability guard for the deferred per-repo cap."""

    def test_admission_docstring_cross_references_coordinator_admit(self) -> None:
        """The deferred cap is intentionally owned by Coordinator._admit."""
        assert ":meth:`~hephaestus.automation.pipeline.coordinator.Coordinator._admit`" in (
            admission.__doc__ or ""
        )


class TestFetchPlannedFiles:
    """Fetch plan file set from issue comments: fail-open on missing/unparseable."""

    def test_fetch_planned_files_no_plan_comment_returns_none(self) -> None:
        """Comments present but none is a plan comment → None (fail-open)."""
        comments = [{"body": "just a chat comment"}, {"body": "## 🔍 Plan Review"}]
        with patch(
            "hephaestus.automation.pipeline.admission._fetch_issue_comment_ids",
            return_value=comments,
        ):
            from hephaestus.automation.pipeline.admission import _fetch_planned_files

            assert _fetch_planned_files(101) is None

    def test_fetch_planned_files_empty_comment_list_returns_none(self) -> None:
        """An empty fetch (the swallowed-error signal) → None; no try/except needed."""
        with patch(
            "hephaestus.automation.pipeline.admission._fetch_issue_comment_ids",
            return_value=[],
        ):
            from hephaestus.automation.pipeline.admission import _fetch_planned_files

            assert _fetch_planned_files(102) is None

    def test_fetch_planned_files_returns_plan_file_set(self) -> None:
        """A real plan comment yields its parsed file set."""
        comments = [
            {"body": "chatter"},
            {
                "body": (
                    "# Implementation Plan\n\n## Files to Modify\n\n"
                    "- `hephaestus/automation/address_review.py`\n"
                )
            },
        ]
        with patch(
            "hephaestus.automation.pipeline.admission._fetch_issue_comment_ids",
            return_value=comments,
        ):
            from hephaestus.automation.pipeline.admission import _fetch_planned_files

            assert _fetch_planned_files(103) == {"hephaestus/automation/address_review.py"}


class TestAdmissionRepoScoping:
    """Admission must look up plans in the OWNING repo, not the ambient CWD (#1795)."""

    def test_fetch_planned_files_forwards_repo(self) -> None:
        """``_fetch_planned_files`` threads ``repo`` down to the comment fetch."""
        with patch(
            "hephaestus.automation.pipeline.admission._fetch_issue_comment_ids",
            return_value=[],
        ) as mock_fetch:
            from hephaestus.automation.pipeline.admission import _fetch_planned_files

            _fetch_planned_files(188, repo=("HomericIntelligence", "Myrmidons"))

        mock_fetch.assert_called_once_with(188, repo=("HomericIntelligence", "Myrmidons"))

    def test_select_non_overlapping_resolves_repo_per_issue(self) -> None:
        """Each issue is looked up in ITS OWN repo.

        The implementation queue is global (keyed by stage, not repo), so a
        single round can hold issues from different repositories. A batch-wide
        repo would send some lookups to the wrong repo — the very bug in #1795.
        """
        repo_of = {
            188: ("HomericIntelligence", "Myrmidons"),
            121: ("HomericIntelligence", "Nestor"),
        }
        with patch(
            "hephaestus.automation.pipeline.admission._fetch_issue_comment_ids",
            return_value=[],
        ) as mock_fetch:
            dispatch, defer = _select_non_overlapping([188, 121], repo_of=repo_of)

        assert dispatch == [188, 121]
        assert defer == []
        seen = {call.args[0]: call.kwargs["repo"] for call in mock_fetch.call_args_list}
        assert seen == repo_of

    def test_select_non_overlapping_missing_repo_entry_is_ambient(self) -> None:
        """Back-compat: an issue absent from ``repo_of`` forwards None (ambient)."""
        with patch(
            "hephaestus.automation.pipeline.admission._fetch_issue_comment_ids",
            return_value=[],
        ) as mock_fetch:
            _select_non_overlapping([7])

        assert mock_fetch.call_args.kwargs["repo"] is None


class TestSelectNonOverlapping:
    """Greedy first-fit partitioning: defer issues with overlapping file sets."""

    def test_select_non_overlapping_defers_second_of_overlapping_pair(self) -> None:
        """AC1/AC2: two issues both listing address_review.py → first runs, second defers."""
        plans = {
            1: {"hephaestus/automation/address_review.py", "hephaestus/automation/a.py"},
            2: {"hephaestus/automation/address_review.py", "hephaestus/automation/b.py"},
        }
        with patch(
            "hephaestus.automation.pipeline.admission._fetch_planned_files",
            side_effect=lambda i, repo=None: plans[i],
        ):
            dispatch, defer = _select_non_overlapping([1, 2])
        assert dispatch == [1]
        assert defer == [2]

    def test_select_non_overlapping_disjoint_both_dispatched(self) -> None:
        """Non-intersecting file sets → both dispatched, none deferred."""
        plans = {
            1: {"hephaestus/automation/a.py"},
            2: {"hephaestus/automation/b.py"},
        }
        with patch(
            "hephaestus.automation.pipeline.admission._fetch_planned_files",
            side_effect=lambda i, repo=None: plans[i],
        ):
            dispatch, defer = _select_non_overlapping([1, 2])
        assert dispatch == [1, 2]
        assert defer == []

    def test_select_non_overlapping_unknown_plan_fails_open(self) -> None:
        """An issue whose plan file set is None claims no files → always dispatched."""
        plans: dict[int, set[str] | None] = {
            1: {"hephaestus/automation/address_review.py"},
            2: None,  # no plan yet — fail open
            3: {"hephaestus/automation/address_review.py"},
        }
        with patch(
            "hephaestus.automation.pipeline.admission._fetch_planned_files",
            side_effect=lambda i, repo=None: plans[i],
        ):
            dispatch, defer = _select_non_overlapping([1, 2, 3])
        # #1 claims address_review.py; #2 unknown → dispatched; #3 overlaps #1 → deferred.
        assert dispatch == [1, 2]
        assert defer == [3]

    def test_select_non_overlapping_first_issue_always_dispatched(self) -> None:
        """Liveness: the first issue always dispatches, so a batch is never wholly deferred."""
        plans = {
            1: {"hephaestus/automation/address_review.py"},
            2: {"hephaestus/automation/address_review.py"},
        }
        with patch(
            "hephaestus.automation.pipeline.admission._fetch_planned_files",
            side_effect=lambda i, repo=None: plans[i],
        ):
            dispatch, defer = _select_non_overlapping([1, 2])
        assert dispatch[0] == 1
        assert defer == [2]

    def test_select_non_overlapping_three_way_chain(self) -> None:
        """Three issues: first claimed, second overlaps, third overlaps second but not first."""
        plans = {
            1: {"hephaestus/automation/file1.py"},
            2: {"hephaestus/automation/file1.py"},  # overlaps #1
            3: {"hephaestus/automation/file2.py"},  # overlaps #2 (both claim distinct files)
        }
        with patch(
            "hephaestus.automation.pipeline.admission._fetch_planned_files",
            side_effect=lambda i, repo=None: plans[i],
        ):
            dispatch, defer = _select_non_overlapping([1, 2, 3])
        # #1 claims file1.py; #2 overlaps file1.py → defer; #3 claims file2.py → dispatch.
        assert dispatch == [1, 3]
        assert defer == [2]


class TestOrderForImplementation:
    """Topological-order gating: dependencies dispatch before dependents."""

    def test_dependency_ordered_before_dependent(self) -> None:
        """#A depends on #B → B precedes A regardless of input order."""
        order = order_for_implementation([_info(10, dependencies=[20]), _info(20)])
        assert order.index(20) < order.index(10)

    def test_uses_public_dependency_api(self) -> None:
        """The admission layer routes dependency edges through the resolver API."""

        class FakeDependencyResolver:
            """Resolver double that fails if callers reach into ``graph`` directly."""

            instances: ClassVar[list[FakeDependencyResolver]] = []

            class Graph:
                def add_dependency(self, issue_number: int, depends_on: int) -> None:
                    pytest.fail(
                        "order_for_implementation must call DependencyResolver.add_dependency()"
                    )

            def __init__(self, skip_closed: bool = True) -> None:
                self.skip_closed = skip_closed
                self.graph = self.Graph()
                self.add_issue_calls: list[int] = []
                self.add_dependency_calls: list[tuple[int, int]] = []
                self.topological_sort_result = [20, 10]
                type(self).instances.append(self)

            def add_issue(self, issue: IssueInfo) -> None:
                self.add_issue_calls.append(issue.number)

            def add_dependency(self, issue_number: int, depends_on: int) -> None:
                self.add_dependency_calls.append((issue_number, depends_on))

            def topological_sort(self) -> list[int]:
                return self.topological_sort_result

        FakeDependencyResolver.instances = []

        with patch(
            "hephaestus.automation.pipeline.admission.DependencyResolver",
            FakeDependencyResolver,
        ):
            order = order_for_implementation([_info(10, dependencies=[20]), _info(20)])

        assert order == [20, 10]
        assert FakeDependencyResolver.instances[0].add_dependency_calls == [(10, 20)]

    def test_no_dependencies_preserves_input_order(self) -> None:
        """Independent issues keep their dispatch-priority (input) order."""
        assert order_for_implementation([_info(3), _info(1), _info(2)]) == [3, 1, 2]

    def test_out_of_set_dependency_ignored(self) -> None:
        """A dependency outside the implementation queue is dropped (fail-open)."""
        # #5 depends on #999 which is not admitted — #5 must still be ordered.
        order = order_for_implementation([_info(5, dependencies=[999]), _info(6)])
        assert sorted(order) == [5, 6]

    def test_chain_fully_ordered(self) -> None:
        """A → B → C chain sorts leaf-dependency first."""
        order = order_for_implementation(
            [_info(1, dependencies=[2]), _info(2, dependencies=[3]), _info(3)]
        )
        assert order == [3, 2, 1]

    def test_ready_dependent_reclaims_input_priority(self) -> None:
        """A newly ready high-priority dependent precedes lower-priority work."""
        order = order_for_implementation([_info(1, dependencies=[2]), _info(2), _info(3)])
        assert order == [2, 1, 3]

    def test_cycle_falls_open_to_input_order(self, caplog: pytest.LogCaptureFixture) -> None:
        """A dependency cycle keeps input order and warns (never wedges the queue)."""
        infos = [_info(1, dependencies=[2]), _info(2, dependencies=[1]), _info(3)]
        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.pipeline.admission"):
            order = order_for_implementation(infos)
        assert order == [1, 2, 3]
        assert any("dependency cycle" in record.message for record in caplog.records)


class TestFilterOpenIssues:
    """Filter closed issues from explicit --issues list (#1576)."""

    @patch("hephaestus.automation.pipeline.admission.prefetch_issue_states")
    @patch("hephaestus.automation.pipeline.admission.is_issue_closed")
    def test_filter_open_issues_keeps_open(self, mock_is_closed, mock_prefetch) -> None:
        """Open issues are kept."""
        mock_prefetch.return_value = {}
        mock_is_closed.return_value = False
        result = _filter_open_issues("repo", [1, 2, 3])
        assert result == [1, 2, 3]

    @patch("hephaestus.automation.pipeline.admission.prefetch_issue_states")
    @patch("hephaestus.automation.pipeline.admission.is_issue_closed")
    def test_filter_open_issues_excludes_closed(self, mock_is_closed, mock_prefetch) -> None:
        """Closed issues are excluded."""
        mock_prefetch.return_value = {}
        mock_is_closed.side_effect = lambda num, _: num == 2
        result = _filter_open_issues("repo", [1, 2, 3])
        assert result == [1, 3]

    @patch("hephaestus.automation.pipeline.admission.prefetch_issue_states")
    def test_filter_open_issues_fails_open_on_api_error(self, mock_prefetch) -> None:
        """Transient API failure → keep all, don't drop work (fail-open)."""
        mock_prefetch.side_effect = RuntimeError("API error")
        result = _filter_open_issues("repo", [1, 2, 3])
        assert result == [1, 2, 3]

    @patch("hephaestus.automation.pipeline.admission.prefetch_issue_states")
    @patch("hephaestus.automation.pipeline.admission.is_issue_closed")
    def test_filter_open_issues_preserves_order(self, mock_is_closed, mock_prefetch) -> None:
        """Excluded issues maintain the original order."""
        mock_prefetch.return_value = {}
        mock_is_closed.side_effect = lambda num, _: num in {2, 4}
        result = _filter_open_issues("repo", [1, 2, 3, 4, 5])
        assert result == [1, 3, 5]
