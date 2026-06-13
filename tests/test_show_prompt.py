"""Tests for the show-prompt CLI (issue #1170)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the repo root is on sys.path so scripts.show_prompt resolves
# without requiring scripts/__init__.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.show_prompt import (
    _PLAN_MARKERS,
    STAGES,
    _extract_plan_from_issue_data,
    build_parser,
    build_prompt,
    fetch_pr_diff,
    fetch_pr_threads,
    main,
)

# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestBuildParser:
    """Tests for the argument parser."""

    def test_requires_issue(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--stage", "planning"])

    def test_requires_stage(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--issue", "1"])

    def test_invalid_stage_rejected(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--issue", "1", "--stage", "bogus"])

    def test_valid_args_parsed(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--issue", "42", "--stage", "planning"])
        assert args.issue == 42
        assert args.stage == "planning"
        assert args.repo == "HomericIntelligence/ProjectHephaestus"

    def test_optional_args(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--issue",
                "10",
                "--stage",
                "implementation",
                "--branch",
                "feature/foo",
                "--worktree",
                "/tmp/wt",
                "--pr",
                "5",
                "--iteration",
                "2",
            ]
        )
        assert args.branch == "feature/foo"
        assert args.worktree == "/tmp/wt"
        assert args.pr == 5
        assert args.iteration == 2


# ---------------------------------------------------------------------------
# build_prompt tests
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    """Tests for build_prompt across all stages."""

    def test_planning_stage(self) -> None:
        """Planning stage does not fetch issue data."""
        with patch("scripts.show_prompt.fetch_issue") as mock_issue:
            prompt = build_prompt("planning", 1, "owner/repo")
            mock_issue.assert_not_called()
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    @patch("scripts.show_prompt._extract_plan_from_issue_data")
    def test_plan_review_stage(self, mock_extract: MagicMock, mock_issue: MagicMock) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        mock_extract.return_value = "# Plan"
        prompt = build_prompt("plan-review", 1, "owner/repo")
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    @patch("scripts.show_prompt._extract_plan_from_issue_data")
    def test_plan_loop_review_stage(self, mock_extract: MagicMock, mock_issue: MagicMock) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        mock_extract.return_value = "# Plan"
        prompt = build_prompt("plan-loop-review", 1, "owner/repo", iteration=1)
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    def test_implementation_stage(self, mock_issue: MagicMock) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        prompt = build_prompt(
            "implementation",
            1,
            "owner/repo",
            branch_name="feat/x",
            worktree_path="/tmp/wt",
        )
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    @patch("scripts.show_prompt.fetch_pr_diff")
    def test_impl_review_stage(self, mock_diff: MagicMock, mock_issue: MagicMock) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        mock_diff.return_value = "diff --git a/foo.py b/foo.py"
        prompt = build_prompt(
            "impl-review",
            1,
            "owner/repo",
            pr_number=5,
            iteration=0,
        )
        mock_diff.assert_called_once_with("owner/repo", 5)
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    def test_impl_review_no_pr_skips_diff(self, mock_issue: MagicMock) -> None:
        """When pr_number is 0, fetch_pr_diff is not called."""
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        with patch("scripts.show_prompt.fetch_pr_diff") as mock_diff:
            prompt = build_prompt("impl-review", 1, "owner/repo", iteration=0)
            mock_diff.assert_not_called()
        assert isinstance(prompt, str)

    @patch("scripts.show_prompt.fetch_issue")
    @patch("scripts.show_prompt.fetch_pr_diff")
    def test_impl_review_diff_failure_graceful(
        self, mock_diff: MagicMock, mock_issue: MagicMock
    ) -> None:
        """When fetch_pr_diff raises, impl-review degrades to empty diff."""
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        mock_diff.side_effect = RuntimeError("gh failed")
        prompt = build_prompt(
            "impl-review",
            1,
            "owner/repo",
            pr_number=5,
            iteration=0,
        )
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    def test_impl_resume_stage(self, mock_issue: MagicMock) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        prompt = build_prompt("impl-resume", 1, "owner/repo", iteration=1)
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    @patch("scripts.show_prompt.fetch_pr_diff")
    def test_pr_review_stage(self, mock_diff: MagicMock, mock_issue: MagicMock) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        mock_diff.return_value = "diff --git a/foo.py b/foo.py"
        prompt = build_prompt("pr-review", 1, "owner/repo", pr_number=5)
        mock_diff.assert_called_once_with("owner/repo", 5)
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    @patch("scripts.show_prompt.fetch_pr_diff")
    def test_pr_review_diff_failure_graceful(
        self, mock_diff: MagicMock, mock_issue: MagicMock
    ) -> None:
        """When fetch_pr_diff raises, pr-review degrades to empty diff."""
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        mock_diff.side_effect = RuntimeError("gh failed")
        prompt = build_prompt("pr-review", 1, "owner/repo", pr_number=5)
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    @patch("scripts.show_prompt.fetch_pr_threads")
    def test_address_review_stage(self, mock_threads: MagicMock, mock_issue: MagicMock) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        mock_threads.return_value = "[]"
        prompt = build_prompt("address-review", 1, "owner/repo", pr_number=5)
        mock_threads.assert_called_once_with("owner/repo", 5)
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    def test_address_review_no_pr_skips_threads(self, mock_issue: MagicMock) -> None:
        """When pr_number is 0, fetch_pr_threads is not called."""
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        with patch("scripts.show_prompt.fetch_pr_threads") as mock_threads:
            prompt = build_prompt("address-review", 1, "owner/repo")
            mock_threads.assert_not_called()
        assert isinstance(prompt, str)

    @patch("scripts.show_prompt.fetch_issue")
    def test_follow_up_stage(self, mock_issue: MagicMock) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        prompt = build_prompt("follow-up", 1, "owner/repo")
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    def test_advise_stage(self, mock_issue: MagicMock) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        prompt = build_prompt("advise", 1, "owner/repo")
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_unknown_stage_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown stage"):
            build_prompt("bogus", 1, "owner/repo")

    @patch("scripts.show_prompt.fetch_issue")
    @patch("scripts.show_prompt._extract_plan_from_issue_data")
    def test_plan_review_falls_back_to_body_when_no_plan_comment(
        self, mock_extract: MagicMock, mock_issue: MagicMock
    ) -> None:
        """When no plan comment is found, the issue body is used as plan text."""
        mock_issue.return_value = {"title": "T", "body": "The issue body", "comments": []}
        mock_extract.return_value = None
        with patch("hephaestus.automation.prompts.planning.get_plan_review_prompt") as mock_prompt:
            mock_prompt.return_value = "prompt"
            build_prompt("plan-review", 1, "owner/repo")
            mock_prompt.assert_called_once()
            assert mock_prompt.call_args.kwargs["plan_text"] == "The issue body"

    @patch("scripts.show_prompt.fetch_issue")
    @patch("scripts.show_prompt._extract_plan_from_issue_data")
    def test_plan_review_falls_back_to_title_when_no_body(
        self, mock_extract: MagicMock, mock_issue: MagicMock
    ) -> None:
        """When no plan comment and no body, the issue title is used as plan text."""
        mock_issue.return_value = {"title": "The issue title", "body": "", "comments": []}
        mock_extract.return_value = None
        with patch("hephaestus.automation.prompts.planning.get_plan_review_prompt") as mock_prompt:
            mock_prompt.return_value = "prompt"
            build_prompt("plan-review", 1, "owner/repo")
            mock_prompt.assert_called_once()
            assert mock_prompt.call_args.kwargs["plan_text"] == "The issue title"

    @patch("scripts.show_prompt.fetch_issue")
    @patch("scripts.show_prompt._extract_plan_from_issue_data")
    def test_plan_review_uses_plan_comment_over_body(
        self, mock_extract: MagicMock, mock_issue: MagicMock
    ) -> None:
        """A plan comment takes precedence over the issue body."""
        mock_issue.return_value = {"title": "T", "body": "The issue body", "comments": []}
        mock_extract.return_value = "# Implementation Plan\nSteps"
        with patch("hephaestus.automation.prompts.planning.get_plan_review_prompt") as mock_prompt:
            mock_prompt.return_value = "prompt"
            build_prompt("plan-review", 1, "owner/repo")
            assert mock_prompt.call_args.kwargs["plan_text"] == "# Implementation Plan\nSteps"

    @patch("scripts.show_prompt.fetch_issue")
    @patch("scripts.show_prompt._extract_plan_from_issue_data")
    def test_plan_loop_review_falls_back_to_body_then_title(
        self, mock_extract: MagicMock, mock_issue: MagicMock
    ) -> None:
        """plan-loop-review uses the same fallback chain: plan > body > title."""
        mock_issue.return_value = {"title": "My Title", "body": "", "comments": []}
        mock_extract.return_value = None
        with patch(
            "hephaestus.automation.prompts.planning.get_plan_loop_review_prompt"
        ) as mock_prompt:
            mock_prompt.return_value = "prompt"
            build_prompt("plan-loop-review", 1, "owner/repo", iteration=0)
            assert mock_prompt.call_args.kwargs["plan_text"] == "My Title"

    @patch("scripts.show_prompt.fetch_issue")
    @patch("scripts.show_prompt._extract_plan_from_issue_data")
    def test_plan_review_falls_back_to_placeholder_when_all_empty(
        self, mock_extract: MagicMock, mock_issue: MagicMock
    ) -> None:
        """When all sources are empty, the placeholder is used."""
        mock_issue.return_value = {"title": "", "body": "", "comments": []}
        mock_extract.return_value = None
        with patch("hephaestus.automation.prompts.planning.get_plan_review_prompt") as mock_prompt:
            mock_prompt.return_value = "prompt"
            build_prompt("plan-review", 1, "owner/repo")
            assert mock_prompt.call_args.kwargs["plan_text"] == "(no plan found)"

    @patch("scripts.show_prompt.fetch_issue")
    @patch("scripts.show_prompt._extract_plan_from_issue_data")
    def test_plan_loop_review_falls_back_to_placeholder_when_all_empty(
        self, mock_extract: MagicMock, mock_issue: MagicMock
    ) -> None:
        """When all sources are empty, plan-loop-review uses the placeholder."""
        mock_issue.return_value = {"title": "", "body": "", "comments": []}
        mock_extract.return_value = None
        with patch(
            "hephaestus.automation.prompts.planning.get_plan_loop_review_prompt"
        ) as mock_prompt:
            mock_prompt.return_value = "prompt"
            build_prompt("plan-loop-review", 1, "owner/repo", iteration=0)
            assert mock_prompt.call_args.kwargs["plan_text"] == "(no plan found)"


# ---------------------------------------------------------------------------
# main() integration tests
# ---------------------------------------------------------------------------


class TestMain:
    """Integration tests for the main entry point."""

    @patch("scripts.show_prompt.build_prompt")
    def test_main_prints_prompt(
        self, mock_build: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_build.return_value = "THE PROMPT TEXT"
        rc = main(["--issue", "1", "--stage", "planning"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "THE PROMPT TEXT" in captured.out

    @patch("scripts.show_prompt.build_prompt")
    def test_main_returns_1_on_error(self, mock_build: MagicMock) -> None:
        mock_build.side_effect = RuntimeError("gh failed")
        rc = main(["--issue", "1", "--stage", "planning"])
        assert rc == 1


# ---------------------------------------------------------------------------
# Coverage: all stages are listed
# ---------------------------------------------------------------------------


def test_all_stages_covered() -> None:
    """Verify STAGES tuple covers all expected pipeline stages."""
    expected = {
        "planning",
        "plan-review",
        "plan-loop-review",
        "implementation",
        "impl-review",
        "impl-resume",
        "pr-review",
        "address-review",
        "follow-up",
        "advise",
    }
    assert set(STAGES) == expected


# ---------------------------------------------------------------------------
# Direct unit tests for helper functions (M5)
# ---------------------------------------------------------------------------


class TestExtractPlanFromIssueData:
    """Direct tests for _extract_plan_from_issue_data."""

    def test_returns_none_for_none_input(self) -> None:
        assert _extract_plan_from_issue_data(None) is None

    def test_returns_none_when_no_comments(self) -> None:
        assert _extract_plan_from_issue_data({"comments": []}) is None

    def test_returns_none_when_no_plan_marker(self) -> None:
        data = {"comments": [{"body": "random comment"}]}
        assert _extract_plan_from_issue_data(data) is None

    def test_finds_implementation_plan(self) -> None:
        plan = "# Implementation Plan\n\n## Steps\n1. Do stuff"
        data = {"comments": [{"body": plan}]}
        assert _extract_plan_from_issue_data(data) == plan

    def test_finds_approach_marker(self) -> None:
        plan = "## Approach\nWe will refactor X."
        data = {"comments": [{"body": plan}]}
        assert _extract_plan_from_issue_data(data) == plan

    def test_finds_proposed_solution(self) -> None:
        plan = "## Proposed Solution\nAdd Y."
        data = {"comments": [{"body": plan}]}
        assert _extract_plan_from_issue_data(data) == plan

    def test_finds_design_marker(self) -> None:
        plan = "## Design\nHigh-level architecture."
        data = {"comments": [{"body": plan}]}
        assert _extract_plan_from_issue_data(data) == plan

    def test_finds_h3_approach(self) -> None:
        plan = "### Approach\nDetailed steps."
        data = {"comments": [{"body": plan}]}
        assert _extract_plan_from_issue_data(data) == plan

    def test_returns_most_recent_plan(self) -> None:
        old_plan = "# Implementation Plan\nOld version"
        new_plan = "# Implementation Plan\nNew version"
        data = {
            "comments": [
                {"body": old_plan},
                {"body": "noise"},
                {"body": new_plan},
            ]
        }
        assert _extract_plan_from_issue_data(data) == new_plan

    def test_plan_markers_tuple_entries(self) -> None:
        """Verify _PLAN_MARKERS has the expected entries."""
        expected = {
            "# Implementation Plan",
            "## Implementation Plan",
            "## Approach",
            "### Approach",
            "## Proposed Solution",
            "## Design",
        }
        assert set(_PLAN_MARKERS) == expected


class TestFetchPrThreads:
    """Direct tests for fetch_pr_threads."""

    @patch("scripts.show_prompt._gh")
    def test_returns_json_string(self, mock_gh: MagicMock) -> None:
        mock_gh.return_value = {"reviewThreads": [], "body": ""}
        result = fetch_pr_threads("owner/repo", 5)
        mock_gh.assert_called_once_with(
            ["pr", "view", "5", "--repo", "owner/repo", "--json", "reviewThreads,body"],
            parse_json=True,
        )
        parsed = json.loads(result)
        assert parsed == {"reviewThreads": [], "body": ""}

    @patch("scripts.show_prompt._gh")
    def test_returns_empty_list_on_runtime_error(self, mock_gh: MagicMock) -> None:
        mock_gh.side_effect = RuntimeError("gh failed")
        result = fetch_pr_threads("owner/repo", 5)
        assert result == "[]"


class TestFetchPrDiff:
    """Direct tests for fetch_pr_diff."""

    @patch("scripts.show_prompt._gh")
    def test_calls_gh_with_correct_args(self, mock_gh: MagicMock) -> None:
        mock_gh.return_value = "diff content"
        result = fetch_pr_diff("owner/repo", 10)
        mock_gh.assert_called_once_with(["pr", "diff", "10", "--repo", "owner/repo"])
        assert result == "diff content"

    @patch("scripts.show_prompt._gh")
    def test_raises_runtime_error_on_failure(self, mock_gh: MagicMock) -> None:
        mock_gh.side_effect = RuntimeError("gh failed")
        with pytest.raises(RuntimeError, match="gh failed"):
            fetch_pr_diff("owner/repo", 10)
