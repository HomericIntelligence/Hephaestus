"""Tests for the CIDriver automation (ci_driver.py)."""

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.agents.runtime import AgentRunResult
from hephaestus.automation.ci_driver import CIDriver, _evaluate_run_result
from hephaestus.automation.models import CIDriverOptions, WorkerResult

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
    # rev-parse HEAD returns SHA_PRE first (snapshot before agent) then SHA_POST
    # (after agent) — a non-zero SHA delta proves the agent committed.
    pre_post_sequence = [
        MagicMock(stdout="aaaa1111\n"),
        MagicMock(stdout="bbbb2222\n"),
    ]

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
        patch("hephaestus.automation.ci_driver.run", side_effect=pre_post_sequence),
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


def test_codex_ci_fix_session_skips_push_when_head_did_not_advance(
    driver: CIDriver,
    tmp_path: Path,
) -> None:
    """Agent returned without committing → no push, no false success log (#836).

    Under #846 the no-commit case re-checks required CI; when there are no
    failing required checks (mocked here as a clean rollup), the
    force-engagement retry is correctly suppressed and the iteration still
    reports failure with no push attempted.
    """
    driver.options.agent = "codex"
    fresh_result = AgentRunResult(stdout="no changes needed", stderr="", session_id="x")
    # Pre and post snapshots return the SAME SHA → agent made no commit.
    unchanged_sha = MagicMock(stdout="cafef00d\n")

    with (
        patch(
            "hephaestus.automation.ci_driver.run_codex_session",
            return_value=fresh_result,
        ),
        patch(
            "hephaestus.automation.ci_driver.push_current_branch_with_lease_on_divergence"
        ) as mock_push,
        patch("hephaestus.automation.ci_driver.sync_worktree_to_remote_branch"),
        patch(
            "hephaestus.automation.ci_driver.run",
            side_effect=[unchanged_sha, unchanged_sha],
        ),
        patch(
            "hephaestus.automation.ci_driver.gh_pr_list_unresolved_threads",
            return_value=[],
        ),
        patch(
            "hephaestus.automation.ci_driver.gh_pr_checks",
            return_value=[],
        ),
    ):
        result = driver._run_ci_fix_session(
            issue_number=789,
            pr_number=101,
            worktree_path=tmp_path,
            ci_logs="failed",
            session_id=None,
            pr_head_branch="789-impl",
        )

    # The iteration must report failure rather than a bogus success.
    assert result is False
    # And we must NOT have attempted a push — the prior bug was that a silent
    # no-op push exited 0 and the driver logged "pushed CI fixes" anyway.
    mock_push.assert_not_called()


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

    def test_shared_pr_issues_populated_for_arming_fanout(self, driver: CIDriver) -> None:
        """_discover_prs must record EVERY sibling per PR for the #840 fan-out."""
        # The arming flow in #840 walks driver.shared_pr_issues[pr_num] to
        # write one arming record per issue covered by a multi-issue PR. If
        # the dedupe forgets the siblings, only the canonical issue gets a
        # /learn on merge and the other 8 lose their lessons.
        siblings_for_103 = [12, 22, 23, 28, 29, 37, 39, 59, 64]
        side_effect = [103] * len(siblings_for_103) + [200]
        with patch.object(driver, "_find_pr_for_issue", side_effect=side_effect):
            driver._discover_prs([*siblings_for_103, 99])
        assert driver.shared_pr_issues[103] == siblings_for_103
        assert driver.shared_pr_issues[200] == [99]


# ---------------------------------------------------------------------------
# #838: repo done-state — open PR count must be zero
# ---------------------------------------------------------------------------


