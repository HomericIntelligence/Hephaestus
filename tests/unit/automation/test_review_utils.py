"""Tests for shared review utilities (_review_utils.py)."""

import json
from typing import Any
from unittest.mock import MagicMock, patch

from hephaestus.automation._review_utils import find_pr_for_issue, parse_json_block

# ---------------------------------------------------------------------------
# parse_json_block
# ---------------------------------------------------------------------------


class TestParseJsonBlock:
    """Tests for the shared parse_json_block helper."""

    def test_extracts_last_json_block(self) -> None:
        """Multiple ```json blocks → returns the last one parsed."""
        text = (
            '```json\n{"comments": ["first"], "summary": "first"}\n```\n'
            '```json\n{"comments": ["second"], "summary": "second"}\n```'
        )
        result = parse_json_block(text)
        assert result["summary"] == "second"

    def test_no_block_returns_defaults(self) -> None:
        """No json block → returns defaults with empty comments list."""
        result = parse_json_block("No json here.")
        assert result["comments"] == []
        assert "No structured output" in result["summary"]

    def test_invalid_json_returns_defaults(self) -> None:
        """Malformed json → returns defaults."""
        text = "```json\n{invalid!!}\n```"
        result = parse_json_block(text)
        assert result["comments"] == []
        assert "Failed to parse" in result["summary"]

    def test_valid_block(self) -> None:
        """Single valid block → parsed correctly."""
        payload = {"comments": [{"path": "a.py", "line": 1, "body": "x"}], "summary": "ok"}
        text = "```json\n" + json.dumps(payload) + "\n```"
        result = parse_json_block(text)
        assert result["summary"] == "ok"
        assert len(result["comments"]) == 1


# ---------------------------------------------------------------------------
# find_pr_for_issue
# ---------------------------------------------------------------------------


def _make_gh_result(payload: Any) -> MagicMock:
    mock = MagicMock()
    mock.stdout = json.dumps(payload)
    return mock


class TestFindPrForIssue:
    """Tests for find_pr_for_issue helper."""

    def test_finds_via_branch_name(self) -> None:
        """Branch-name lookup succeeds → returns PR number immediately."""
        with patch(
            "hephaestus.automation._review_utils._gh_call",
            return_value=_make_gh_result([{"number": 42}]),
        ):
            result = find_pr_for_issue(123)

        assert result == 42

    def test_falls_back_to_body_search(self) -> None:
        """Branch-name returns empty → falls back to body search."""
        call_count = 0

        def _side_effect(args: list[str], **kw: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if "--head" in args:
                return _make_gh_result([])
            # body search
            return _make_gh_result([{"number": 99}])

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=_side_effect,
        ):
            result = find_pr_for_issue(123)

        assert result == 99
        assert call_count == 2

    def test_returns_none_when_nothing_found(self) -> None:
        """All strategies return empty → None."""
        with patch(
            "hephaestus.automation._review_utils._gh_call",
            return_value=_make_gh_result([]),
        ):
            result = find_pr_for_issue(123)

        assert result is None

    def test_extra_strategies_uses_review_state(self) -> None:
        """extra_strategies=True checks review state when branch-name fails."""
        # Branch-name returns empty; review-state lookup succeeds
        call_results = [
            _make_gh_result([]),  # branch-name: no match
            _make_gh_result({"state": "OPEN", "number": 55}),  # gh pr view
        ]
        call_iter = iter(call_results)

        review_state = MagicMock()
        review_state.pr_number = 55

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=lambda *a, **kw: next(call_iter),
        ):
            result = find_pr_for_issue(
                123,
                extra_strategies=True,
                _load_review_state_fn=lambda: review_state,
            )

        assert result == 55

    def test_extra_strategies_skips_closed_pr(self) -> None:
        """extra_strategies=True: review-state PR is closed → fall through to body search."""
        call_results = [
            _make_gh_result([]),  # branch-name: empty
            _make_gh_result({"state": "CLOSED", "number": 55}),  # gh pr view: closed
            _make_gh_result([{"number": 77}]),  # body search: match
        ]
        call_iter = iter(call_results)

        review_state = MagicMock()
        review_state.pr_number = 55

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=lambda *a, **kw: next(call_iter),
        ):
            result = find_pr_for_issue(
                123,
                extra_strategies=True,
                _load_review_state_fn=lambda: review_state,
            )

        assert result == 77

    def test_branch_name_gh_error_falls_back(self) -> None:
        """Branch-name lookup raises → falls back gracefully."""
        import subprocess

        def _side_effect(args: list[str], **kw: Any) -> MagicMock:
            if "--head" in args:
                raise subprocess.CalledProcessError(1, "gh")
            return _make_gh_result([{"number": 10}])

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=_side_effect,
        ):
            result = find_pr_for_issue(123)

        assert result == 10
