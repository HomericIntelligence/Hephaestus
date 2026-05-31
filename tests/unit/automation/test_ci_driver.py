"""Tests for the CIDriver automation (ci_driver.py)."""

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.agents.runtime import AgentRunResult
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
    """Create CIDriverOptions with minimal parallelism and no UI.

    ``enable_advise=False`` by default: the advise-first step (#30) would
    otherwise spawn a real ``claude``/``gh`` subprocess in the CI-fix path on
    hosts where ``claude`` is on PATH. Tests that specifically exercise advise
    flip it back on and patch ``_run_advise``.
    """
    return CIDriverOptions(
        issues=[123],
        max_workers=1,
        dry_run=False,
        enable_ui=False,
        enable_advise=False,
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
        """Legacy state file with Claude session_id returns it for Claude."""
        state_file = tmp_path / "state-123.json"
        state_file.write_text(json.dumps({"session_id": "sess-xyz"}))
        driver.state_dir = tmp_path

        result = driver._load_impl_session_id(123)

        assert result == "sess-xyz"

    def test_skips_legacy_session_for_codex(self, driver: CIDriver, tmp_path: Path) -> None:
        """Legacy state files contain Claude sessions and must not resume as Codex."""
        state_file = tmp_path / "state-123.json"
        state_file.write_text(json.dumps({"session_id": "sess-xyz"}))
        driver.state_dir = tmp_path
        driver.options.agent = "codex"

        result = driver._load_impl_session_id(123)

        assert result is None

    def test_returns_matching_codex_session(self, driver: CIDriver, tmp_path: Path) -> None:
        """Provider metadata allows Codex sessions to be resumed by Codex."""
        state_file = tmp_path / "state-123.json"
        state_file.write_text(json.dumps({"session_id": "codex-sess", "session_agent": "codex"}))
        driver.state_dir = tmp_path
        driver.options.agent = "codex"

        result = driver._load_impl_session_id(123)

        assert result == "codex-sess"

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


def test_codex_ci_fix_session_falls_back_to_fresh_on_resume_failure(
    driver: CIDriver,
    tmp_path: Path,
) -> None:
    """Codex CI repair should retry fresh when a saved session cannot resume."""
    driver.options.agent = "codex"
    resume_error = subprocess.CalledProcessError(
        1,
        ["codex"],
        stderr="session not found",
    )
    fresh_result = AgentRunResult(stdout="fixed", stderr="", session_id="fresh-session")

    with (
        patch("hephaestus.automation.ci_driver.resume_codex_session", side_effect=resume_error),
        patch(
            "hephaestus.automation.ci_driver.run_codex_session",
            return_value=fresh_result,
        ) as mock_fresh,
        patch(
            "hephaestus.automation.ci_driver.push_current_branch_with_lease_on_divergence"
        ) as mock_push,
        patch("hephaestus.automation.ci_driver.sync_worktree_to_remote_branch") as mock_sync,
    ):
        result = driver._run_ci_fix_session(
            issue_number=123,
            pr_number=456,
            worktree_path=tmp_path,
            ci_logs="failed",
            session_id="old-session",
            pr_head_branch="456-pr-head",
        )

    assert result is True
    mock_fresh.assert_called_once()
    # Worktree must be reset to the PR's remote head *before* the agent runs
    # so the fix is committed on top of the real PR history (#832).
    mock_sync.assert_called_once_with(tmp_path, "456-pr-head")
    # And the push must target the PR's head branch explicitly — not bare HEAD —
    # so a Claude-side branch switch cannot route the fix to a stray branch (#832).
    mock_push.assert_called_once_with(tmp_path, branch="456-pr-head", push_ref="HEAD:456-pr-head")


# ---------------------------------------------------------------------------
# _discover_prs: dedupe shared-PR
# ---------------------------------------------------------------------------


class TestDiscoverPrsDedupe:
    """Tests for #834: PRs that close multiple issues are processed once."""

    def test_single_issue_per_pr_unchanged(self, driver: CIDriver) -> None:
        """The 1:1 mapping case is unchanged: every input issue resolves to a PR."""
        with patch.object(driver, "_find_pr_for_issue", side_effect=[100, 101, 102]):
            result = driver._discover_prs([1, 2, 3])
        assert result == {1: 100, 2: 101, 3: 102}

    def test_multiple_issues_one_pr_collapses_to_lowest(self, driver: CIDriver) -> None:
        """Nine issues → same PR → result has one entry keyed by the lowest issue (#834)."""
        # Reproduces the ProjectNestor failure: PR #103 closes nine issues.
        # Without dedupe the driver would race nine workers against the same
        # branch and the eight losers would fail `git worktree add`.
        with patch.object(
            driver,
            "_find_pr_for_issue",
            side_effect=[103] * 9,
        ):
            result = driver._discover_prs([64, 59, 39, 37, 29, 28, 23, 22, 12])

        assert result == {12: 103}

    def test_mixed_shared_and_unique_prs(self, driver: CIDriver) -> None:
        """Mix of shared and unique PRs: each PR appears once, shared via lowest issue."""
        # 1,2 → PR 100 (shared); 3 → PR 200 (unique); 4,5 → PR 300 (shared)
        with patch.object(
            driver,
            "_find_pr_for_issue",
            side_effect=[100, 100, 200, 300, 300],
        ):
            result = driver._discover_prs([1, 2, 3, 4, 5])
        assert result == {1: 100, 3: 200, 4: 300}

    def test_no_pr_skipped_separately_from_shared(self, driver: CIDriver) -> None:
        """Issues with no PR are dropped; remaining issues still get deduped."""
        # 1 → PR 100; 2 → no PR; 3 → PR 100 (shared with 1)
        with patch.object(driver, "_find_pr_for_issue", side_effect=[100, None, 100]):
            result = driver._discover_prs([1, 2, 3])
        assert result == {1: 100}


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
            patch.object(driver, "_run_drive_green_learnings"),
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
            patch.object(driver, "_run_drive_green_learnings"),
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
            patch.object(driver, "_run_drive_green_learnings"),
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

    def test_pending_checks_skip_fix(
        self, driver: CIDriver, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All checks pending (not completed) → no fix attempted."""
        checks = [
            _make_check("test", status="in_progress", conclusion="", required=True),
        ]
        monkeypatch.setenv("HEPH_CI_POLL_MAX_WAIT", "0")
        with (
            patch.object(driver, "_find_pr_for_issue", return_value=42),
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(driver, "_run_ci_fix_session") as mock_fix,
        ):
            result = driver._drive_issue(123, 456, 0)

        mock_fix.assert_not_called()
        assert result.success is True


# ---------------------------------------------------------------------------
# advise-first (#30) — runs once before the fix loop, gated by enable_advise
# ---------------------------------------------------------------------------


class TestCiDriverAdvise:
    """Stage 3 advise-first wiring in _attempt_ci_fixes."""

    def test_advise_runs_before_fix_loop_when_enabled(self, driver: CIDriver) -> None:
        """enable_advise=True → _run_advise is called and its findings reach the fix session."""
        driver.options.enable_advise = True
        with (
            patch.object(driver, "_get_failing_ci_logs", return_value="error log"),
            patch.object(driver, "_load_impl_session_id", return_value=None),
            patch.object(driver, "_get_worktree_path", return_value=Path("/tmp/wt")),
            patch.object(
                driver, "_run_advise", return_value="## Findings\n- mind X"
            ) as mock_advise,
            patch.object(driver, "_run_ci_fix_session", return_value=True) as mock_fix,
        ):
            driver._attempt_ci_fixes(123, 456, 0)

        mock_advise.assert_called_once_with(123)
        # Findings are forwarded as the 6th positional arg to the fix session.
        assert mock_fix.call_args.args[5] == "## Findings\n- mind X"

    def test_advise_skipped_when_disabled(self, driver: CIDriver) -> None:
        """enable_advise=False → _run_advise is never called and findings are empty."""
        driver.options.enable_advise = False
        with (
            patch.object(driver, "_get_failing_ci_logs", return_value="error log"),
            patch.object(driver, "_load_impl_session_id", return_value=None),
            patch.object(driver, "_get_worktree_path", return_value=Path("/tmp/wt")),
            patch.object(driver, "_run_advise") as mock_advise,
            patch.object(driver, "_run_ci_fix_session", return_value=True) as mock_fix,
        ):
            driver._attempt_ci_fixes(123, 456, 0)

        mock_advise.assert_not_called()
        assert mock_fix.call_args.args[5] == ""


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


# ---------------------------------------------------------------------------
# #376: _enable_auto_merge fallback + return value
# ---------------------------------------------------------------------------


class TestEnableAutoMerge:
    """Tests for _enable_auto_merge fallback logic (#376)."""

    def test_squash_success_returns_true(self, driver: CIDriver) -> None:
        """Successful --auto --squash returns True (squash-only repo)."""
        with patch("hephaestus.automation.ci_driver._gh_call") as mock_gh:
            result = driver._enable_auto_merge(99)

        assert result is True
        assert mock_gh.call_count == 1
        # Primary auto-merge MUST be squash — rebase is disabled by branch protection.
        primary_call_args = mock_gh.call_args_list[0][0][0]
        assert "--squash" in primary_call_args
        assert "--rebase" not in primary_call_args

    def test_squash_failure_no_fallback_flag_returns_false(self, driver: CIDriver) -> None:
        """--auto --squash fails; force_merge_on_stall=False → returns False, no fallback."""
        driver.options.force_merge_on_stall = False
        with patch(
            "hephaestus.automation.ci_driver._gh_call",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ) as mock_gh:
            result = driver._enable_auto_merge(99)

        assert result is False
        # Only 1 gh call (the failed --auto --squash); no fallback call
        assert mock_gh.call_count == 1

    def test_squash_failure_with_fallback_flag_tries_squash(self, driver: CIDriver) -> None:
        """--auto --squash fails; force_merge_on_stall=True → tries squash, returns True."""
        driver.options.force_merge_on_stall = True
        call_results = [
            subprocess.CalledProcessError(1, "gh"),  # first call: --auto fails
            MagicMock(),  # second call: squash succeeds
        ]
        with patch("hephaestus.automation.ci_driver._gh_call", side_effect=call_results) as mock_gh:
            result = driver._enable_auto_merge(99)

        assert result is True
        assert mock_gh.call_count == 2
        squash_call_args = mock_gh.call_args_list[1][0][0]
        assert "--squash" in squash_call_args

    def test_both_strategies_fail_returns_false(self, driver: CIDriver) -> None:
        """Both --auto and --squash fail → returns False."""
        driver.options.force_merge_on_stall = True
        with patch(
            "hephaestus.automation.ci_driver._gh_call",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ):
            result = driver._enable_auto_merge(99)

        assert result is False

    def test_auto_merge_failure_propagates_to_drive_issue(self, driver: CIDriver) -> None:
        """When _enable_auto_merge returns False, _drive_issue returns success=False.

        Learnings must NOT run when auto-merge fails (gated to success).
        """
        checks = [_make_check("test", required=True)]
        with (
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(driver, "_enable_auto_merge", return_value=False),
            patch.object(driver, "_run_drive_green_learnings") as mock_learn,
        ):
            result = driver._drive_issue(123, 456, 0)

        assert result.success is False
        assert result.error is not None
        mock_learn.assert_not_called()


# ---------------------------------------------------------------------------
# Stage 3: drive-green learnings step (AGENT_CI_DRIVER, Session 3)
# ---------------------------------------------------------------------------


class TestDriveGreenLearnings:
    """Tests for the post-green learnings capture under AGENT_CI_DRIVER."""

    def test_learnings_runs_on_success_under_ci_driver_agent(
        self, driver: CIDriver, tmp_path: Path
    ) -> None:
        """All required green + auto-merge ok → learnings resumes Session 3."""
        from hephaestus.automation.session_naming import AGENT_CI_DRIVER

        wt = tmp_path / "wt-123"
        checks = [_make_check("test", required=True)]
        with (
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(driver, "_enable_auto_merge", return_value=True),
            patch.object(driver, "_get_worktree_path", return_value=wt),
            patch(
                "hephaestus.automation.ci_driver.invoke_claude_with_session",
                return_value=("ok", "sid"),
            ) as mock_invoke,
            patch("hephaestus.automation.ci_driver.get_repo_slug", return_value="ProjectX"),
            patch("hephaestus.automation.ci_driver.current_trunk_githash", return_value="abc1234"),
        ):
            result = driver._drive_issue(123, 456, 0)

        assert result.success is True
        mock_invoke.assert_called_once()
        assert mock_invoke.call_args.kwargs["agent"] == AGENT_CI_DRIVER
        assert mock_invoke.call_args.kwargs["issue"] == 123
        # Must resume from the worktree so the Session 3 transcript is found.
        assert mock_invoke.call_args.kwargs["cwd"] == wt

    def test_learnings_failure_is_non_fatal(self, driver: CIDriver, tmp_path: Path) -> None:
        """A raising learnings session must not flip a successful drive to failure."""
        checks = [_make_check("test", required=True)]
        with (
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(driver, "_enable_auto_merge", return_value=True),
            patch.object(driver, "_get_worktree_path", return_value=tmp_path),
            patch(
                "hephaestus.automation.ci_driver.invoke_claude_with_session",
                side_effect=RuntimeError("boom"),
            ),
            patch("hephaestus.automation.ci_driver.get_repo_slug", return_value="ProjectX"),
            patch("hephaestus.automation.ci_driver.current_trunk_githash", return_value="abc1234"),
        ):
            result = driver._drive_issue(123, 456, 0)

        # Drive still succeeds even though learnings raised.
        assert result.success is True

    def test_learnings_skipped_for_codex(self, driver: CIDriver) -> None:
        """Codex has no persisted drive-green session, so learnings is skipped."""
        driver.options.agent = "codex"
        with patch("hephaestus.automation.ci_driver.invoke_claude_with_session") as mock_invoke:
            result = driver._run_drive_green_learnings(123, 456)

        assert result is False
        mock_invoke.assert_not_called()


# ---------------------------------------------------------------------------
# #377: CI poll loop for pending checks
# ---------------------------------------------------------------------------


class TestCIPollLoop:
    """Tests for the bounded CI poll loop (#377)."""

    def test_polls_until_checks_complete(
        self, driver: CIDriver, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gh_pr_checks returns queued×2 then success×1; loop polls 3 times."""
        pending_check = _make_check("test", status="queued", conclusion="", required=True)
        completed_check = _make_check(
            "test", status="completed", conclusion="success", required=True
        )

        call_count = {"n": 0}

        def side_effect(pr_number: int, **kwargs: Any) -> list[dict[str, Any]]:
            call_count["n"] += 1
            if call_count["n"] < 3:
                return [pending_check]
            return [completed_check]

        monkeypatch.setenv("HEPH_CI_POLL_MAX_WAIT", "3600")
        with (
            patch("hephaestus.automation.ci_driver.gh_pr_checks", side_effect=side_effect),
            patch("hephaestus.automation.ci_driver.time.sleep"),
            patch.object(driver, "_enable_auto_merge", return_value=True),
            patch.object(driver, "_run_drive_green_learnings"),
        ):
            result = driver._drive_issue(123, 456, 0)

        assert call_count["n"] == 3, f"Expected 3 poll calls, got {call_count['n']}"
        assert result.success is True

    def test_pending_check_exceeds_timeout_returns_success(
        self, driver: CIDriver, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All checks stay pending → returns success=True after timeout (not our job to wait)."""
        pending_check = _make_check("test", status="in_progress", conclusion="", required=True)

        monkeypatch.setenv("HEPH_CI_POLL_MAX_WAIT", "0")
        with (
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=[pending_check]),
            patch("hephaestus.automation.ci_driver.time.sleep"),
        ):
            result = driver._drive_issue(123, 456, 0)

        assert result.success is True


# ---------------------------------------------------------------------------
# #378: CIDriver.run() cleanup_all in finally + preserved reporting
# ---------------------------------------------------------------------------


class TestRunCleanup:
    """Tests that CIDriver.run() cleans up worktrees in a finally block (#378).

    Note: cleanup_all is only reached when _discover_prs returns a non-empty
    map (i.e. there are PRs to process). The early-return for no-PR cases
    bypasses the try/finally intentionally — there are no worktrees to clean.
    """

    def _make_driver_with_mock_wm(
        self,
        mock_options: CIDriverOptions,
        tmp_path: Path,
        preserved: list,
    ) -> tuple["CIDriver", MagicMock]:
        """Create a CIDriver with a MagicMock WorktreeManager."""
        with (
            patch("hephaestus.automation.ci_driver.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.ci_driver.StatusTracker"),
        ):
            mock_wm = MagicMock()
            mock_wm.preserved = preserved
            with patch("hephaestus.automation.ci_driver.WorktreeManager", return_value=mock_wm):
                d = CIDriver(mock_options)
                d.state_dir = tmp_path
        return d, mock_wm

    def test_cleanup_all_called_on_success(
        self, mock_options: CIDriverOptions, tmp_path: Path
    ) -> None:
        """cleanup_all() is called when _drive_issue completes normally."""
        d, mock_wm = self._make_driver_with_mock_wm(mock_options, tmp_path, [])

        # Provide a non-empty PR map so we enter the try/finally block
        green_check = _make_check("test", required=True)
        with (
            patch.object(d, "_discover_prs", return_value={1: 10}),
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=[green_check]),
            patch.object(d, "_enable_auto_merge", return_value=True),
            patch.object(d, "_run_drive_green_learnings"),
        ):
            d.run()

        mock_wm.cleanup_all.assert_called_once()

    def test_cleanup_all_called_even_on_exception(
        self, mock_options: CIDriverOptions, tmp_path: Path
    ) -> None:
        """cleanup_all() is called even when _drive_issue raises."""
        d, mock_wm = self._make_driver_with_mock_wm(mock_options, tmp_path, [])

        with (
            patch.object(d, "_discover_prs", return_value={1: 10}),
            patch.object(d, "_drive_issue", side_effect=RuntimeError("boom")),
        ):
            results = d.run()

        mock_wm.cleanup_all.assert_called_once()
        assert results[1].success is False

    def test_preserved_worktrees_are_logged(
        self, mock_options: CIDriverOptions, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """After cleanup_all, preserved worktrees are logged at INFO."""
        import logging

        preserved_path = tmp_path / "issue-1"
        d, _mock_wm = self._make_driver_with_mock_wm(mock_options, tmp_path, [(1, preserved_path)])

        green_check = _make_check("test", required=True)
        with (
            patch.object(d, "_discover_prs", return_value={1: 10}),
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=[green_check]),
            patch.object(d, "_enable_auto_merge", return_value=True),
            patch.object(d, "_run_drive_green_learnings"),
            caplog.at_level(logging.INFO, logger="hephaestus.automation.ci_driver"),
        ):
            d.run()

        logs = caplog.text
        assert "Preserved worktrees" in logs
        assert str(preserved_path) in logs


# ---------------------------------------------------------------------------
# #379: _get_failing_ci_logs scoped to PR's branch
# ---------------------------------------------------------------------------


class TestGetFailingCiLogs:
    """Tests that _get_failing_ci_logs scopes runs to the PR's branch (#379)."""

    def test_uses_pr_branch_in_gh_call(self, driver: CIDriver) -> None:
        """``gh run list`` must include ``--branch <branch>`` from the PR."""
        driver.options.dry_run = False
        with (
            patch.object(driver, "_get_pr_branch", return_value="123-auto-impl"),
            patch("hephaestus.automation.ci_driver._gh_call") as mock_gh,
        ):
            mock_gh.return_value = MagicMock(stdout="[]")
            driver._get_failing_ci_logs(pr_number=456)

        call_args = mock_gh.call_args[0][0]
        assert "--branch" in call_args
        assert "123-auto-impl" in call_args

    def test_does_not_use_repo_wide_list(self, driver: CIDriver) -> None:
        """``gh run list`` must NOT be called without a ``--branch`` filter."""
        with (
            patch.object(driver, "_get_pr_branch", return_value="my-branch"),
            patch("hephaestus.automation.ci_driver._gh_call") as mock_gh,
        ):
            mock_gh.return_value = MagicMock(stdout="[]")
            driver._get_failing_ci_logs(pr_number=10)

        call_args = mock_gh.call_args[0][0]
        # Without --branch this would be a repo-wide list which we must avoid
        assert "--branch" in call_args


# ---------------------------------------------------------------------------
# #382/A4-09: No dead tempfile in _run_ci_fix_session
# ---------------------------------------------------------------------------


class TestNoDeadTempfile:
    """Tests that _run_ci_fix_session no longer creates an unused tempfile (#382/A4-09)."""

    def test_no_tempfile_created(self, driver: CIDriver, tmp_path: Path) -> None:
        """_run_ci_fix_session must not create any .txt files in worktree_path."""
        with (
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stderr="fail", stdout="")
            driver._run_ci_fix_session(
                issue_number=1,
                pr_number=2,
                worktree_path=tmp_path,
                ci_logs="",
                session_id=None,
                pr_head_branch="some-branch",
            )

        txt_files = list(tmp_path.glob("*.txt"))
        assert txt_files == [], f"Unexpected temp files: {txt_files}"


# ---------------------------------------------------------------------------
# #382/A4-10: Body search uses 'Closes #N in:body'
# ---------------------------------------------------------------------------


class TestBodySearch:
    """Tests that _find_pr_for_issue uses 'Closes #N in:body' (#382/A4-10)."""

    def test_body_search_uses_closes_pattern(self, driver: CIDriver) -> None:
        """The search string must use 'Closes #<N> in:body'."""
        # _find_pr_for_issue now delegates to _review_utils.find_pr_for_issue;
        # patch _gh_call at its actual call site there.
        with patch("hephaestus.automation._review_utils._gh_call") as mock_gh:
            mock_gh.return_value = MagicMock(stdout="[]")
            driver._find_pr_for_issue(42)

        # The second gh call should be the body search
        body_search_calls = [c for c in mock_gh.call_args_list if "search" in str(c)]
        assert body_search_calls, "No gh call with --search found"
        search_arg = str(body_search_calls[0])
        assert "Closes #42" in search_arg
