"""Tests for issue duplicate detection used by follow_up.py."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

from hephaestus.automation.issue_dedup import (
    IssueMatch,
    extract_new_info,
    find_duplicate_open_issue,
)


def _mock_gh(returned: list[dict[str, Any]]) -> MagicMock:
    """Build a mock for `_gh_call` returning a CompletedProcess-like with JSON stdout."""
    proc = MagicMock()
    proc.stdout = json.dumps(returned)
    return proc


class TestFindDuplicateOpenIssue:
    """Tests for find_duplicate_open_issue."""

    def test_returns_none_when_search_empty(self) -> None:
        with patch(
            "hephaestus.automation.issue_dedup._gh_call",
            return_value=_mock_gh([]),
        ):
            assert find_duplicate_open_issue("Add JWT auth helper", "") is None

    def test_returns_none_when_no_distinctive_tokens(self) -> None:
        # All-stopword title — search would be empty, must skip
        with patch("hephaestus.automation.issue_dedup._gh_call") as mock_gh:
            assert find_duplicate_open_issue("the a fix", "") is None
            mock_gh.assert_not_called()

    def test_returns_match_above_threshold(self) -> None:
        candidates = [
            {"number": 42, "title": "Add JWT auth helper for login flow", "body": "details"},
            {"number": 99, "title": "Refactor database layer", "body": "unrelated"},
        ]
        with patch(
            "hephaestus.automation.issue_dedup._gh_call",
            return_value=_mock_gh(candidates),
        ):
            match = find_duplicate_open_issue(
                "Add JWT auth helper for login flow",
                "needs JWT helper",
            )
        assert match is not None
        assert match.number == 42
        assert match.similarity >= 0.85

    def test_returns_none_below_threshold(self) -> None:
        candidates = [
            # Different topic — only minor token overlap
            {"number": 17, "title": "Update CI workflow timeouts", "body": ""},
        ]
        with patch(
            "hephaestus.automation.issue_dedup._gh_call",
            return_value=_mock_gh(candidates),
        ):
            match = find_duplicate_open_issue("Add JWT auth helper for login", "")
        assert match is None

    def test_picks_best_match_on_multiple_above_threshold(self) -> None:
        candidates = [
            {"number": 1, "title": "Add JWT auth helper for login flow", "body": "x"},
            {
                "number": 2,
                "title": "Add JWT auth helper for login flow service module",
                "body": "y",
            },
        ]
        with patch(
            "hephaestus.automation.issue_dedup._gh_call",
            return_value=_mock_gh(candidates),
        ):
            match = find_duplicate_open_issue("Add JWT auth helper for login flow", "")
        assert match is not None
        # The exact-match candidate (#1) has higher similarity than the longer one
        assert match.number == 1

    def test_search_failure_returns_none(self) -> None:
        with patch(
            "hephaestus.automation.issue_dedup._gh_call",
            side_effect=RuntimeError("gh down"),
        ):
            assert find_duplicate_open_issue("Add JWT auth helper", "") is None


class TestExtractNewInfo:
    """Tests for paragraph-level set diff."""

    def test_empty_when_pure_restatement(self) -> None:
        existing = "JWT helper is missing for login.\n\nWe should add it to auth/utils.py."
        new = "JWT helper missing for login.\n\nShould add it to auth utils."
        out = extract_new_info(new, existing)
        # Both paragraphs are restatements — high token overlap
        assert out == ""

    def test_returns_genuinely_new_paragraphs(self) -> None:
        existing = "JWT helper is missing for login."
        new = (
            "JWT helper is missing for login.\n\n"
            "Additionally, the refresh token rotation policy is undocumented "
            "and we should clarify it in docs/auth.md before shipping."
        )
        out = extract_new_info(new, existing)
        assert "refresh token rotation" in out
        assert "JWT helper is missing" not in out

    def test_returns_full_body_when_existing_empty(self) -> None:
        new = "Brand new paragraph one.\n\nBrand new paragraph two with more tokens."
        out = extract_new_info(new, "")
        assert "Brand new paragraph one" in out
        assert "Brand new paragraph two" in out


class TestIssueMatchDataclass:
    """Smoke test on IssueMatch construction."""

    def test_construction(self) -> None:
        m = IssueMatch(number=1, title="t", body="b", similarity=0.9)
        assert m.number == 1
        assert m.similarity == 0.9
