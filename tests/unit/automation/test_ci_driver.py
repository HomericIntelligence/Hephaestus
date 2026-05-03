"""Tests for the CIDriver automation (ci_driver.py)."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from hephaestus.automation.ci_driver import CIDriver
from hephaestus.automation.models import CIDriverOptions

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_check(
    name: str,
    status: str = "completed",
    conclusion: str = "success",
    required: bool = True,
) -> dict[str, Any]:
    """Build a CI check dict."""
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "required": required,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_options() -> CIDriverOptions:
    """Create CIDriverOptions with minimal parallelism and no UI."""
    return CIDriverOptions(
        issues=[123],
        max_workers=1,
        dry_run=False,
        enable_ui=False,
        max_fix_iterations=1,
    )


@pytest.fixture
def driver(mock_options: CIDriverOptions, tmp_path: Path) -> CIDriver:
    """Create a CIDriver with mocked repo root."""
    with (
        patch("hephaestus.automation.ci_driver.get_repo_root", return_value=tmp_path),
        patch("hephaestus.automation.ci_driver.WorktreeManager"),
        patch("hephaestus.automation.ci_driver.StatusTracker"),
    ):
        d = CIDriver(mock_options)
        d.state_dir = tmp_path
        return d


# ---------------------------------------------------------------------------
# _load_impl_session_id
# ---------------------------------------------------------------------------


class TestLoadImplSessionId:
    """Tests for _load_impl_session_id."""

    def test_returns_session_id_when_present(self, driver: CIDriver, tmp_path: Path) -> None:
        """state-{issue}.json with session_id → returns it."""
        state_file = tmp_path / "state-123.json"
        state_file.write_text(json.dumps({"session_id": "sess-xyz"}))
        driver.state_dir = tmp_path

        result = driver._load_impl_session_id(123)

        assert result == "sess-xyz"

    def test_returns_none_when_no_file(self, driver: CIDriver, tmp_path: Path) -> None:
        """No state file → returns None."""
        driver.state_dir = tmp_path  # empty

        result = driver._load_impl_session_id(123)

        assert result is None

    def test_returns_none_when_no_key(self, driver: CIDriver, tmp_path: Path) -> None:
        """State file missing session_id key → returns None."""
        state_file = tmp_path / "state-123.json"
        state_file.write_text(json.dumps({"phase": "completed"}))
        driver.state_dir = tmp_path

        result = driver._load_impl_session_id(123)

        assert result is None


# ---------------------------------------------------------------------------
# _parse_json_block
# ---------------------------------------------------------------------------


class TestParseJsonBlock:
    """Tests for CIDriver._parse_json_block."""

    def test_extracts_json_block(self, driver: CIDriver) -> None:
        """Parses first ```json block from text."""
        payload = {"fixed": True, "notes": "All tests pass"}
        text = "Some output\n```json\n" + json.dumps(payload) + "\n```\nMore text"
        result = driver._parse_json_block(text)
        assert result == payload

    def test_falls_back_to_raw_json(self, driver: CIDriver) -> None:
        """Parses raw JSON if no code block present."""
        payload = {"fixed": False}
        result = driver._parse_json_block(json.dumps(payload))
        assert result == payload

    def test_returns_empty_dict_on_invalid(self, driver: CIDriver) -> None:
        """Returns {} for unparseable input."""
        result = driver._parse_json_block("not json at all")
        assert result == {}


# ---------------------------------------------------------------------------
# _drive_issue: no PR found
# ---------------------------------------------------------------------------


class TestNoPrFound:
    """Tests for when no PR exists for an issue."""

    def test_no_pr_found_skips(self, driver: CIDriver) -> None:
        """No PR for any issue → run() returns {} without launching any workers."""
        with patch.object(driver, "_find_pr_for_issue", return_value=None):
            results = driver.run()

        assert results == {}


# ---------------------------------------------------------------------------
# _drive_issue: all-green path
# ---------------------------------------------------------------------------


class TestAllRequiredGreen:
    """Tests for the all-green CI path."""

    def test_all_required_green_enables_auto_merge(self, driver: CIDriver) -> None:
        """All required checks success → _enable_auto_merge called."""
        checks = [
            _make_check("test", required=True),
            _make_check("lint", required=True),
        ]
        with (
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(driver, "_enable_auto_merge") as mock_merge,
        ):
            result = driver._drive_issue(123, 456, 0)

        assert result.success is True
        mock_merge.assert_called_once_with(456)

    def test_dry_run_no_auto_merge(self, mock_options: CIDriverOptions, tmp_path: Path) -> None:
        """dry_run=True, all green → gh pr merge not called."""
        mock_options.dry_run = True

        with (
            patch("hephaestus.automation.ci_driver.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.ci_driver.WorktreeManager"),
            patch("hephaestus.automation.ci_driver.StatusTracker"),
        ):
            dry_driver = CIDriver(mock_options)
            dry_driver.state_dir = tmp_path

        checks = [_make_check("test", required=True)]
        with (
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(dry_driver, "_enable_auto_merge") as mock_merge,
            patch("hephaestus.automation.ci_driver._gh_call") as mock_gh,
        ):
            result = dry_driver._drive_issue(123, 456, 0)

        assert result.success is True
        mock_merge.assert_not_called()
        # Ensure the raw gh call for merge was not made either
        merge_calls = [c for c in mock_gh.call_args_list if "merge" in str(c)]
        assert len(merge_calls) == 0


# ---------------------------------------------------------------------------
# required vs non-required check classification
# ---------------------------------------------------------------------------


class TestRequiredVsNonRequired:
    """Tests for required vs non-required check gate logic."""

    def test_no_required_checks_uses_all(self, driver: CIDriver) -> None:
        """No check has required=True → all checks treated as required."""
        checks = [
            _make_check("test", required=False, conclusion="success"),
            _make_check("lint", required=False, conclusion="success"),
        ]
        with (
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(driver, "_enable_auto_merge") as mock_merge,
        ):
            result = driver._drive_issue(123, 456, 0)

        # All non-required treated as required → all green → auto-merge
        assert result.success is True
        mock_merge.assert_called_once_with(456)

    def test_required_vs_nonrequired_only_required_gates_green(self, driver: CIDriver) -> None:
        """Mix of required/non-required; only required=True ones gate green."""
        checks = [
            _make_check("required-test", required=True, conclusion="success"),
            # Non-required check is failing but should NOT block auto-merge
            _make_check("optional-lint", required=False, conclusion="failure"),
        ]
        with (
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(driver, "_enable_auto_merge") as mock_merge,
        ):
            result = driver._drive_issue(123, 456, 0)

        assert result.success is True
        mock_merge.assert_called_once_with(456)

    def test_failing_required_runs_fix_session(self, driver: CIDriver) -> None:
        """Required check failed → _run_ci_fix_session called."""
        checks = [
            _make_check("required-test", required=True, conclusion="failure"),
        ]
        with (
            patch.object(driver, "_find_pr_for_issue", return_value=42),
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(driver, "_get_failing_ci_logs", return_value="error log"),
            patch.object(driver, "_load_impl_session_id", return_value=None),
            patch.object(driver, "_get_worktree_path", return_value=Path("/tmp/wt")),
            patch.object(driver, "_run_ci_fix_session", return_value=True) as mock_fix,
        ):
            result = driver._drive_issue(123, 456, 0)

        mock_fix.assert_called_once()
        assert result.success is True

    def test_pending_checks_skip_fix(self, driver: CIDriver) -> None:
        """All checks pending (not completed) → no fix attempted."""
        checks = [
            _make_check("test", status="in_progress", conclusion="", required=True),
        ]
        with (
            patch.object(driver, "_find_pr_for_issue", return_value=42),
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(driver, "_run_ci_fix_session") as mock_fix,
        ):
            result = driver._drive_issue(123, 456, 0)

        mock_fix.assert_not_called()
        assert result.success is True


# ---------------------------------------------------------------------------
# dry_run with failing checks
# ---------------------------------------------------------------------------


class TestDryRunWithFailingChecks:
    """Tests for dry_run=True when checks are failing."""

    def test_dry_run_no_fix_push(self, mock_options: CIDriverOptions, tmp_path: Path) -> None:
        """dry_run=True, required check failed → fix session logs intent but doesn't push."""
        mock_options.dry_run = True

        with (
            patch("hephaestus.automation.ci_driver.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.ci_driver.WorktreeManager"),
            patch("hephaestus.automation.ci_driver.StatusTracker"),
        ):
            dry_driver = CIDriver(mock_options)
            dry_driver.state_dir = tmp_path

        checks = [_make_check("test", required=True, conclusion="failure")]
        with (
            patch.object(dry_driver, "_find_pr_for_issue", return_value=42),
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(dry_driver, "_get_failing_ci_logs", return_value="log"),
            patch.object(dry_driver, "_load_impl_session_id", return_value=None),
            patch.object(dry_driver, "_get_worktree_path", return_value=tmp_path),
            patch.object(dry_driver, "_run_ci_fix_session") as mock_fix,
        ):
            result = dry_driver._drive_issue(123, 456, 0)

        # dry_run returns success before actually running the fix session
        assert result.success is True
        mock_fix.assert_not_called()


# ---------------------------------------------------------------------------
# No CI checks found
# ---------------------------------------------------------------------------


class TestNoCiChecks:
    """Tests for when no CI checks are returned."""

    def test_no_checks_returns_success(self, driver: CIDriver) -> None:
        """No CI checks for PR → returns WorkerResult(success=True)."""
        with patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=[]):
            result = driver._drive_issue(123, 456, 0)

        assert result.success is True
        assert result.pr_number == 456
