"""Tests for direct --prs mode in CI driver (issue #918)."""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import ci_driver
from hephaestus.automation.ci_driver import CIDriver
from hephaestus.automation.models import CIDriverOptions, WorkerResult


class TestParseArgsPrs:
    """Verify --prs argparse integration."""

    def test_prs_parses_integers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--prs accepts PR numbers as integers."""
        monkeypatch.setattr(sys, "argv", ["ci", "--prs", "661", "662", "664", "666"])
        args = ci_driver._parse_args()
        assert args.prs == [661, 662, 664, 666]

    def test_prs_defaults_to_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--prs defaults to empty list when omitted."""
        monkeypatch.setattr(sys, "argv", ["ci"])
        args = ci_driver._parse_args()
        assert args.prs == []

    def test_prs_combined_with_issues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--prs and --issues can be used together."""
        monkeypatch.setattr(sys, "argv", ["ci", "--issues", "918", "--prs", "661", "662"])
        args = ci_driver._parse_args()
        assert args.issues == [918]
        assert args.prs == [661, 662]

    def test_prs_alone_without_issues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--prs can be used without --issues."""
        monkeypatch.setattr(sys, "argv", ["ci", "--prs", "661", "662"])
        args = ci_driver._parse_args()
        assert args.prs == [661, 662]
        assert args.issues == []


class TestDiscoverPrsDirectMode:
    """Unit tests for _discover_prs with options.prs populated."""

    @pytest.fixture
    def driver(self, tmp_path) -> CIDriver:
        """Create a CIDriver with bot-PR discovery disabled to prevent _gh_call in tests."""
        options = CIDriverOptions(include_bot_prs=False)
        return CIDriver(options)

    def test_direct_prs_alone_keyed_by_pr_number(self, driver: CIDriver) -> None:
        """Direct PRs alone bypass issue discovery, keyed by PR number."""
        driver.options.prs = [661, 662]

        # Mock _validate_pr_open to return True for these PRs
        # Also mock _discover_failing_prs: empty issues triggers the failing-PR path (#819)
        with patch.object(driver, "_validate_pr_open", return_value=True) as mock_validate:
            with patch("hephaestus.automation.ci_driver.find_pr_for_issue", return_value=None):
                with patch.object(driver, "_discover_failing_prs", return_value={}):
                    result = driver._discover_prs([])

        assert 661 in result.values()
        assert 662 in result.values()
        # Each PR is keyed by itself (synthetic issue)
        assert result[661] == 661
        assert result[662] == 662
        # Validate was called for each PR
        assert mock_validate.call_count == 2
        mock_validate.assert_any_call(661)
        mock_validate.assert_any_call(662)

    def test_direct_prs_populate_shared_pr_issues(self, driver: CIDriver) -> None:
        """Direct PRs populate shared_pr_issues[pr] = [pr]."""
        driver.options.prs = [661]

        with patch.object(driver, "_validate_pr_open", return_value=True):
            with patch("hephaestus.automation.ci_driver.find_pr_for_issue", return_value=None):
                with patch.object(driver, "_discover_failing_prs", return_value={}):
                    driver._discover_prs([])

        assert driver.shared_pr_issues.get(661) == [661]

    def test_issues_and_prs_overlap_deduped(self, driver: CIDriver) -> None:
        """When both --issues and --prs name the same PR, dedup (issue wins)."""
        driver.options.prs = [661]
        driver.options.include_bot_prs = False

        # Mock find_pr_for_issue to return 661 for issue 918
        with patch(
            "hephaestus.automation.ci_driver.find_pr_for_issue",
            return_value=661,
        ):
            with patch.object(driver, "_validate_pr_open", return_value=True) as mock_validate:
                with patch.object(driver, "_discover_failing_prs", return_value={}):
                    result = driver._discover_prs([918])

        # PR 661 should appear exactly once, keyed by issue 918 (from issue path)
        assert result[918] == 661
        # Direct-PR path should see 661 already in deduped.values() and skip it
        # Validate should NOT be called for the direct-PR duplicate
        mock_validate.assert_not_called()
        # Only one entry in the result
        assert len(result) == 1

    def test_issues_and_prs_disjoint_both_included(self, driver: CIDriver) -> None:
        """When --issues and --prs are disjoint, both PRs appear in result."""
        driver.options.prs = [661, 662]
        driver.options.include_bot_prs = False

        # Issue 918 maps to PR 999
        with patch(
            "hephaestus.automation.ci_driver.find_pr_for_issue",
            return_value=999,
        ):
            with patch.object(driver, "_validate_pr_open", return_value=True):
                with patch.object(driver, "_discover_failing_prs", return_value={}):
                    result = driver._discover_prs([918])

        # Issue 918 should map to PR 999
        assert result[918] == 999
        # Direct PRs 661 and 662 should also appear, keyed by themselves
        assert result[661] == 661
        assert result[662] == 662
        assert len(result) == 3

    def test_closed_pr_dropped_with_warning(self, driver: CIDriver) -> None:
        """Closed PRs are dropped with a warning, not raised."""
        driver.options.prs = [661, 662, 663]
        driver.options.include_bot_prs = False

        def validate_side_effect(pr_num):
            return pr_num != 662  # 662 is closed/non-existent

        with patch.object(driver, "_validate_pr_open", side_effect=validate_side_effect):
            with patch("hephaestus.automation.ci_driver.find_pr_for_issue", return_value=None):
                with patch.object(driver, "_discover_failing_prs", return_value={}):
                    result = driver._discover_prs([])

        # Valid PRs included
        assert result.get(661) == 661
        assert result.get(663) == 663
        # Closed PR excluded
        assert 662 not in result.values()

    def test_prs_honored_without_issues(self, driver: CIDriver) -> None:
        """--prs is honored even when issue_numbers is empty."""
        driver.options.prs = [661]
        driver.options.include_bot_prs = False

        with patch.object(driver, "_validate_pr_open", return_value=True):
            with patch("hephaestus.automation.ci_driver.find_pr_for_issue", return_value=None):
                with patch.object(driver, "_discover_failing_prs", return_value={}):
                    result = driver._discover_prs([])

        assert result.get(661) == 661

    def test_prs_honored_with_bot_prs_disabled(self, driver: CIDriver) -> None:
        """--prs works with include_bot_prs=False."""
        driver.options.prs = [661]
        driver.options.include_bot_prs = False

        with patch.object(driver, "_validate_pr_open", return_value=True):
            with patch("hephaestus.automation.ci_driver.find_pr_for_issue", return_value=None):
                with patch.object(driver, "_discover_failing_prs", return_value={}):
                    result = driver._discover_prs([])

        assert result.get(661) == 661