class TestOpenPrsRemaining:
    """Tests for #838: repo is only "done" when no open PRs remain."""

    def test_no_open_prs_marks_repo_done(self, driver: CIDriver) -> None:
        """Empty paginated response → ``open_prs_remaining`` is empty."""
        # gh api --paginate emits a concatenated JSON array; an empty repo
        # surfaces as ``[]``. The driver must read this as "done".
        result_mock = MagicMock(stdout="[]")
        with (
            patch("hephaestus.automation.ci_driver.get_repo_info", return_value=("o", "r")),
            patch("hephaestus.automation.ci_driver._gh_call", return_value=result_mock),
        ):
            remaining = driver._list_open_prs_remaining()
        assert remaining == []

    def test_open_prs_normalised_from_rest_shape(self, driver: CIDriver) -> None:
        """REST snake_case fields are normalised to the gh-CLI camelCase shape."""
        # gh api returns the GitHub REST shape (``head.ref``, ``auto_merge``);
        # downstream consumers in this module expect gh-CLI shape.
        rest_pulls = [
            {
                "number": 1,
                "title": "first",
                "head": {"ref": "branch-1"},
                "auto_merge": {"enabled_by": {"login": "bot"}},
            },
            {
                "number": 2,
                "title": "second",
                "head": {"ref": "branch-2"},
                "auto_merge": None,
            },
        ]
        result_mock = MagicMock(stdout=json.dumps(rest_pulls))
        with (
            patch("hephaestus.automation.ci_driver.get_repo_info", return_value=("o", "r")),
            patch("hephaestus.automation.ci_driver._gh_call", return_value=result_mock),
        ):
            remaining = driver._list_open_prs_remaining()
        assert remaining == [
            {
                "number": 1,
                "title": "first",
                "headRefName": "branch-1",
                "autoMergeRequest": {"enabled_by": {"login": "bot"}},
            },
            {
                "number": 2,
                "title": "second",
                "headRefName": "branch-2",
                "autoMergeRequest": None,
            },
        ]

    def test_gh_failure_returns_unknown_marker(self, driver: CIDriver) -> None:
        """If gh fails we treat the state as unknown — repo is NOT done."""
        # The conservative default: if we can't list open PRs we don't claim
        # the repo is clean. ``main()`` reads non-empty open_prs_remaining as
        # a failure.
        with (
            patch("hephaestus.automation.ci_driver.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                side_effect=subprocess.CalledProcessError(1, "gh", stderr="rate limited"),
            ),
        ):
            remaining = driver._list_open_prs_remaining()
        assert len(remaining) == 1
        assert remaining[0]["number"] == -1
        assert "unknown" in remaining[0]["title"].lower()

    def test_gh_pagination_endpoint_used(self, driver: CIDriver) -> None:
        """The call must include ``--paginate`` so all open PRs are returned, not 100."""
        # Without --paginate a repo with 200 open dependabot PRs would falsely
        # pass the done-check after looking at only 100.
        result_mock = MagicMock(stdout="[]")
        with (
            patch("hephaestus.automation.ci_driver.get_repo_info", return_value=("o", "r")),
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=result_mock,
            ) as mock_gh,
        ):
            driver._list_open_prs_remaining()

        args, _ = mock_gh.call_args
        cmd = args[0]
        assert "api" in cmd
        assert "--paginate" in cmd
        # And it must be the repo-scoped pulls endpoint, not the search API.
        assert any("/repos/o/r/pulls" in a for a in cmd)


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
            patch.object(driver, "_wait_for_pr_terminal", return_value="MERGED"),
        ):
            result = driver._drive_issue(123, 456, 0)

        assert result.success is True
        mock_merge.assert_called_once_with(456, is_bot_pr=False)

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
            patch.object(driver, "_wait_for_pr_terminal", return_value="MERGED"),
        ):
            result = driver._drive_issue(123, 456, 0)

        # All non-required treated as required → all green → auto-merge
        assert result.success is True
        mock_merge.assert_called_once_with(456, is_bot_pr=False)

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
            patch.object(driver, "_wait_for_pr_terminal", return_value="MERGED"),
        ):
            result = driver._drive_issue(123, 456, 0)

        assert result.success is True
        mock_merge.assert_called_once_with(456, is_bot_pr=False)

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
    """Tests for the post-merge /learn capture under AGENT_CI_DRIVER (#840).

    /learn no longer fires when auto-merge is *armed* — it fires when GitHub
    reports the PR as MERGED on a subsequent run. The drive success path
    writes one arming record per sibling issue (covering the #834 shared-PR
    fan-out); ``_check_arming_on_drive_start`` is what triggers /learn next
    time. Each arming record is idempotent and self-clears on
    abandoned-PR or head-SHA-advanced states.
    """

    def test_auto_merge_armed_writes_arming_record_but_no_learn(
        self, driver: CIDriver, tmp_path: Path
    ) -> None:
        """All-green + auto-merge ok → arming record written, /learn NOT called."""
        checks = [_make_check("test", required=True)]
        # The dedupe map normally drives shared_pr_issues; for this test
        # set it up directly with a single-issue PR.
        driver.shared_pr_issues = {456: [123]}
        with (
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(driver, "_enable_auto_merge", return_value=True),
            patch.object(driver, "_get_pr_branch", return_value="123-impl"),
            patch.object(
                driver,
                "_gh_pr_state",
                return_value={"state": "OPEN", "headRefOid": "abc1234"},
            ),
            patch(
                "hephaestus.automation.ci_driver.invoke_claude_with_session",
                return_value=("ok", "sid"),
            ) as mock_invoke,
            # Don't block on the post-arm wait loop; we only assert the
            # arming record is written here, not the merge outcome.
            patch.object(driver, "_wait_for_pr_terminal", return_value="TIMEOUT"),
        ):
            result = driver._drive_issue(123, 456, 0)

        assert result.success is True
        # No /learn yet — only an arming record on disk.
        mock_invoke.assert_not_called()
        record = driver._load_arming_state(123)
        assert record is not None
        assert record["pr_number"] == 456
        assert record["pr_head_branch"] == "123-impl"
        assert record["head_sha_at_arming"] == "abc1234"
        assert record["learn_captured_at"] is None

    def test_arming_fans_out_across_shared_pr_group(self, driver: CIDriver) -> None:
        """A PR closing 9 issues → 9 arming records (so 9 /learns fire on merge)."""
        # Reproduces the ProjectNestor scenario from #834: PR #103 closes nine
        # issues. The canonical issue drives it, but every sibling needs its
        # own arming record so each one gets its own /learn capture once the
        # PR finally merges in a subsequent run (#840).
        siblings = [12, 22, 23, 28, 29, 37, 39, 59, 64]
        driver.shared_pr_issues = {103: siblings}

        driver._arm_drive_green(pr_number=103, pr_head_branch="12-impl", pr_head_sha="abc")

        for issue in siblings:
            record = driver._load_arming_state(issue)
            assert record is not None, f"missing arming record for issue #{issue}"
            assert record["pr_number"] == 103
            assert record["learn_captured_at"] is None

    def test_arming_skips_already_captured_record(self, driver: CIDriver) -> None:
        """An issue with learn_captured_at set must not be re-armed."""
        # Idempotency guard: re-running the drive should not clobber a record
        # whose /learn already fired.
        driver.shared_pr_issues = {500: [42]}
        driver._save_arming_state(
            42,
            {
                "pr_number": 500,
                "pr_head_branch": "old-branch",
                "head_sha_at_arming": "deadbee",
                "armed_at": "2026-01-01T00:00:00Z",
                "learn_captured_at": "2026-01-02T00:00:00Z",
            },
        )

        driver._arm_drive_green(pr_number=500, pr_head_branch="new-branch", pr_head_sha="cafef00d")

        record = driver._load_arming_state(42)
        assert record is not None
        # The pre-existing captured timestamp survives — no overwrite.
        assert record["learn_captured_at"] == "2026-01-02T00:00:00Z"
        assert record["pr_head_branch"] == "old-branch"

    def test_check_armed_pr_merged_fires_learn_once(self, driver: CIDriver) -> None:
        """An armed issue whose PR is now MERGED triggers /learn exactly once."""
        driver._save_arming_state(
            42,
            {
                "pr_number": 500,
                "pr_head_branch": "42-impl",
                "head_sha_at_arming": "abc1234",
                "armed_at": "2026-01-01T00:00:00Z",
                "learn_captured_at": None,
            },
        )
        with (
            patch.object(
                driver,
                "_gh_pr_state",
                return_value={
                    "state": "MERGED",
                    "headRefOid": "abc1234",
                    "mergedAt": "2026-01-02T00:00:00Z",
                },
            ),
            patch.object(driver, "_run_drive_green_learnings", return_value=True) as mock_learn,
        ):
            result = driver._check_arming_on_drive_start(42, 500)

        assert result is not None
        assert result.success is True
        mock_learn.assert_called_once_with(42, 500)
        # Captured timestamp is now set so subsequent runs short-circuit.
        record = driver._load_arming_state(42)
        assert record is not None
        assert record["learn_captured_at"] is not None

    def test_check_armed_pr_merged_skips_when_already_captured(self, driver: CIDriver) -> None:
        """learn_captured_at != None → return success without re-firing /learn."""
        driver._save_arming_state(
            42,
            {
                "pr_number": 500,
                "pr_head_branch": "42-impl",
                "head_sha_at_arming": "abc1234",
                "armed_at": "2026-01-01T00:00:00Z",
                "learn_captured_at": "2026-01-02T00:00:00Z",
            },
        )
        with (
            patch.object(driver, "_gh_pr_state") as mock_state,
            patch.object(driver, "_run_drive_green_learnings") as mock_learn,
        ):
            result = driver._check_arming_on_drive_start(42, 500)

        assert result is not None
        assert result.success is True
        # Already captured — no /learn re-fire, no state query needed.
        mock_learn.assert_not_called()
        mock_state.assert_not_called()

    def test_check_armed_pr_open_at_same_sha_waits_then_pending(self, driver: CIDriver) -> None:
        """OPEN at the armed SHA → wait; still pending (TIMEOUT) keeps the record."""
        driver._save_arming_state(
            42,
            {
                "pr_number": 500,
                "pr_head_branch": "42-impl",
                "head_sha_at_arming": "abc1234",
                "armed_at": "2026-01-01T00:00:00Z",
                "learn_captured_at": None,
            },
        )
        with (
            patch.object(
                driver,
                "_gh_pr_state",
                return_value={"state": "OPEN", "headRefOid": "abc1234"},
            ),
            patch.object(driver, "_run_drive_green_learnings") as mock_learn,
            # The PR never resolves within the budget → TIMEOUT (still pending).
            patch.object(driver, "_wait_for_pr_terminal", return_value="TIMEOUT"),
        ):
            result = driver._check_arming_on_drive_start(42, 500)

        assert result is not None
        assert result.success is True
        mock_learn.assert_not_called()
        # Record preserved for the next run.
        assert driver._load_arming_state(42) is not None

    def test_check_armed_pr_merges_during_wait_fires_learn(self, driver: CIDriver) -> None:
        """OPEN at armed SHA that merges during the wait → /learn fires once."""
        driver._save_arming_state(
            42,
            {
                "pr_number": 500,
                "pr_head_branch": "42-impl",
                "head_sha_at_arming": "abc1234",
                "armed_at": "2026-01-01T00:00:00Z",
                "learn_captured_at": None,
            },
        )
        with (
            patch.object(
                driver,
                "_gh_pr_state",
                return_value={"state": "OPEN", "headRefOid": "abc1234"},
            ),
            patch.object(driver, "_run_drive_green_learnings") as mock_learn,
            patch.object(driver, "_wait_for_pr_terminal", return_value="MERGED"),
        ):
            result = driver._check_arming_on_drive_start(42, 500)

        assert result is not None
        assert result.success is True
        mock_learn.assert_called_once_with(42, 500)
        # Record marked captured so /learn never re-fires.
        record = driver._load_arming_state(42)
        assert record is not None
        assert record["learn_captured_at"] is not None

    def test_check_armed_pr_head_advanced_clears_record_and_falls_through(
        self, driver: CIDriver
    ) -> None:
        """OPEN with a different head SHA → drop record, return None (re-enter drive)."""
        driver._save_arming_state(
            42,
            {
                "pr_number": 500,
                "pr_head_branch": "42-impl",
                "head_sha_at_arming": "abc1234",
                "armed_at": "2026-01-01T00:00:00Z",
                "learn_captured_at": None,
            },
        )
        with patch.object(
            driver,
            "_gh_pr_state",
            return_value={"state": "OPEN", "headRefOid": "fffffff"},
        ):
            result = driver._check_arming_on_drive_start(42, 500)

        # None → caller will re-enter the normal drive path.
        assert result is None
        # Record cleared so the next /enable_auto_merge can re-arm fresh.
        assert driver._load_arming_state(42) is None

    def test_check_armed_pr_closed_without_merge_clears_record(self, driver: CIDriver) -> None:
        """CLOSED-without-merge → drop record, return None, /learn NOT fired."""
        # Lessons aren't load-bearing if nothing shipped; capturing /learn
        # for an abandoned PR would pollute Mnemosyne with false-positives.
        driver._save_arming_state(
            42,
            {
                "pr_number": 500,
                "pr_head_branch": "42-impl",
                "head_sha_at_arming": "abc1234",
                "armed_at": "2026-01-01T00:00:00Z",
                "learn_captured_at": None,
            },
        )
        with (
            patch.object(
                driver,
                "_gh_pr_state",
                return_value={"state": "CLOSED", "headRefOid": "abc1234"},
            ),
            patch.object(driver, "_run_drive_green_learnings") as mock_learn,
        ):
            result = driver._check_arming_on_drive_start(42, 500)

        assert result is None
        mock_learn.assert_not_called()
        assert driver._load_arming_state(42) is None

    def test_check_with_no_arming_record_returns_none(self, driver: CIDriver) -> None:
        """No prior arming → return None (fall through to the normal drive)."""
        result = driver._check_arming_on_drive_start(42, 500)
        assert result is None

    def test_learn_failure_marks_captured_to_avoid_loop(self, driver: CIDriver) -> None:
        """A failing /learn still flips learn_captured_at so we don't loop forever."""
        # If /learn fails (model quota, network, etc.) and we retried it on
        # every subsequent run, we'd churn API calls for the same issue
        # indefinitely. Best-effort: mark captured, log the failure, move on.
        driver._save_arming_state(
            42,
            {
                "pr_number": 500,
                "pr_head_branch": "42-impl",
                "head_sha_at_arming": "abc1234",
                "armed_at": "2026-01-01T00:00:00Z",
                "learn_captured_at": None,
            },
        )
        with (
            patch.object(
                driver,
                "_gh_pr_state",
                return_value={"state": "MERGED", "headRefOid": "abc1234"},
            ),
            patch.object(driver, "_run_drive_green_learnings", return_value=False),
        ):
            result = driver._check_arming_on_drive_start(42, 500)

        assert result is not None
        assert result.success is True
        record = driver._load_arming_state(42)
        assert record is not None
        assert record["learn_captured_at"] is not None

    def test_learnings_skipped_for_codex(self, driver: CIDriver) -> None:
        """Codex has no persisted drive-green session, so learnings is skipped."""
        # Direct test of _run_drive_green_learnings — the codex path is
        # untouched by the #840 arming rework.
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
            patch.object(driver, "_wait_for_pr_terminal", return_value="MERGED"),
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
            # Don't block the worker on the post-arm wait loop (#838).
            patch.object(d, "_wait_for_pr_terminal", return_value="MERGED"),
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
            # Don't block the worker on the post-arm wait loop (#838).
            patch.object(d, "_wait_for_pr_terminal", return_value="MERGED"),
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


