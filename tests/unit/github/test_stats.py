"""Tests for hephaestus.github.stats."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hephaestus.github.stats import (
    collect_stats,
    format_stats_table,
    get_commits_stats,
    get_current_repo,
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
    """Tests for get_prs_stats() — single GraphQL round-trip (#811)."""

    def _make_mock(self, returncode: int, stdout: str) -> MagicMock:
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        return m

    def test_returns_counts(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock(0, "[8,5,1]\n")
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

    def test_malformed_json_returns_zeros(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock(0, "not-json\n")
            result = get_prs_stats("2026-01-01", "2026-01-31", None, "owner/repo")
        assert result == {"total": 0, "merged": 0, "open": 0, "closed": 0}

    def test_short_array_returns_zeros(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock(0, "[1,2]\n")
            result = get_prs_stats("2026-01-01", "2026-01-31", None, "owner/repo")
        assert result == {"total": 0, "merged": 0, "open": 0, "closed": 0}

    def test_uses_single_graphql_call(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock(0, "[0,0,0]\n")
            get_prs_stats("2026-01-01", "2026-01-31", None, "owner/repo")
        assert mock_run.call_count == 1
        argv = mock_run.call_args[0][0]
        assert argv[:3] == ["gh", "api", "graphql"]

    def test_uses_graphql_with_correct_jq_filter(self) -> None:
        """The jq filter is part of the parse contract — drift breaks parsing."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock(0, "[0,0,0]\n")
            get_prs_stats("2026-01-01", "2026-01-31", None, "owner/repo")
        argv = mock_run.call_args[0][0]
        jq_idx = argv.index("--jq")
        jq_filter = argv[jq_idx + 1]
        assert ".data.total.issueCount" in jq_filter
        assert ".data.merged.issueCount" in jq_filter
        assert ".data.open.issueCount" in jq_filter
        assert "// 0" in jq_filter

    def test_author_included_in_aliased_queries(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._make_mock(0, "[0,0,0]\n")
            get_prs_stats("2026-01-01", "2026-01-31", "mvillmow", "owner/repo")
        argv = mock_run.call_args[0][0]
        joined = " ".join(argv)
        assert "author:mvillmow" in joined
        assert "is:merged" in joined
        assert "state:open" in joined
        assert "type:pr" in joined


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

    def test_invalid_start_date_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        from hephaestus.github.stats import main

        monkeypatch.setattr("sys.argv", ["hephaestus-github-stats", "bad", "2026-01-31", "--json"])
        assert main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert "start date" in payload["message"]

    def test_invalid_end_date_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        from hephaestus.github.stats import main

        monkeypatch.setattr("sys.argv", ["hephaestus-github-stats", "2026-01-01", "bad", "--json"])
        assert main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert "end date" in payload["message"]

    def test_valid_dates_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        from hephaestus.github.stats import main

        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-github-stats",
                "2026-01-01",
                "2026-01-31",
                "--repo",
                "owner/repo",
                "--json",
            ],
        )
        mock_stats = {
            "issues": {"total": 1, "open": 0, "closed": 1},
            "prs": {"total": 1, "merged": 1, "open": 0, "closed": 0},
            "commits": {"total": 5},
        }
        with patch("hephaestus.github.stats.collect_stats", return_value=mock_stats):
            assert main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["repo"] == "owner/repo"
        assert payload["stats"]["commits"]["total"] == 5

    def test_default_repo_via_get_current_repo(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When --repo not given, get_current_repo() is invoked."""
        import json

        from hephaestus.github.stats import main

        monkeypatch.setattr(
            "sys.argv",
            ["hephaestus-github-stats", "2026-01-01", "2026-01-31", "--json"],
        )
        with (
            patch(
                "hephaestus.github.stats.get_current_repo", return_value="owner/auto"
            ) as mock_repo,
            patch(
                "hephaestus.github.stats.collect_stats",
                return_value={
                    "issues": {"total": 0, "open": 0, "closed": 0},
                    "prs": {"total": 0, "merged": 0, "open": 0, "closed": 0},
                    "commits": {"total": 0},
                },
            ),
        ):
            assert main() == 0
        mock_repo.assert_called_once()
        payload = json.loads(capsys.readouterr().out)
        assert payload["repo"] == "owner/auto"


class TestStatsSubprocessTimeouts:
    """Every gh metadata read in stats.py must pass ``timeout=`` (#684).

    stats.py now routes every gh read through
    :func:`hephaestus.github.client.gh_call`, which invokes the subprocess via
    ``run_subprocess`` with ``timeout=gh_cli_timeout()`` (#713). These tests
    assert at that seam that a positive timeout is still supplied on every call,
    preserving the no-timeout-less-read invariant after the adapter move.
    """

    @staticmethod
    def _ok(stdout: str = "0\n") -> MagicMock:
        m = MagicMock()
        m.returncode = 0
        m.stdout = stdout
        return m

    def test_get_current_repo_passes_timeout(self) -> None:
        with patch("hephaestus.github.client.run_subprocess") as mock_run:
            mock_run.return_value = self._ok("owner/repo")
            get_current_repo()
        assert mock_run.call_args.kwargs["timeout"] > 0

    def test_get_issues_stats_all_calls_pass_timeout(self) -> None:
        with patch("hephaestus.github.client.run_subprocess") as mock_run:
            mock_run.side_effect = [self._ok("10\n"), self._ok("3\n")]
            get_issues_stats("2026-01-01", "2026-01-31", None, "owner/repo")
        assert mock_run.call_count == 2
        for call in mock_run.call_args_list:
            assert call.kwargs["timeout"] > 0

    def test_get_prs_stats_passes_timeout(self) -> None:
        # get_prs_stats batches total/merged/open into a single GraphQL call
        # routed through the shared gh_call adapter (client.run_subprocess).
        with patch("hephaestus.github.client.run_subprocess") as mock_run:
            mock_run.return_value = self._ok("[8,5,1]\n")
            get_prs_stats("2026-01-01", "2026-01-31", None, "owner/repo")
        assert mock_run.call_count == 1
        assert mock_run.call_args.kwargs["timeout"] > 0

    def test_get_commits_stats_passes_timeout(self) -> None:
        with patch("hephaestus.github.client.run_subprocess") as mock_run:
            mock_run.return_value = self._ok("4\n")
            get_commits_stats("2026-01-01", "2026-01-31", None, "owner/repo")
        assert mock_run.call_args.kwargs["timeout"] > 0