class TestRunGateWithPrs:
    """Verify run() does not abort early when --prs supplied."""

    def test_run_gate_does_not_abort_with_prs(self) -> None:
        """run() gate does not abort when --prs is supplied (only issues+bot are checked)."""
        options = CIDriverOptions(issues=[], prs=[661], include_bot_prs=False)
        driver = CIDriver(options)

        # Mock _discover_prs, _sweep_orphaned_arming_records, _drive_issue, and
        # _list_open_prs_remaining to avoid network I/O (circuit breaker would
        # trip on real _gh_call). The post-drive done-check
        # (_list_open_prs_remaining) resolves the viewer login via `gh api user`
        # under the default @me author filter (#821); without gh auth (CI) that
        # raises, so it must be mocked here alongside the other I/O paths.
        # Return a proper WorkerResult so run() can call result.success without error.
        fake_result = WorkerResult(issue_number=661, success=True)
        with patch.object(driver, "_discover_prs", return_value={661: 661}) as mock_discover:
            with patch.object(driver, "_sweep_orphaned_arming_records"):
                with patch.object(driver, "_drive_issue", return_value=fake_result):
                    with patch.object(driver, "_list_open_prs_remaining", return_value=[]):
                        driver.run()

        # Verify the gate did not abort by checking that _discover_prs was called
        mock_discover.assert_called_once()

    def test_run_gate_aborts_with_no_issues_no_prs_no_bot_prs(self) -> None:
        """run() aborts when all sources are empty."""
        options = CIDriverOptions(issues=[], prs=[], include_bot_prs=False)
        driver = CIDriver(options)
        result = driver.run()
        assert result == {}