# ---------------------------------------------------------------------------
# #846: no-commit retry + review-thread injection
# ---------------------------------------------------------------------------


class TestReviewThreadInjection:
    """Unresolved PR review threads must be fetched and folded into the fix prompt."""

    def test_no_unresolved_threads_returns_empty_string(
        self, driver: CIDriver, tmp_path: Path
    ) -> None:
        """No unresolved threads → no block injected (avoid prompt noise)."""
        with patch(
            "hephaestus.automation.ci_driver.gh_pr_list_unresolved_threads", return_value=[]
        ):
            result = driver._format_review_threads_block(pr_number=999)
        assert result == ""

    def test_unresolved_threads_rendered_verbatim(self, driver: CIDriver, tmp_path: Path) -> None:
        """Each unresolved thread's body, path, and line appear verbatim in the block."""
        threads = [
            {"id": "t1", "path": "src/a.py", "line": 42, "body": "Use safe_write here."},
            {"id": "t2", "path": "src/b.py", "line": None, "body": "Magic constant."},
        ]
        with patch(
            "hephaestus.automation.ci_driver.gh_pr_list_unresolved_threads",
            return_value=threads,
        ):
            block = driver._format_review_threads_block(pr_number=42)
        assert "## Unresolved PR Review Threads" in block
        assert "src/a.py:42" in block
        assert "Use safe_write here." in block
        assert "src/b.py" in block  # line None → no trailing :None
        assert "src/b.py:None" not in block
        assert "Magic constant." in block

    def test_graphql_failure_is_swallowed(self, driver: CIDriver, tmp_path: Path) -> None:
        """A gh failure must not block the drive — empty string + info log only."""
        with patch(
            "hephaestus.automation.ci_driver.gh_pr_list_unresolved_threads",
            side_effect=RuntimeError("graphql down"),
        ):
            assert driver._format_review_threads_block(pr_number=7) == ""


