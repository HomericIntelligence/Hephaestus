"""File-overlap serialization and admission control for the implementation queue.

Tests the within-round file-overlap guard that defers issues whose planned file
sets intersect an in-flight peer's, and the filtered-open-issues helper.
"""

from __future__ import annotations

from unittest.mock import patch

from hephaestus.automation.pipeline.admission import (
    _filter_open_issues,
    _parse_planned_files,
    _select_non_overlapping,
)


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
            side_effect=lambda i: plans[i],
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
            side_effect=lambda i: plans[i],
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
            side_effect=lambda i: plans[i],
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
            side_effect=lambda i: plans[i],
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
            side_effect=lambda i: plans[i],
        ):
            dispatch, defer = _select_non_overlapping([1, 2, 3])
        # #1 claims file1.py; #2 overlaps file1.py → defer; #3 claims file2.py → dispatch.
        assert dispatch == [1, 3]
        assert defer == [2]


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
