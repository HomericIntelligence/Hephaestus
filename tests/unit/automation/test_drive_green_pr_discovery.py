"""Unit tests for drive-green PR discovery in loop_runner (#819)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from hephaestus.automation import loop_runner
from hephaestus.automation.loop_runner import (
    LoopConfig,
    PhaseResult,
    _build_phase_argv,
    _count_failing_prs,
)


def _ok(name: str) -> PhaseResult:
    """Create a successful phase result."""
    return PhaseResult(name=name, rc=0, elapsed_s=0.1)


@pytest.fixture
def repo_inputs(tmp_path: Path) -> tuple[Path, LoopConfig]:
    """Build a projects_dir + LoopConfig for process_repo tests."""
    projects = tmp_path
    (projects / "r" / ".git").mkdir(parents=True)
    cfg = LoopConfig(loops=1, projects_dir=projects)
    return projects, cfg


class TestCountFailingPrs:
    """Tests for _count_failing_prs gate function."""

    def test_count_failing_prs_counts_only_failing_conclusions(self) -> None:
        """PRs with failing conclusions are counted."""
        mock_output = [
            {
                "number": 1,
                "isDraft": False,
                "statusCheckRollup": [{"conclusion": "FAILURE"}],
                "mergeStateStatus": "CLEAN",
            },
            {
                "number": 2,
                "isDraft": False,
                "statusCheckRollup": [{"conclusion": "SUCCESS"}],
                "mergeStateStatus": "CLEAN",
            },
            {
                "number": 3,
                "isDraft": False,
                "statusCheckRollup": [{"conclusion": "CANCELLED"}],
                "mergeStateStatus": "CLEAN",
            },
        ]
        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh:
            mock_gh.return_value = Mock(stdout=json.dumps(mock_output))
            result = _count_failing_prs("MyOrg", "MyRepo")
        assert result == 2

    def test_count_failing_prs_excludes_draft_prs(self) -> None:
        """Draft PRs are excluded from the count."""
        mock_output = [
            {
                "number": 1,
                "isDraft": True,
                "statusCheckRollup": [{"conclusion": "FAILURE"}],
                "mergeStateStatus": "CLEAN",
            },
        ]
        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh:
            mock_gh.return_value = Mock(stdout=json.dumps(mock_output))
            result = _count_failing_prs("MyOrg", "MyRepo")
        assert result == 0

    def test_count_failing_prs_returns_zero_on_gh_error(self) -> None:
        """Returns 0 on gh command failure (fail-closed)."""
        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh:
            mock_gh.side_effect = subprocess.CalledProcessError(1, ["gh"])
            result = _count_failing_prs("MyOrg", "MyRepo")
        assert result == 0

    def test_count_failing_prs_returns_zero_on_timeout(self) -> None:
        """Returns 0 on timeout (fail-closed)."""
        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh:
            mock_gh.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)
            result = _count_failing_prs("MyOrg", "MyRepo")
        assert result == 0

    def test_count_failing_prs_returns_zero_on_invalid_json(self) -> None:
        """Returns 0 on invalid JSON (fail-closed)."""
        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh:
            mock_gh.return_value = Mock(stdout="not-json")
            result = _count_failing_prs("MyOrg", "MyRepo")
        assert result == 0

    def test_count_failing_prs_logs_warning_on_limit_hit(self) -> None:
        """A warning is logged when the 1000-PR cap is hit."""
        mock_output = [
            {
                "number": i,
                "isDraft": False,
                "statusCheckRollup": [{"conclusion": "FAILURE"}],
                "mergeStateStatus": "CLEAN",
            }
            for i in range(1, 1001)
        ]
        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh:
            mock_gh.return_value = Mock(stdout=json.dumps(mock_output))
            with patch("hephaestus.automation.loop_repo_manager.LOG") as mock_logger:
                result = _count_failing_prs("MyOrg", "MyRepo")
        assert result == 1000
        mock_logger.warning.assert_called()


class TestBuildPhaseArgv:
    """Tests for _build_phase_argv with drive-green."""

    def test_build_phase_argv_drive_green_omits_issues_when_unscoped(self) -> None:
        """--issues is NOT passed when cfg.issues is empty and no-args discovery is intended."""
        cfg = LoopConfig(
            org="MyOrg",
            agent="claude-opus-4-8",
            issues=[],
            max_workers=3,
            dry_run=False,
            no_advise=False,
            phases=("drive-green",),
            loops=1,
        )
        open_issues = [7, 8]
        with patch("hephaestus.automation.loop_runner._resolve_phase_bin") as mock_bin:
            mock_bin.return_value = ("/py", ["script.py"])
            argv = _build_phase_argv("drive-green", cfg, open_issues)
        assert argv is not None
        assert "--issues" not in argv

    def test_build_phase_argv_drive_green_passes_explicit_issues(self) -> None:
        """--issues is passed with explicit issue numbers when cfg.issues is provided."""
        cfg = LoopConfig(
            org="MyOrg",
            agent="claude-opus-4-8",
            issues=[123, 456],
            max_workers=3,
            dry_run=False,
            no_advise=False,
            phases=("drive-green",),
            loops=1,
        )
        open_issues = [7, 8]
        with patch("hephaestus.automation.loop_runner._resolve_phase_bin") as mock_bin:
            mock_bin.return_value = ("/py", ["script.py"])
            argv = _build_phase_argv("drive-green", cfg, open_issues)
        assert argv is not None
        issues_idx = argv.index("--issues")
        assert argv[issues_idx + 1 : issues_idx + 3] == ["123", "456"]


class TestPostLoopDriveGreenSkipLogic:
    """Tests for drive-green SKIP logic in ``_run_post_loop_stages`` (post-#818).

    drive-green moved out of the loop body and into the post-loop terminal
    stage. The same work-discovery gate (``_count_failing_prs``, --issues
    override) now lives in ``_run_post_loop_stages`` rather than
    ``process_repo``; these tests pin that invariant at the new home.
    """

    def test_drive_green_skips_with_reason_no_failing_prs(
        self, repo_inputs: tuple[Path, LoopConfig]
    ) -> None:
        """drive-green SKIPs with reason 'no failing PRs' when no failing PRs exist."""
        projects, cfg = repo_inputs
        cfg = LoopConfig(phases=("drive-green",), loops=1, projects_dir=projects)
        with (
            patch.object(loop_runner, "_rebase_main", return_value=("deadbee", True)),
            patch.object(loop_runner, "_list_open_issue_numbers", return_value=[]),
            patch.object(loop_runner, "_count_failing_prs", return_value=0),
            patch.object(
                loop_runner,
                "_resolve_repo_dir",
                side_effect=lambda pd, r: pd / r,
            ),
            patch.object(loop_runner, "run_phase") as mock_run,
        ):
            results = loop_runner._run_post_loop_stages(cfg, ["r"])
        drive_green = next(p for p in results[0].post_loop_phases if p.name == "drive-green")
        assert drive_green.skipped
        assert drive_green.skip_reason == "no failing PRs"
        mock_run.assert_not_called()

    def test_drive_green_runs_when_failing_prs_but_no_issues(
        self, repo_inputs: tuple[Path, LoopConfig]
    ) -> None:
        """drive-green RUNS when failing PRs exist but no issues are scoped."""
        projects, cfg = repo_inputs
        cfg = LoopConfig(phases=("drive-green",), loops=1, projects_dir=projects)
        with (
            patch.object(loop_runner, "_rebase_main", return_value=("deadbee", True)),
            patch.object(loop_runner, "_list_open_issue_numbers", return_value=[]),
            patch.object(loop_runner, "_count_failing_prs", return_value=3),
            patch.object(
                loop_runner,
                "_resolve_repo_dir",
                side_effect=lambda pd, r: pd / r,
            ),
            patch.object(loop_runner, "run_phase", side_effect=lambda **kw: _ok(kw["phase"])),
        ):
            results = loop_runner._run_post_loop_stages(cfg, ["r"])
        drive_green = next(p for p in results[0].post_loop_phases if p.name == "drive-green")
        assert not drive_green.skipped

    def test_drive_green_runs_when_operator_scoped_issues_present(
        self, repo_inputs: tuple[Path, LoopConfig]
    ) -> None:
        """drive-green RUNS when operator passes --issues, regardless of failing PR count."""
        projects, cfg = repo_inputs
        cfg = LoopConfig(
            issues=[10],
            phases=("drive-green",),
            loops=1,
            projects_dir=projects,
        )
        with (
            patch.object(loop_runner, "_rebase_main", return_value=("deadbee", True)),
            patch.object(loop_runner, "_list_open_issue_numbers", return_value=[]),
            patch.object(loop_runner, "_count_failing_prs", return_value=0),
            patch.object(
                loop_runner,
                "_resolve_repo_dir",
                side_effect=lambda pd, r: pd / r,
            ),
            patch.object(loop_runner, "run_phase", side_effect=lambda **kw: _ok(kw["phase"])),
        ):
            results = loop_runner._run_post_loop_stages(cfg, ["r"])
        drive_green = next(p for p in results[0].post_loop_phases if p.name == "drive-green")
        assert not drive_green.skipped