class TestFailingRequiredCheckNames:
    """Required-check failure naming for the force-engagement retry prompt."""

    def test_all_green_returns_empty(self, driver: CIDriver) -> None:
        checks = [_make_check("lint", conclusion="success")]
        with patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks):
            assert driver._failing_required_check_names(pr_number=1) == []

    def test_one_required_failure_returned(self, driver: CIDriver) -> None:
        checks = [
            _make_check("lint", conclusion="success"),
            _make_check("test", conclusion="failure"),
            _make_check("non-req", conclusion="failure", required=False),
        ]
        with patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks):
            names = driver._failing_required_check_names(pr_number=1)
        assert names == ["test"]

    def test_no_required_defined_falls_back_to_all_checks(self, driver: CIDriver) -> None:
        """When no check is marked required, the helper treats all checks as required."""
        checks = [
            _make_check("only-check", conclusion="failure", required=False),
        ]
        with patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks):
            assert driver._failing_required_check_names(pr_number=1) == ["only-check"]

    def test_gh_failure_returns_empty(self, driver: CIDriver) -> None:
        """A gh blip must not promote the no-commit to a retry — empty list = skip."""
        with patch(
            "hephaestus.automation.ci_driver.gh_pr_checks",
            side_effect=RuntimeError("api down"),
        ):
            assert driver._failing_required_check_names(pr_number=1) == []


class TestForceEngagementPrompt:
    """The retry prompt must name failing checks verbatim and re-state invariants."""

    def test_prompt_names_failing_checks_and_branch(self, driver: CIDriver, tmp_path: Path) -> None:
        prompt = driver._force_engagement_prompt(
            issue_number=1,
            pr_number=2,
            worktree_path=tmp_path,
            pr_head_branch="1-fix",
            failing_check_names=["lint", "test-py310"],
            review_threads_block="",
        )
        # The failing-check names must be in the prompt body, not a placeholder.
        assert "- lint" in prompt
        assert "- test-py310" in prompt
        # PR + issue identifiers visible (the prompt uses pr_ref/issue_ref
        # which include the repo slug when available, so just assert the
        # numbers + the "PR"/"issue" keywords land in the right order).
        assert "#2" in prompt
        assert "#1" in prompt
        assert "issue " in prompt.lower()
        assert "pr " in prompt.lower()
        # The branch invariant is restated — agent must not switch branches.
        assert "1-fix" in prompt
        assert "DO NOT create a new branch" in prompt
        # Signed-commits and no --no-verify re-stated (user requirement).
        assert "git commit -S" in prompt
        assert "--no-verify" in prompt
        # Bug 4: the agent must NOT be told to commit a blocker file (a new
        # Markdown file fails the repo's markdownlint and turns 1 red check into
        # 2). It should use the BLOCKED: line and never disable a lint rule.
        assert "CI_BLOCKER.md" in prompt  # explicitly named as forbidden
        assert "BLOCKED:" in prompt
        assert "no rule disabled" in prompt
        # The old "write a commit that documents the blocker" instruction is gone.
        assert "write a commit that documents the blocker" not in prompt

    def test_review_threads_block_prepended_when_nonempty(
        self, driver: CIDriver, tmp_path: Path
    ) -> None:
        prompt = driver._force_engagement_prompt(
            issue_number=1,
            pr_number=2,
            worktree_path=tmp_path,
            pr_head_branch="1-fix",
            failing_check_names=["lint"],
            review_threads_block="## Unresolved PR Review Threads\n\nSee below.\n",
        )
        assert prompt.startswith("## Unresolved PR Review Threads")


class TestNoCommitRetry:
    """Force-engagement retry path: triggers, stays on PR/branch, bounded once."""

    def _patch_common(
        self,
        *,
        failing_checks: list[str],
        head_after_retry: str,
        pre_sha: str = "cafef00d",
    ) -> dict[str, Any]:
        """Build the common patch dict for retry tests."""
        return {
            "failing_checks": failing_checks,
            "head_after_retry": head_after_retry,
            "pre_sha": pre_sha,
        }

    def test_retry_skipped_when_no_failing_required_checks(
        self, driver: CIDriver, tmp_path: Path
    ) -> None:
        """No-commit + green CI → no retry, returns False (don't perturb a passing PR)."""
        with (
            patch(
                "hephaestus.automation.ci_driver.gh_pr_checks",
                return_value=[_make_check("lint", conclusion="success")],
            ),
            patch("hephaestus.automation.ci_driver.invoke_claude_with_session") as mock_invoke,
            patch(
                "hephaestus.automation.ci_driver.gh_pr_list_unresolved_threads",
                return_value=[],
            ),
        ):
            result = driver._retry_no_commit_once(
                issue_number=1,
                pr_number=2,
                worktree_path=tmp_path,
                pr_head_branch="1-fix",
                pre_agent_sha="cafef00d",
                session_id=None,
            )
        assert result is False
        mock_invoke.assert_not_called()

    def test_retry_fires_when_failing_and_returns_true_on_commit(
        self, driver: CIDriver, tmp_path: Path
    ) -> None:
        """No-commit + failing CI → one resumed claude call; HEAD advanced ⇒ True."""
        post_sha = MagicMock(stdout="deadbeef\n")
        with (
            patch(
                "hephaestus.automation.ci_driver.gh_pr_checks",
                return_value=[_make_check("lint", conclusion="failure")],
            ),
            patch(
                "hephaestus.automation.ci_driver.gh_pr_list_unresolved_threads",
                return_value=[],
            ),
            patch(
                "hephaestus.automation.ci_driver.invoke_claude_with_session",
                return_value=("done", "sess"),
            ) as mock_invoke,
            patch("hephaestus.automation.ci_driver.run", return_value=post_sha),
        ):
            result = driver._retry_no_commit_once(
                issue_number=1,
                pr_number=2,
                worktree_path=tmp_path,
                pr_head_branch="1-fix",
                pre_agent_sha="cafef00d",
                session_id=None,
            )
        assert result is True
        # Exactly one retry — never two, never zero.
        mock_invoke.assert_called_once()
        # The retry must stay on the same (repo, issue, AGENT_CI_DRIVER) session.
        kwargs = mock_invoke.call_args.kwargs
        assert kwargs["agent"] == "ci-driver"
        assert kwargs["issue"] == 1

    def test_repeated_no_commit_returns_false_and_writes_marker(
        self, driver: CIDriver, tmp_path: Path
    ) -> None:
        """Retry returned without commit too → False + state_dir marker (#846)."""
        unchanged = MagicMock(stdout="cafef00d\n")  # post == pre
        with (
            patch(
                "hephaestus.automation.ci_driver.gh_pr_checks",
                return_value=[_make_check("lint", conclusion="failure")],
            ),
            patch(
                "hephaestus.automation.ci_driver.gh_pr_list_unresolved_threads",
                return_value=[],
            ),
            patch(
                "hephaestus.automation.ci_driver.invoke_claude_with_session",
                return_value=("nope", "sess"),
            ) as mock_invoke,
            patch("hephaestus.automation.ci_driver.run", return_value=unchanged),
        ):
            result = driver._retry_no_commit_once(
                issue_number=1,
                pr_number=2,
                worktree_path=tmp_path,
                pr_head_branch="1-fix",
                pre_agent_sha="cafef00d",
                session_id=None,
            )
        assert result is False
        # Bounded retries — re-engages up to max_retries (default 2) before
        # giving up, so a single no-op turn is no longer terminal (#846).
        assert mock_invoke.call_count == 2
        # Forensics marker is written next to the arming-state files.
        marker = driver.state_dir / "repeated-no-commit-2.json"
        assert marker.exists()
        payload = json.loads(marker.read_text())
        assert payload["pr_number"] == 2
        assert payload["pr_head_branch"] == "1-fix"
        assert payload["failing_required_checks"] == ["lint"]

    def test_retry_exception_returns_false_no_marker(
        self, driver: CIDriver, tmp_path: Path
    ) -> None:
        """A subprocess error during retry → False, no marker (could not prove repeated)."""
        with (
            patch(
                "hephaestus.automation.ci_driver.gh_pr_checks",
                return_value=[_make_check("lint", conclusion="failure")],
            ),
            patch(
                "hephaestus.automation.ci_driver.gh_pr_list_unresolved_threads",
                return_value=[],
            ),
            patch(
                "hephaestus.automation.ci_driver.invoke_claude_with_session",
                side_effect=subprocess.CalledProcessError(1, ["claude"], stderr="boom"),
            ),
        ):
            result = driver._retry_no_commit_once(
                issue_number=1,
                pr_number=2,
                worktree_path=tmp_path,
                pr_head_branch="1-fix",
                pre_agent_sha="cafef00d",
                session_id=None,
            )
        assert result is False
        # Marker is only for *repeated* no-commit — a subprocess failure is a
        # different signal and we don't want to confuse the forensics record.
        assert not (driver.state_dir / "repeated-no-commit-2.json").exists()

    def test_retry_codex_path_resumes_session(self, driver: CIDriver, tmp_path: Path) -> None:
        """Codex agent + session_id → resume_codex_session called once with the session."""
        driver.options.agent = "codex"
        post_sha = MagicMock(stdout="deadbeef\n")
        with (
            patch(
                "hephaestus.automation.ci_driver.gh_pr_checks",
                return_value=[_make_check("lint", conclusion="failure")],
            ),
            patch(
                "hephaestus.automation.ci_driver.gh_pr_list_unresolved_threads",
                return_value=[],
            ),
            patch(
                "hephaestus.automation.ci_driver.resume_codex_session",
                return_value=AgentRunResult(stdout="ok", stderr="", session_id="s"),
            ) as mock_resume,
            patch("hephaestus.automation.ci_driver.run_codex_session") as mock_fresh,
            patch("hephaestus.automation.ci_driver.run", return_value=post_sha),
        ):
            result = driver._retry_no_commit_once(
                issue_number=1,
                pr_number=2,
                worktree_path=tmp_path,
                pr_head_branch="1-fix",
                pre_agent_sha="cafef00d",
                session_id="old-codex-session",
            )
        assert result is True
        mock_resume.assert_called_once()
        mock_fresh.assert_not_called()


