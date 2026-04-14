"""Tests for hephaestus.github.stats."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hephaestus.github.stats import (
    collect_stats,
    format_stats_table,
    get_commits_stats,
    get_issues_stats,
    get_prs_stats,
    validate_date,
)


class TestValidateDate:
    """Tests for validate_date()."""

    def test_valid_date(self) -> None:
        assert validate_date("2026-01-15") is True

    def test_invalid_format(self) -> None:
        assert validate_date("01/15/2026") is False

    def test_non_existent_date(self) -> None:
        assert validate_date("2026-13-01") is False

    def test_empty_string(self) -> None:
        assert validate_date("") is False

    def test_year_only(self) -> None:
        assert validate_date("2026") is False


class TestGetIssuesStats:
    """Tests for get_issues_stats()."""

    def _make_mock(self, returncode: int, stdout: str) -> MagicMock:
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        return m

    def test_returns_counts(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                self._make_mock(0, "10\n"),
                self._make_mock(0, "3\n"),
            ]
            result = get_issues_stats("2026-01-01", "2026-01-31", None, "owner/repo")
        assert result["total"] == 10
        assert result["open"] == 3
        assert result["closed"] == 7

    def test_gh_failure_returns_zeros(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock(1, "")
            result = get_issues_stats("2026-01-01", "2026-01-31", None, "owner/repo")
        assert result == {"total": 0, "open": 0, "closed": 0}

    def test_author_filter_included_in_query(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                self._make_mock(0, "5\n"),
                self._make_mock(0, "2\n"),
            ]
            get_issues_stats("2026-01-01", "2026-01-31", "mvillmow", "owner/repo")
        # Verify author was part of the query string
        first_call_args = mock_run.call_args_list[0][0][0]
        query_arg = next((a for a in first_call_args if "author:" in a), "")
        assert "author:mvillmow" in query_arg


class TestGetPrsStats:
    """Tests for get_prs_stats()."""

    def _make_mock(self, returncode: int, stdout: str) -> MagicMock:
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        return m

    def test_returns_counts(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                self._make_mock(0, "8\n"),
                self._make_mock(0, "5\n"),
                self._make_mock(0, "1\n"),
            ]
            result = get_prs_stats("2026-01-01", "2026-01-31", None, "owner/repo")
        assert result["total"] == 8
        assert result["merged"] == 5
        assert result["open"] == 1
        assert result["closed"] == 2

    def test_gh_failure_returns_zeros(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock(1, "")
            result = get_prs_stats("2026-01-01", "2026-01-31", None, "owner/repo")
        assert result == {"total": 0, "merged": 0, "open": 0, "closed": 0}


class TestGetCommitsStats:
    """Tests for get_commits_stats()."""

    def _make_mock(self, returncode: int, stdout: str) -> MagicMock:
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        return m

    def test_returns_count(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock(0, "42\n")
            result = get_commits_stats("2026-01-01", "2026-01-31", None, "owner/repo")
        assert result["total"] == 42

    def test_sums_paginated_output(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock(0, "30\n12\n")
            result = get_commits_stats("2026-01-01", "2026-01-31", None, "owner/repo")
        assert result["total"] == 42

    def test_gh_failure_returns_zero(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock(1, "")
            result = get_commits_stats("2026-01-01", "2026-01-31", None, "owner/repo")
        assert result == {"total": 0}

    def test_author_param_passed(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock(0, "5\n")
            get_commits_stats("2026-01-01", "2026-01-31", "mvillmow", "owner/repo")
        call_args = mock_run.call_args[0][0]
        assert "author=mvillmow" in " ".join(call_args)


class TestCollectStats:
    """Tests for collect_stats()."""

    def test_returns_all_categories(self) -> None:
        issues = {"total": 1, "open": 0, "closed": 1}
        prs = {"total": 2, "merged": 1, "open": 0, "closed": 1}
        commits = {"total": 3}
        with (
            patch("hephaestus.github.stats.get_issues_stats", return_value=issues),
            patch("hephaestus.github.stats.get_prs_stats", return_value=prs),
            patch("hephaestus.github.stats.get_commits_stats", return_value=commits),
        ):
            result = collect_stats("2026-01-01", "2026-01-31", None, "owner/repo")
        assert "issues" in result
        assert "prs" in result
        assert "commits" in result


class TestFormatStatsTable:
    """Tests for format_stats_table()."""

    def _make_stats(self) -> dict:
        return {
            "issues": {"total": 10, "open": 3, "closed": 7},
            "prs": {"total": 8, "merged": 5, "open": 1, "closed": 2},
            "commits": {"total": 42},
        }

    def test_contains_header(self) -> None:
        table = format_stats_table(self._make_stats())
        assert "GitHub Contribution Statistics" in table

    def test_contains_issue_counts(self) -> None:
        table = format_stats_table(self._make_stats())
        assert "10" in table
        assert "ISSUES" in table

    def test_contains_pr_counts(self) -> None:
        table = format_stats_table(self._make_stats())
        assert "PULL REQUESTS" in table
        assert "8" in table

    def test_contains_commit_count(self) -> None:
        table = format_stats_table(self._make_stats())
        assert "COMMITS" in table
        assert "42" in table


class TestMain:
    """Tests for main() CLI entry point."""

    def test_invalid_start_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hephaestus.github.stats import main

        monkeypatch.setattr("sys.argv", ["hephaestus-github-stats", "bad-date", "2026-01-31"])
        assert main() == 1

    def test_invalid_end_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hephaestus.github.stats import main

        monkeypatch.setattr("sys.argv", ["hephaestus-github-stats", "2026-01-01", "not-a-date"])
        assert main() == 1

    def test_valid_dates_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hephaestus.github.stats import main

        monkeypatch.setattr(
            "sys.argv",
            ["hephaestus-github-stats", "2026-01-01", "2026-01-31", "--repo", "owner/repo"],
        )
        mock_stats = {
            "issues": {"total": 1, "open": 0, "closed": 1},
            "prs": {"total": 1, "merged": 1, "open": 0, "closed": 0},
            "commits": {"total": 5},
        }
        with patch("hephaestus.github.stats.collect_stats", return_value=mock_stats):
            result = main()
        assert result == 0
