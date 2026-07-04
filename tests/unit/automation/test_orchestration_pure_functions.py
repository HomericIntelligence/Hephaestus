"""Behavioral tests for pure-function helpers around orchestration modules.

Most source files covered here are in pyproject.toml [tool.coverage.run].omit
(live claude/gh CLI boundary), so they do not count toward the coverage
denominator. The github_api helpers remain here as the compatibility package
that replaced the former flat github_api.py module.
"""

from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# loop_runner — confirmed from loop_runner.py:122-129 and :534-539
# ---------------------------------------------------------------------------
class TestLoopRunnerPureFunctions:
    """Test pure helpers in loop_runner (omitted from coverage measurement)."""

    def test_parse_repo_list_comma_separated(self) -> None:
        from hephaestus.automation.loop_runner import _parse_repo_list

        assert _parse_repo_list("a,b,c") == ["a", "b", "c"]

    def test_parse_repo_list_whitespace_stripped(self) -> None:
        from hephaestus.automation.loop_runner import _parse_repo_list

        assert _parse_repo_list("a , b") == ["a", "b"]

    def test_parse_repo_list_empty_returns_empty_list(self) -> None:
        # Confirmed from docstring: "Empty input returns an empty list"
        from hephaestus.automation.loop_runner import _parse_repo_list

        assert _parse_repo_list("") == []

    def test_validate_phases_known_phases_pass(self) -> None:
        # ALL_SELECTABLE = ("plan", "implement", "drive-green") — loop_runner.py:108
        from hephaestus.automation.loop_runner import _validate_phases

        result = _validate_phases("plan,implement")
        assert set(result) == {"plan", "implement"}

    def test_validate_phases_drive_green_valid(self) -> None:
        from hephaestus.automation.loop_runner import _validate_phases

        result = _validate_phases("drive-green")
        assert result == ("drive-green",)

    def test_validate_phases_unknown_raises_system_exit(self) -> None:
        # Raises SystemExit — confirmed from loop_runner.py:537
        from hephaestus.automation.loop_runner import _validate_phases

        with pytest.raises(SystemExit):
            _validate_phases("notavalidphase")

    def test_validate_phases_partial_invalid_raises(self) -> None:
        from hephaestus.automation.loop_runner import _validate_phases

        with pytest.raises(SystemExit):
            _validate_phases("plan,notvalid")


# ---------------------------------------------------------------------------
# ci_driver — confirmed from ci_driver.py:82-97
# ---------------------------------------------------------------------------
class TestCIDriverPureFunctions:
    """Test pure helpers in ci_driver (omitted from coverage measurement)."""

    def test_pr_is_failing_blocked_merge_state(self) -> None:
        # mergeStateStatus == "BLOCKED" → True regardless of rollup
        from hephaestus.automation.ci_driver import _pr_is_failing

        pr = {"isDraft": False, "mergeStateStatus": "BLOCKED", "statusCheckRollup": []}
        assert _pr_is_failing(pr) is True

    def test_pr_is_failing_draft_is_not_failing(self) -> None:
        # Draft PRs are excluded — ci_driver.py:85
        from hephaestus.automation.ci_driver import _pr_is_failing

        pr = {"isDraft": True, "mergeStateStatus": "BLOCKED", "statusCheckRollup": []}
        assert _pr_is_failing(pr) is False

    def test_pr_is_failing_all_success(self) -> None:
        from hephaestus.automation.ci_driver import _pr_is_failing

        pr = {
            "isDraft": False,
            "mergeStateStatus": "MERGEABLE",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        }
        assert _pr_is_failing(pr) is False

    def test_pr_is_failing_empty_rollup_not_blocked(self) -> None:
        from hephaestus.automation.ci_driver import _pr_is_failing

        pr = {"isDraft": False, "mergeStateStatus": "MERGEABLE", "statusCheckRollup": []}
        assert _pr_is_failing(pr) is False