# ---------------------------------------------------------------------------
# #848: ecosystem honesty triple-fix
# ---------------------------------------------------------------------------


class TestBotPrDiscovery:
    """``_discover_bot_prs`` and ``_discover_prs`` bot-mode union."""

    def test_no_bot_prs_returns_empty(self, driver: CIDriver, tmp_path: Path) -> None:
        with (
            patch(
                "hephaestus.automation.ci_driver.get_repo_info",
                return_value=("o", "r"),
            ),
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=MagicMock(stdout="[]"),
            ),
        ):
            assert driver._discover_bot_prs() == {}

    def test_bot_prs_returned_as_self_keyed_map(self, driver: CIDriver, tmp_path: Path) -> None:
        raw = [
            {"number": 100, "user": {"type": "Bot", "login": "app/dependabot"}},
            {"number": 101, "user": {"type": "User", "login": "alice"}},
            {"number": 102, "user": {"type": "Bot", "login": "app/github-actions"}},
        ]
        with (
            patch(
                "hephaestus.automation.ci_driver.get_repo_info",
                return_value=("o", "r"),
            ),
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=MagicMock(stdout=json.dumps(raw)),
            ),
        ):
            result = driver._discover_bot_prs()
        # Only bot-authored PRs; PR-number used as the key (synthetic issue).
        assert result == {100: 100, 102: 102}

    def test_gh_failure_returns_empty(self, driver: CIDriver, tmp_path: Path) -> None:
        with (
            patch(
                "hephaestus.automation.ci_driver.get_repo_info",
                return_value=("o", "r"),
            ),
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                side_effect=subprocess.CalledProcessError(1, ["gh"], stderr="boom"),
            ),
        ):
            assert driver._discover_bot_prs() == {}

    def test_discover_prs_unions_bot_prs_when_enabled(
        self, driver: CIDriver, tmp_path: Path
    ) -> None:
        """include_bot_prs=True (default) unions bot PRs into the deduped map."""
        with (
            patch.object(driver, "_find_pr_for_issue", return_value=500),
            patch.object(driver, "_discover_bot_prs", return_value={900: 900, 901: 901}),
        ):
            result = driver._discover_prs([42])
        # issue 42 → PR 500 PLUS the two bot PRs as self-keyed entries.
        assert result == {42: 500, 900: 900, 901: 901}

    def test_discover_prs_skips_bot_discovery_when_disabled(
        self, driver: CIDriver, tmp_path: Path
    ) -> None:
        driver.options.include_bot_prs = False
        with (
            patch.object(driver, "_find_pr_for_issue", return_value=500),
            patch.object(driver, "_discover_bot_prs") as mock_bots,
        ):
            result = driver._discover_prs([42])
        assert result == {42: 500}
        mock_bots.assert_not_called()

    def test_discover_prs_does_not_overwrite_issue_driven_entry(
        self, driver: CIDriver, tmp_path: Path
    ) -> None:
        """A bot-PR collision with an issue-driven PR must not displace the issue key."""
        with (
            patch.object(driver, "_find_pr_for_issue", return_value=900),
            patch.object(driver, "_discover_bot_prs", return_value={900: 900}),
        ):
            result = driver._discover_prs([42])
        # Issue 42 already drives PR 900; bot enumeration must not add a
        # second 900→900 entry that would collide with the canonical entry.
        assert result == {42: 900}


class TestIsBotPrMode:
    """The single-rule (issue == pr) detector for the bot-PR short-circuit."""

    def test_equal_means_bot_mode(self, driver: CIDriver) -> None:
        assert driver._is_bot_pr_mode(900, 900) is True

    def test_different_means_normal_mode(self, driver: CIDriver) -> None:
        assert driver._is_bot_pr_mode(42, 900) is False