class TestMainPrsFlow:
    """End-to-end via main() with --prs."""

    def test_main_prs_flows_through_to_driver_options(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() threads --prs into CIDriverOptions.prs."""
        monkeypatch.setattr(sys, "argv", ["ci", "--prs", "661", "662", "--dry-run"])
        with patch("hephaestus.automation.ci_driver.resolve_agent", return_value="claude"):
            with patch("hephaestus.automation.ci_driver.CIDriver") as mock_driver_class:
                with patch(
                    "hephaestus.automation.ci_driver._evaluate_run_result",
                    return_value=0,
                ):
                    mock_instance = MagicMock()
                    mock_instance.run.return_value = {}
                    mock_instance.open_prs_remaining = []
                    mock_driver_class.return_value = mock_instance

                    # Import and call main
                    ci_driver.main()

        # Verify CIDriver was instantiated with options.prs=[661, 662]
        call_args = mock_driver_class.call_args
        assert call_args is not None
        options = call_args[0][0]
        assert options.prs == [661, 662]

    def test_main_prs_json_output_includes_open_prs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--prs --json output includes open PRs from result."""
        monkeypatch.setattr(sys, "argv", ["ci", "--prs", "661", "662", "--json"])
        with patch("hephaestus.automation.ci_driver.resolve_agent", return_value="claude"):
            with patch("hephaestus.automation.ci_driver.CIDriver") as mock_driver_class:
                with patch(
                    "hephaestus.automation.ci_driver._evaluate_run_result",
                    return_value=0,
                ) as mock_eval:
                    mock_instance = MagicMock()
                    mock_instance.run.return_value = {661: None, 662: None}
                    mock_instance.open_prs_remaining = [661, 662]
                    mock_driver_class.return_value = mock_instance

                    ci_driver.main()

        # Verify that _evaluate_run_result was called with a result dict
        # containing the PR keys 661 and 662
        call_args = mock_eval.call_args
        result_dict = call_args[0][0]
        assert 661 in result_dict and 662 in result_dict


class TestValidatePrOpen:
    """Unit tests for the _validate_pr_open helper."""

    def test_validate_pr_open_returns_true_for_open_pr(self):
        """_validate_pr_open returns True for an OPEN PR."""
        options = CIDriverOptions()
        driver = CIDriver(options)

        with patch("hephaestus.automation.ci_driver._gh_call") as mock_gh_call:
            mock_gh_call.return_value.stdout = json.dumps({"state": "OPEN", "number": 661})
            result = driver._validate_pr_open(661)

        assert result is True

    def test_validate_pr_open_returns_false_for_closed_pr(self):
        """_validate_pr_open returns False for a CLOSED PR."""
        options = CIDriverOptions()
        driver = CIDriver(options)

        with patch("hephaestus.automation.ci_driver._gh_call") as mock_gh_call:
            mock_gh_call.return_value.stdout = json.dumps({"state": "CLOSED", "number": 661})
            result = driver._validate_pr_open(661)

        assert result is False

    def test_validate_pr_open_returns_false_for_nonexistent_pr(self):
        """_validate_pr_open returns False for non-existent PR."""
        options = CIDriverOptions()
        driver = CIDriver(options)

        with patch("hephaestus.automation.ci_driver._gh_call") as mock_gh_call:
            import subprocess

            mock_gh_call.side_effect = subprocess.CalledProcessError(1, "gh")
            result = driver._validate_pr_open(999)

        assert result is False

    def test_validate_pr_open_returns_false_for_empty_stdout(self):
        """_validate_pr_open returns False when stdout is None (empty response)."""
        options = CIDriverOptions()
        driver = CIDriver(options)

        with patch("hephaestus.automation.ci_driver._gh_call") as mock_gh_call:
            mock_gh_call.return_value.stdout = None
            result = driver._validate_pr_open(661)

        assert result is False

    def test_validate_pr_open_calls_gh_pr_view_correctly(self):
        """_validate_pr_open calls gh pr view with correct args."""
        options = CIDriverOptions()
        driver = CIDriver(options)

        with patch("hephaestus.automation.ci_driver._gh_call") as mock_gh_call:
            mock_gh_call.return_value.stdout = json.dumps({"state": "OPEN", "number": 661})
            driver._validate_pr_open(661)

        mock_gh_call.assert_called_once()
        call_args = mock_gh_call.call_args[0][0]
        assert "pr" in call_args
        assert "view" in call_args
        assert "661" in call_args
        assert "--json" in call_args