# ---------------------------------------------------------------------------
# github_api compatibility package — confirmed from github_api/issues.py
# ---------------------------------------------------------------------------
class TestGithubApiPureFunctions:
    """Test pure helpers in the github_api compatibility package."""

    def test_parse_issue_number_from_url(self) -> None:
        # Regex: r"/issues/(\d+)" — github_api/issues.py:_parse_issue_number
        from hephaestus.automation.github_api import _parse_issue_number

        assert _parse_issue_number("https://github.com/org/repo/issues/42") == 42

    def test_parse_issue_number_bare_numeric_string(self) -> None:
        # Fallback: int(output.split("/")[-1]) — github_api/issues.py:_parse_issue_number
        from hephaestus.automation.github_api import _parse_issue_number

        assert _parse_issue_number("99") == 99

    def test_assert_body_has_closes_valid_line(self) -> None:
        from hephaestus.automation.github_api import _assert_body_has_closes

        # Must not raise
        _assert_body_has_closes("Summary\n\nCloses #42\n")

    def test_assert_body_has_closes_missing_raises_value_error(self) -> None:
        # Raises ValueError — confirmed in github_api/issues.py
        from hephaestus.automation.github_api import _assert_body_has_closes

        with pytest.raises(ValueError):
            _assert_body_has_closes("Summary\n\nFixes #42\n")

    def test_assert_body_has_closes_fixes_keyword_not_accepted(self) -> None:
        from hephaestus.automation.github_api import _assert_body_has_closes

        with pytest.raises(ValueError):
            _assert_body_has_closes("Fixes #42")

    def test_parse_issue_dependencies_finds_deps(self) -> None:
        from hephaestus.automation.github_api import parse_issue_dependencies

        deps = parse_issue_dependencies("Depends on #10, #20")
        assert 10 in deps
        assert 20 in deps

    def test_parse_issue_dependencies_no_deps(self) -> None:
        from hephaestus.automation.github_api import parse_issue_dependencies

        assert parse_issue_dependencies("No dependencies here") == []


# ---------------------------------------------------------------------------
# audit_reviewer — confirmed from audit_reviewer.py:38-57
# ---------------------------------------------------------------------------
class TestAuditReviewerPureFunctions:
    """Test pure helpers in audit_reviewer (omitted from coverage measurement)."""

    def test_parse_coordinator_results_single_pr_dict(self) -> None:
        # Dict with "pr_number" field → appended directly
        from hephaestus.automation.audit_reviewer import _parse_coordinator_results

        payload = {"pr_number": 1, "verdict": "GO"}
        text = f"```json\n{json.dumps(payload)}\n```"
        results = _parse_coordinator_results(text)
        assert len(results) == 1
        assert results[0]["pr_number"] == 1

    def test_parse_coordinator_results_audits_list(self) -> None:
        # {"audits": [...]} → items flattened — audit_reviewer.py:52
        from hephaestus.automation.audit_reviewer import _parse_coordinator_results

        payload = {"audits": [{"pr_number": 2, "verdict": "NOGO"}]}
        text = f"```json\n{json.dumps(payload)}\n```"
        results = _parse_coordinator_results(text)
        assert len(results) == 1
        assert results[0]["pr_number"] == 2

    def test_parse_coordinator_results_empty_returns_empty_list(self) -> None:
        # Confirmed from docstring: "Empty / whitespace-only / prose input → []"
        from hephaestus.automation.audit_reviewer import _parse_coordinator_results

        assert _parse_coordinator_results("") == []
        assert _parse_coordinator_results("prose with no fences") == []

    def test_parse_coordinator_results_malformed_json_skipped(self) -> None:
        # Bad JSON block skipped, good block preserved — audit_reviewer.py:44-48
        from hephaestus.automation.audit_reviewer import _parse_coordinator_results

        text = "```json\n{bad json\n```\n```json\n" + json.dumps({"pr_number": 3}) + "\n```"
        results = _parse_coordinator_results(text)
        assert len(results) == 1
        assert results[0]["pr_number"] == 3