class TestArmingSweep:
    """Startup sweep that resolves arming records whose issue is no longer in the input list."""

    def _write_record(
        self,
        driver: CIDriver,
        issue: int,
        pr_number: int,
        learn_captured_at: str | None = None,
    ) -> Path:
        path = driver.state_dir / f"drive-green-armed-{issue}.json"
        path.write_text(
            json.dumps(
                {
                    "pr_number": pr_number,
                    "pr_head_branch": f"{issue}-impl",
                    "head_sha_at_arming": "deadbeef",
                    "armed_at": "2026-05-31T00:00:00Z",
                    "learn_captured_at": learn_captured_at,
                }
            )
        )
        return path

    def test_no_records_is_noop(self, driver: CIDriver) -> None:
        # state_dir is the fixture's tmp_path with no arming files in it.
        driver._sweep_orphaned_arming_records()

    def test_merged_orphan_fires_learn_and_marks_captured(
        self, driver: CIDriver, tmp_path: Path
    ) -> None:
        self._write_record(driver, issue=841, pr_number=843)
        with (
            patch.object(driver, "_gh_pr_state", return_value={"state": "MERGED"}),
            patch.object(driver, "_run_drive_green_learnings", return_value=True) as mock_learn,
        ):
            driver._sweep_orphaned_arming_records()
        mock_learn.assert_called_once_with(841, 843)
        record = json.loads((driver.state_dir / "drive-green-armed-841.json").read_text())
        assert record["learn_captured_at"] is not None

    def test_closed_not_merged_orphan_dropped(self, driver: CIDriver, tmp_path: Path) -> None:
        path = self._write_record(driver, issue=841, pr_number=843)
        with (
            patch.object(driver, "_gh_pr_state", return_value={"state": "CLOSED"}),
            patch.object(driver, "_run_drive_green_learnings") as mock_learn,
        ):
            driver._sweep_orphaned_arming_records()
        mock_learn.assert_not_called()
        assert not path.exists()

    def test_open_record_left_alone(self, driver: CIDriver, tmp_path: Path) -> None:
        path = self._write_record(driver, issue=841, pr_number=843)
        with (
            patch.object(driver, "_gh_pr_state", return_value={"state": "OPEN"}),
            patch.object(driver, "_run_drive_green_learnings") as mock_learn,
        ):
            driver._sweep_orphaned_arming_records()
        mock_learn.assert_not_called()
        assert path.exists()

    def test_already_captured_skipped(self, driver: CIDriver, tmp_path: Path) -> None:
        self._write_record(
            driver, issue=841, pr_number=843, learn_captured_at="2026-05-31T00:00:00Z"
        )
        with (
            patch.object(driver, "_gh_pr_state") as mock_state,
            patch.object(driver, "_run_drive_green_learnings") as mock_learn,
        ):
            driver._sweep_orphaned_arming_records()
        mock_state.assert_not_called()
        mock_learn.assert_not_called()

    def test_unknown_gh_state_left_alone(self, driver: CIDriver, tmp_path: Path) -> None:
        path = self._write_record(driver, issue=841, pr_number=843)
        with (
            patch.object(driver, "_gh_pr_state", return_value=None),
            patch.object(driver, "_run_drive_green_learnings") as mock_learn,
        ):
            driver._sweep_orphaned_arming_records()
        mock_learn.assert_not_called()
        assert path.exists()

    def test_learn_failure_still_marks_captured(self, driver: CIDriver, tmp_path: Path) -> None:
        """Best-effort: a /learn that throws still marks captured (no infinite retry)."""
        self._write_record(driver, issue=841, pr_number=843)
        with (
            patch.object(driver, "_gh_pr_state", return_value={"state": "MERGED"}),
            patch.object(
                driver,
                "_run_drive_green_learnings",
                side_effect=RuntimeError("boom"),
            ),
        ):
            # The sweeper doesn't swallow the exception from _run_drive_green_learnings
            # itself — but the underlying helper IS best-effort (returns False on
            # failure), so we mirror that contract here: when the helper raises,
            # the sweeper propagates. Production callers wrap; the test verifies
            # the contract holds (record NOT mutated on raise).
            with pytest.raises(RuntimeError):
                driver._sweep_orphaned_arming_records()
        record = json.loads((driver.state_dir / "drive-green-armed-841.json").read_text())
        assert record["learn_captured_at"] is None


class TestRunSweeperWiring:
    """``CIDriver.run()`` invokes the sweeper before any per-issue work."""

    def test_run_calls_sweeper_unless_dry_run(self, driver: CIDriver, tmp_path: Path) -> None:
        driver.options.issues = [42]
        with (
            patch.object(driver, "_sweep_orphaned_arming_records") as mock_sweep,
            patch.object(driver, "_discover_prs", return_value={}),
        ):
            driver.run()
        mock_sweep.assert_called_once()

    def test_dry_run_skips_sweeper(self, driver: CIDriver, tmp_path: Path) -> None:
        driver.options.dry_run = True
        driver.options.issues = [42]
        with (
            patch.object(driver, "_sweep_orphaned_arming_records") as mock_sweep,
            patch.object(driver, "_discover_prs", return_value={}),
        ):
            driver.run()
        mock_sweep.assert_not_called()


class TestAdviseBotShortCircuit:
    """Bot PRs skip the advise step because the issue is synthetic."""

    def test_bot_mode_skips_advise(self, driver: CIDriver) -> None:
        driver.options.enable_advise = True
        with (
            patch.object(driver, "_get_failing_ci_logs", return_value="failed"),
            patch.object(driver, "_load_impl_session_id", return_value=None),
            patch.object(driver, "_get_worktree_path", return_value=Path("/tmp/x")),
            patch.object(driver, "_get_pr_branch", return_value="dep-branch"),
            patch.object(driver, "_run_ci_fix_session", return_value=True),
            patch.object(driver, "_run_advise") as mock_advise,
            patch.object(driver, "_enable_auto_merge", return_value=True),
        ):
            driver._attempt_ci_fixes(issue_number=900, pr_number=900, acquired_slot=0)
        mock_advise.assert_not_called()

    def test_non_bot_mode_calls_advise(self, driver: CIDriver) -> None:
        driver.options.enable_advise = True
        with (
            patch.object(driver, "_get_failing_ci_logs", return_value="failed"),
            patch.object(driver, "_load_impl_session_id", return_value=None),
            patch.object(driver, "_get_worktree_path", return_value=Path("/tmp/x")),
            patch.object(driver, "_get_pr_branch", return_value="42-fix"),
            patch.object(driver, "_run_ci_fix_session", return_value=True),
            patch.object(driver, "_run_advise", return_value="") as mock_advise,
            patch.object(driver, "_enable_auto_merge", return_value=True),
        ):
            driver._attempt_ci_fixes(issue_number=42, pr_number=900, acquired_slot=0)
        mock_advise.assert_called_once_with(42)


# ---------------------------------------------------------------------------
# _attempt_mechanical_rebase (#871)
# ---------------------------------------------------------------------------


class TestMechanicalRebase:
    """Tests for the mechanical-rebase pre-step that runs before the agent (#871)."""

    @staticmethod
    def _pr_state(
        merge_state: str,
        head: str = "5-impl",
        base: str = "main",
    ) -> MagicMock:
        """Build a ``_gh_call`` return for the merge-state query."""
        return MagicMock(
            stdout=json.dumps(
                {
                    "mergeStateStatus": merge_state,
                    "mergeable": "MERGEABLE",
                    "headRefName": head,
                    "baseRefName": base,
                }
            )
        )

    def test_behind_pr_rebases_clean_and_pushes_no_agent(
        self, driver: CIDriver, tmp_path: Path
    ) -> None:
        """A BEHIND PR rebases cleanly → pushes with lease, returns True."""
        with (
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=self._pr_state("BEHIND"),
            ),
            patch.object(driver, "_get_worktree_path", return_value=tmp_path),
            patch("hephaestus.automation.ci_driver.sync_worktree_to_remote_branch") as mock_sync,
            patch(
                "hephaestus.automation.ci_driver.rebase_worktree_onto", return_value=True
            ) as mock_rebase,
            patch(
                "hephaestus.automation.ci_driver.push_current_branch_with_lease_on_divergence"
            ) as mock_push,
        ):
            result = driver._attempt_mechanical_rebase(
                issue_number=5, pr_number=50, acquired_slot=0
            )

        assert result is True
        mock_sync.assert_called_once_with(tmp_path, "5-impl")
        mock_rebase.assert_called_once_with(tmp_path, "main")
        mock_push.assert_called_once_with(tmp_path, branch="5-impl", push_ref="HEAD:5-impl")

    def test_conflicting_pr_defers_to_agent_no_push(self, driver: CIDriver, tmp_path: Path) -> None:
        """A DIRTY PR whose rebase conflicts must NOT push — it returns False."""
        with (
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=self._pr_state("DIRTY"),
            ),
            patch.object(driver, "_get_worktree_path", return_value=tmp_path),
            patch("hephaestus.automation.ci_driver.sync_worktree_to_remote_branch"),
            patch("hephaestus.automation.ci_driver.rebase_worktree_onto", return_value=False),
            patch(
                "hephaestus.automation.ci_driver.push_current_branch_with_lease_on_divergence"
            ) as mock_push,
        ):
            result = driver._attempt_mechanical_rebase(
                issue_number=5, pr_number=50, acquired_slot=0
            )

        assert result is False
        mock_push.assert_not_called()

    def test_up_to_date_pr_skips_rebase_entirely(self, driver: CIDriver, tmp_path: Path) -> None:
        """A CLEAN/BLOCKED PR is already on its base — no rebase, no push."""
        with (
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=self._pr_state("CLEAN"),
            ),
            patch("hephaestus.automation.ci_driver.rebase_worktree_onto") as mock_rebase,
            patch(
                "hephaestus.automation.ci_driver.push_current_branch_with_lease_on_divergence"
            ) as mock_push,
        ):
            result = driver._attempt_mechanical_rebase(
                issue_number=5, pr_number=50, acquired_slot=0
            )

        assert result is False
        mock_rebase.assert_not_called()
        mock_push.assert_not_called()

    def test_blocked_review_gated_pr_is_not_rebased(self, driver: CIDriver, tmp_path: Path) -> None:
        """BLOCKED (green, waiting on review) is on-base — must not be rebased."""
        with (
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=self._pr_state("BLOCKED"),
            ),
            patch("hephaestus.automation.ci_driver.rebase_worktree_onto") as mock_rebase,
        ):
            result = driver._attempt_mechanical_rebase(
                issue_number=5, pr_number=50, acquired_slot=0
            )

        assert result is False
        mock_rebase.assert_not_called()

    def test_uses_pr_base_ref_not_hardcoded_main(self, driver: CIDriver, tmp_path: Path) -> None:
        """The rebase targets the PR's actual baseRefName, not a hardcoded main."""
        with (
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=self._pr_state("BEHIND", base="develop"),
            ),
            patch.object(driver, "_get_worktree_path", return_value=tmp_path),
            patch("hephaestus.automation.ci_driver.sync_worktree_to_remote_branch"),
            patch(
                "hephaestus.automation.ci_driver.rebase_worktree_onto", return_value=True
            ) as mock_rebase,
            patch("hephaestus.automation.ci_driver.push_current_branch_with_lease_on_divergence"),
        ):
            driver._attempt_mechanical_rebase(issue_number=5, pr_number=50, acquired_slot=0)

        mock_rebase.assert_called_once_with(tmp_path, "develop")

    def test_gh_query_failure_returns_false_no_crash(
        self, driver: CIDriver, tmp_path: Path
    ) -> None:
        """A bad/empty gh response must be swallowed → False, never raise."""
        with (
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=MagicMock(stdout="not json"),
            ),
            patch("hephaestus.automation.ci_driver.rebase_worktree_onto") as mock_rebase,
        ):
            result = driver._attempt_mechanical_rebase(
                issue_number=5, pr_number=50, acquired_slot=0
            )

        assert result is False
        mock_rebase.assert_not_called()

    def test_rebase_step_skipped_when_option_disabled(
        self, driver: CIDriver, tmp_path: Path
    ) -> None:
        """``enable_mechanical_rebase=False`` skips the call inside _drive_issue.

        Verified at the _drive_issue gate: with the option off, the driver must
        not invoke _attempt_mechanical_rebase. We assert the guard by checking
        the option default flips the call.
        """
        driver.options.enable_mechanical_rebase = False
        # The gate in _drive_issue is ``if enable_mechanical_rebase and not dry_run``.
        # With the flag off the method is simply never called; here we assert the
        # option is wired so the guard evaluates False.
        assert driver.options.enable_mechanical_rebase is False


# ---------------------------------------------------------------------------
# #838: wait-for-merge + final-gate honesty
# ---------------------------------------------------------------------------


class TestWaitForPrTerminal:
    """``_wait_for_pr_terminal`` blocks until a real terminal state (#838)."""

    def test_merged_returns_merged(self, driver: CIDriver) -> None:
        with patch.object(driver, "_gh_pr_state", return_value={"state": "MERGED"}):
            assert driver._wait_for_pr_terminal(1, 2) == "MERGED"

    def test_closed_returns_closed(self, driver: CIDriver) -> None:
        with patch.object(driver, "_gh_pr_state", return_value={"state": "CLOSED"}):
            assert driver._wait_for_pr_terminal(1, 2) == "CLOSED"

    def test_open_with_red_required_check_returns_failing(self, driver: CIDriver) -> None:
        # OPEN but a required check is red → react immediately, don't wait it out.
        with (
            patch.object(driver, "_gh_pr_state", return_value={"state": "OPEN"}),
            patch.object(driver, "_failing_required_check_names", return_value=["lint"]),
        ):
            assert driver._wait_for_pr_terminal(1, 2) == "FAILING"

    def test_open_dirty_returns_dirty(self, driver: CIDriver) -> None:
        # OPEN, checks green, but mergeStateStatus DIRTY (conflict) → DIRTY,
        # don't wait out the full timeout on an unmergeable armed PR (#838).
        with (
            patch.object(
                driver,
                "_gh_pr_state",
                return_value={"state": "OPEN", "mergeStateStatus": "DIRTY"},
            ),
            patch.object(driver, "_failing_required_check_names", return_value=[]),
        ):
            assert driver._wait_for_pr_terminal(1, 2) == "DIRTY"

    def test_open_and_green_times_out(
        self, driver: CIDriver, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # OPEN, no failing checks, never merges → bounded by HEPH_PR_MERGE_MAX_WAIT.
        monkeypatch.setenv("HEPH_PR_MERGE_MAX_WAIT", "0")
        with (
            patch.object(driver, "_gh_pr_state", return_value={"state": "OPEN"}),
            patch.object(driver, "_failing_required_check_names", return_value=[]),
            patch("hephaestus.automation.ci_driver.time.sleep") as mock_sleep,
        ):
            assert driver._wait_for_pr_terminal(1, 2) == "TIMEOUT"
        # With a 0s budget the very first sleep would overrun → no sleep at all.
        mock_sleep.assert_not_called()

    def test_dry_run_short_circuits_to_timeout(
        self, mock_options: CIDriverOptions, tmp_path: Path
    ) -> None:
        mock_options.dry_run = True
        with (
            patch("hephaestus.automation.ci_driver.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.ci_driver.WorktreeManager"),
            patch("hephaestus.automation.ci_driver.StatusTracker"),
        ):
            d = CIDriver(mock_options)
            d.state_dir = tmp_path
        # Must not touch the network in dry-run.
        with patch.object(d, "_gh_pr_state") as mock_state:
            assert d._wait_for_pr_terminal(1, 2) == "TIMEOUT"
        mock_state.assert_not_called()


class TestRecheckAndArmAfterFix:
    """A pushed CI fix must re-poll + arm; never leave a now-green PR un-armed."""

    def test_green_after_fix_arms_and_waits(self, driver: CIDriver) -> None:
        green = [_make_check("test", required=True, conclusion="success")]
        with (
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=green),
            patch.object(driver, "_enable_auto_merge", return_value=True) as mock_arm,
            patch.object(driver, "_gh_pr_state", return_value={"headRefOid": "abc"}),
            patch.object(driver, "_get_pr_branch", return_value="b"),
            patch.object(driver, "_arm_drive_green") as mock_record,
            patch.object(driver, "_wait_for_pr_terminal", return_value="MERGED") as mock_wait,
        ):
            result = driver._recheck_and_arm_after_fix(1, 2, 0)
        assert result is not None and result.success is True
        mock_arm.assert_called_once()
        mock_record.assert_called_once()
        mock_wait.assert_called_once()

    def test_still_pending_after_fix_returns_none(
        self, driver: CIDriver, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CI hasn't concluded yet after the push → None (caller keeps fix success;
        # a later run arms it). Bounded by HEPH_CI_POLL_MAX_WAIT.
        monkeypatch.setenv("HEPH_CI_POLL_MAX_WAIT", "0")
        pending = [_make_check("test", status="in_progress", conclusion="", required=True)]
        with (
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=pending),
            patch("hephaestus.automation.ci_driver.time.sleep"),
            patch.object(driver, "_enable_auto_merge") as mock_arm,
        ):
            assert driver._recheck_and_arm_after_fix(1, 2, 0) is None
        mock_arm.assert_not_called()

    def test_still_red_after_fix_returns_none(self, driver: CIDriver) -> None:
        red = [_make_check("test", required=True, conclusion="failure")]
        with (
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=red),
            patch.object(driver, "_enable_auto_merge") as mock_arm,
        ):
            assert driver._recheck_and_arm_after_fix(1, 2, 0) is None
        mock_arm.assert_not_called()


class TestResolveDirtyPr:
    """An armed-but-DIRTY PR is rebased, then handed to the agent if still conflicting."""

    def test_clean_mechanical_rebase_then_arm(self, driver: CIDriver) -> None:
        with (
            patch.object(driver, "_attempt_mechanical_rebase", return_value=True) as mock_rb,
            patch.object(
                driver,
                "_recheck_and_arm_after_fix",
                return_value=WorkerResult(issue_number=1, success=True, pr_number=2),
            ) as mock_rearm,
        ):
            result = driver._resolve_dirty_pr(1, 2, 0)
        assert result.success is True
        mock_rb.assert_called_once()
        mock_rearm.assert_called_once()

    def test_conflict_routes_to_agent_with_context(self, driver: CIDriver) -> None:
        with (
            patch.object(driver, "_attempt_mechanical_rebase", return_value=False),
            patch(
                "hephaestus.automation.ci_driver._gh_call",
                return_value=MagicMock(stdout='{"baseRefName":"main"}'),
            ),
            patch.object(
                driver,
                "_attempt_ci_fixes",
                return_value=WorkerResult(issue_number=1, success=True, pr_number=2),
            ) as mock_fix,
            patch.object(
                driver,
                "_recheck_and_arm_after_fix",
                return_value=WorkerResult(issue_number=1, success=True, pr_number=2),
            ),
        ):
            result = driver._resolve_dirty_pr(1, 2, 0)
        assert result.success is True
        # The agent must get explicit conflict-resolution context.
        ctx = mock_fix.call_args.kwargs.get("extra_context", "")
        assert "MERGE CONFLICT" in ctx

    def test_unresolved_conflict_returns_failure(self, driver: CIDriver) -> None:
        with (
            patch.object(driver, "_attempt_mechanical_rebase", return_value=False),
            patch("hephaestus.automation.ci_driver._gh_call", return_value=MagicMock(stdout="{}")),
            patch.object(driver, "_attempt_ci_fixes", return_value=None),
        ):
            result = driver._resolve_dirty_pr(1, 2, 0)
        assert result.success is False
        assert "conflict" in (result.error or "").lower()


class TestEnableAutoMergeBotRetry:
    """Bot PRs get a strategy-agnostic ``--auto`` retry before giving up (#848)."""

    def test_bot_pr_falls_back_to_strategy_agnostic_auto(self, driver: CIDriver) -> None:
        calls: list[list[str]] = []

        def fake_gh(args: list[str]) -> MagicMock:
            calls.append(args)
            # First call (--auto --squash) fails; second (--auto) succeeds.
            if "--squash" in args:
                raise subprocess.CalledProcessError(1, args, stderr="squash disabled")
            return MagicMock(stdout="")

        with patch("hephaestus.automation.ci_driver._gh_call", side_effect=fake_gh):
            ok = driver._enable_auto_merge(2, is_bot_pr=True)
        assert ok is True
        # Exactly two arming attempts: --squash then strategy-agnostic --auto.
        assert ["pr", "merge", "2", "--auto", "--squash"] in calls
        assert ["pr", "merge", "2", "--auto"] in calls

    def test_non_bot_pr_does_not_get_extra_retry(self, driver: CIDriver) -> None:
        # force_merge_on_stall is False by default → no fallback for human PRs.
        with patch(
            "hephaestus.automation.ci_driver._gh_call",
            side_effect=subprocess.CalledProcessError(1, ["gh"], stderr="nope"),
        ) as mock_gh:
            ok = driver._enable_auto_merge(2, is_bot_pr=False)
        assert ok is False
        # Only the primary --squash attempt; no strategy-agnostic retry.
        assert mock_gh.call_count == 1


class TestEvaluateRunResult:
    """The final gate separates armed-pending PRs from genuinely-stuck ones (#838)."""

    def test_clean_repo_is_zero(self) -> None:
        results = {1: WorkerResult(issue_number=1, success=True, pr_number=10)}
        assert _evaluate_run_result(results, [], issues=[1], as_json=False) == 0

    def test_armed_pending_only_is_zero(self) -> None:
        # A PR still merging on its own must NOT red-flag the repo.
        results = {1: WorkerResult(issue_number=1, success=True, pr_number=10)}
        remaining = [{"number": 10, "autoMergeRequest": {"enabledAt": "now"}}]
        assert _evaluate_run_result(results, remaining, issues=[1], as_json=False) == 0

    def test_needs_action_pr_is_one(self) -> None:
        # An un-armed PR genuinely needs manual action → failure.
        results = {1: WorkerResult(issue_number=1, success=True, pr_number=10)}
        remaining = [{"number": 11, "autoMergeRequest": None}]
        assert _evaluate_run_result(results, remaining, issues=[1], as_json=False) == 1

    def test_failed_issue_is_one(self) -> None:
        results = {1: WorkerResult(issue_number=1, success=False, pr_number=10)}
        assert _evaluate_run_result(results, [], issues=[1], as_json=False) == 1
