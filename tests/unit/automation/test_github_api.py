"""Tests for GitHub API utilities."""

import json
import subprocess
import threading
from collections.abc import Generator
from typing import Any
from unittest.mock import Mock, patch

import pytest

import hephaestus.automation.github_api as _github_api_module
import hephaestus.github.client as client_module
from hephaestus.automation.github_api import (
    GitHubRateLimitError,
    _check_graphql_errors,
    _gh_call,
    fetch_issue_info,
    gh_create_label,
    gh_issue_add_labels,
    gh_issue_comment,
    gh_issue_create,
    gh_issue_delete_comment,
    gh_issue_json,
    gh_issue_remove_labels,
    gh_issue_upsert_comment,
    gh_list_labels,
    gh_list_open_issues,
    gh_pr_checks,
    gh_pr_create,
    gh_pr_inline_comment_index,
    gh_pr_resolve_thread,
    gh_pr_review_post,
    is_issue_closed,
    parse_issue_dependencies,
    prefetch_issue_states,
    skip_epics,
)
from hephaestus.automation.models import IssueState
from hephaestus.github.rate_limit import configure_gh_global_throttle
from hephaestus.io import utils as io_utils

# Circuit-breaker reset is now an autouse package-scope fixture in
# ``tests/unit/automation/conftest.py`` (#708), so it applies to every test
# under the automation package — not just this file.


class TestGhIssueJson:
    """Tests for gh_issue_json function."""

    @patch("hephaestus.automation.github_api._gh_call")
    def test_successful_fetch(self, mock_gh_call: Any) -> None:
        """Test successful issue fetch."""
        mock_result = Mock()
        mock_result.stdout = json.dumps(
            {
                "number": 123,
                "title": "Test issue",
                "state": "OPEN",
                "labels": [{"name": "bug"}],
                "body": "Test body",
            }
        )
        mock_gh_call.return_value = mock_result

        data = gh_issue_json(123)

        assert data["number"] == 123
        assert data["title"] == "Test issue"
        assert data["state"] == "OPEN"

    @patch("hephaestus.automation.github_api._gh_call")
    def test_failed_fetch(self, mock_gh_call: Any) -> None:
        """Test failed issue fetch."""
        mock_gh_call.side_effect = subprocess.CalledProcessError(1, "gh")

        with pytest.raises(RuntimeError, match="Failed to fetch issue"):
            gh_issue_json(123)

    @patch("hephaestus.automation.github_api._gh_call")
    def test_strips_null_bytes_from_title_and_body(self, mock_gh_call: Any) -> None:
        """#1661: NUL bytes in title/body are stripped at the source."""
        mock_result = Mock()
        mock_result.stdout = json.dumps(
            {
                "number": 1509,
                "title": "bad\x00title",
                "state": "OPEN",
                "labels": [],
                "body": "bad\x00body",
            }
        )
        mock_gh_call.return_value = mock_result

        data = gh_issue_json(1509)

        assert data["title"] == "badtitle"
        assert data["body"] == "badbody"
        assert "\x00" not in data["title"]
        assert "\x00" not in data["body"]

    @patch("hephaestus.automation.github_api._gh_call")
    def test_strip_tolerates_missing_and_non_string_fields(self, mock_gh_call: Any) -> None:
        """#1661: the .get/isinstance guard skips missing or non-string fields.

        Exercises the defensive branch — a payload with ``body`` absent and a
        non-string ``title`` must not raise (None/int have no ``.replace``).
        """
        mock_result = Mock()
        mock_result.stdout = json.dumps(
            {
                "number": 1509,
                "title": 123,  # non-string — isinstance guard must skip it
                "state": "OPEN",
                "labels": [],
                # body intentionally omitted — .get returns None, guard skips it
            }
        )
        mock_gh_call.return_value = mock_result

        data = gh_issue_json(1509)

        assert data["title"] == 123
        assert "body" not in data


class TestParseIssueDependencies:
    """Tests for parse_issue_dependencies function."""

    def test_depends_on_pattern(self) -> None:
        """Test parsing 'depends on' pattern."""
        body = "This depends on #123 and also #456"
        deps = parse_issue_dependencies(body)

        assert 123 in deps
        assert 456 in deps

    def test_blocked_by_pattern(self) -> None:
        """Test parsing 'blocked by' pattern."""
        body = "Blocked by #789"
        deps = parse_issue_dependencies(body)

        assert 789 in deps

    def test_requires_pattern(self) -> None:
        """Test parsing 'requires' pattern."""
        body = "Requires #111"
        deps = parse_issue_dependencies(body)

        assert 111 in deps

    def test_dependencies_section(self) -> None:
        """Test parsing dependencies section."""
        body = """
        ## Dependencies
        - #100
        - #200
        """
        deps = parse_issue_dependencies(body)

        assert 100 in deps
        assert 200 in deps

    def test_no_dependencies(self) -> None:
        """Test when there are no dependencies."""
        body = "This is a simple issue with no dependencies"
        deps = parse_issue_dependencies(body)

        assert len(deps) == 0

    def test_duplicate_removal(self) -> None:
        """Test that duplicates are removed."""
        body = "Depends on #123, blocked by #123"
        deps = parse_issue_dependencies(body)

        assert len(deps) == 1
        assert 123 in deps

    def test_no_dependencies_prose_with_hash_ref_not_captured(self) -> None:
        """A '#N' ref sharing a line with 'no dependencies' prose is not harvested (#1830)."""
        body = "Part of epic #1809. First issue of the epic — no dependencies."
        deps = parse_issue_dependencies(body)

        assert deps == []

    def test_epic_ref_and_depends_on_same_line_only_captures_depends_on_target(self) -> None:
        """A '#N' ref preceding the keyword is excluded; only refs after it are harvested."""
        body = "Part of epic #1809. Depends on #1811."
        deps = parse_issue_dependencies(body)

        assert deps == [1811]

    def test_dependencies_heading_section_still_parsed(self) -> None:
        """The '## Dependencies' list-item scan (Pattern 2) is unaffected by the fix (#1830)."""
        body = """
        Part of epic #1809. No inline dependency mentions here.

        ## Dependencies
        - #100
        - #200
        """
        deps = parse_issue_dependencies(body)

        assert set(deps) == {100, 200}
        assert 1809 not in deps


class TestFetchIssueInfo:
    """Tests for fetch_issue_info function."""

    @patch("hephaestus.automation.github_api.gh_issue_json")
    def test_successful_fetch(self, mock_gh_json: Any) -> None:
        """Test successful issue info fetch."""
        mock_gh_json.return_value = {
            "number": 123,
            "title": "Test issue",
            "state": "OPEN",
            "labels": [{"name": "bug"}, {"name": "priority"}],
            "body": "Depends on #100",
        }

        issue = fetch_issue_info(123)

        assert issue.number == 123
        assert issue.title == "Test issue"
        assert issue.state == IssueState.OPEN
        assert "bug" in issue.labels
        assert 100 in issue.dependencies


class TestIsIssueClosed:
    """Tests for is_issue_closed function."""

    def test_with_cached_state_closed(self) -> None:
        """Test with cached state showing closed."""
        cached = {123: IssueState.CLOSED}

        assert is_issue_closed(123, cached) is True

    def test_with_cached_state_open(self) -> None:
        """Test with cached state showing open."""
        cached = {123: IssueState.OPEN}

        assert is_issue_closed(123, cached) is False

    @patch("hephaestus.automation.github_api.gh_issue_json")
    def test_without_cache_closed(self, mock_gh_json: Any) -> None:
        """Test without cache, issue is closed."""
        mock_gh_json.return_value = {"state": "CLOSED"}

        assert is_issue_closed(123) is True

    @patch("hephaestus.automation.github_api.gh_issue_json")
    def test_without_cache_open(self, mock_gh_json: Any) -> None:
        """Test without cache, issue is open."""
        mock_gh_json.return_value = {"state": "OPEN"}

        assert is_issue_closed(123) is False

    @patch("hephaestus.automation.github_api.gh_issue_json")
    def test_error_returns_false(self, mock_gh_json: Any) -> None:
        """Test that errors return False."""
        mock_gh_json.side_effect = Exception("API error")

        assert is_issue_closed(123) is False


class TestPrefetchIssueStates:
    """Tests for prefetch_issue_states function."""

    @pytest.fixture(autouse=True)
    def _clear_state_cache(self) -> Any:
        """Reset the #1587 in-process issue-state memo between tests.

        The memo persists module-level, so without this a state cached by one
        test would satisfy another test's request and skip its mocked gh call.
        """
        from hephaestus.automation import github_api

        github_api._issue_state_cache.clear()
        yield
        github_api._issue_state_cache.clear()

    def test_empty_list(self) -> None:
        """Test with empty issue list."""
        states = prefetch_issue_states([])
        assert states == {}

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_successful_batch_fetch(self, mock_repo_info: Any, mock_gh_call: Any) -> None:
        """Test successful batch fetch."""
        mock_repo_info.return_value = ("owner", "repo")

        mock_result = Mock()
        mock_result.stdout = json.dumps(
            {
                "data": {
                    "repository": {
                        "issue0": {"number": 123, "state": "OPEN"},
                        "issue1": {"number": 456, "state": "CLOSED"},
                    }
                }
            }
        )
        mock_gh_call.return_value = mock_result

        states = prefetch_issue_states([123, 456])

        assert states[123] == IssueState.OPEN
        assert states[456] == IssueState.CLOSED

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_memoizes_in_process(self, mock_repo_info: Any, mock_gh_call: Any) -> None:
        """#1587: a second call for cached numbers does not re-query gh."""
        mock_repo_info.return_value = ("owner", "repo")
        mock_result = Mock()
        mock_result.stdout = json.dumps(
            {"data": {"repository": {"issue0": {"number": 123, "state": "OPEN"}}}}
        )
        mock_gh_call.return_value = mock_result

        first = prefetch_issue_states([123])
        second = prefetch_issue_states([123])

        assert first == second == {123: IssueState.OPEN}
        # One network round-trip total despite two calls.
        assert mock_gh_call.call_count == 1

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_refresh_forces_requery(self, mock_repo_info: Any, mock_gh_call: Any) -> None:
        """#1587: refresh=True bypasses the memo and re-queries."""
        mock_repo_info.return_value = ("owner", "repo")
        mock_result = Mock()
        mock_result.stdout = json.dumps(
            {"data": {"repository": {"issue0": {"number": 123, "state": "OPEN"}}}}
        )
        mock_gh_call.return_value = mock_result

        prefetch_issue_states([123])
        prefetch_issue_states([123], refresh=True)

        assert mock_gh_call.call_count == 2

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_only_missing_numbers_queried(self, mock_repo_info: Any, mock_gh_call: Any) -> None:
        """#1587: a follow-up call only queries numbers not already cached."""
        mock_repo_info.return_value = ("owner", "repo")
        mock_gh_call.side_effect = [
            Mock(
                stdout=json.dumps(
                    {"data": {"repository": {"issue0": {"number": 1, "state": "OPEN"}}}}
                )
            ),
            Mock(
                stdout=json.dumps(
                    {"data": {"repository": {"issue0": {"number": 2, "state": "CLOSED"}}}}
                )
            ),
        ]

        prefetch_issue_states([1])
        states = prefetch_issue_states([1, 2])

        assert states == {1: IssueState.OPEN, 2: IssueState.CLOSED}
        assert mock_gh_call.call_count == 2  # second call fetched only #2

    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_repo_info_failure(self, mock_repo_info: Any) -> None:
        """Test when repo info fails."""
        mock_repo_info.side_effect = RuntimeError("Not in repo")

        states = prefetch_issue_states([123])

        assert states == {}

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_int_validation_in_graphql_query(self, mock_repo_info: Any, mock_gh_call: Any) -> None:
        """Issue numbers must flow through `-F nN=<int>` flags, not interpolation (#738)."""
        mock_repo_info.return_value = ("owner", "repo")
        mock_result = Mock()
        mock_result.stdout = json.dumps(
            {"data": {"repository": {"issue0": {"number": 123, "state": "OPEN"}}}}
        )
        mock_gh_call.return_value = mock_result

        states = prefetch_issue_states([123])

        mock_gh_call.assert_called()
        argv = mock_gh_call.call_args[0][0]
        query = next(a for a in argv if a.startswith("query="))
        assert "$n0:Int!" in query
        assert "issue(number:$n0)" in query
        assert "issue(number: 123)" not in query  # regression guard for #738
        assert "n0=123" in argv
        # owner/repo also parameterised (no f-string interpolation)
        assert "repository(owner:$owner,name:$name)" in query
        assert 'owner: "owner"' not in query
        assert "owner=owner" in argv and "name=repo" in argv
        assert states[123] == IssueState.OPEN

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_non_numeric_issue_number_raises_error(
        self, mock_repo_info: Any, mock_gh_call: Any
    ) -> None:
        """Test that non-numeric issue numbers raise ValueError during int() casting."""
        mock_repo_info.return_value = ("owner", "repo")

        # Try to pass a non-numeric string (type-hint violation)
        # This should raise a ValueError when int() is called on it
        with pytest.raises(ValueError):
            # Using "abc" as string will fail the int() cast
            prefetch_issue_states(["abc"])  # type: ignore

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_batch_uses_one_variable_per_issue(
        self, mock_repo_info: Any, mock_gh_call: Any
    ) -> None:
        """Each batch element gets its own $nN scalar + -F nN flag (#738)."""
        mock_repo_info.return_value = ("owner", "repo")
        mock_result = Mock()
        mock_result.stdout = json.dumps(
            {
                "data": {
                    "repository": {
                        "issue0": {"number": 11, "state": "OPEN"},
                        "issue1": {"number": 22, "state": "CLOSED"},
                        "issue2": {"number": 33, "state": "OPEN"},
                    }
                }
            }
        )
        mock_gh_call.return_value = mock_result

        prefetch_issue_states([11, 22, 33])

        argv = mock_gh_call.call_args[0][0]
        query = next(a for a in argv if a.startswith("query="))
        for idx, num in enumerate([11, 22, 33]):
            assert f"$n{idx}:Int!" in query
            assert f"issue{idx}: issue(number:$n{idx})" in query
            assert f"n{idx}={num}" in argv

    @patch("hephaestus.automation.github_api.gh_issue_json")
    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_batch_failure_falls_back_to_individual_fetch(
        self, mock_repo_info: Any, mock_gh_call: Any, mock_issue_json: Any
    ) -> None:
        """When batch GraphQL fails, _fetch_batch_states falls back to gh_issue_json."""
        mock_repo_info.return_value = ("owner", "repo")
        mock_gh_call.side_effect = subprocess.CalledProcessError(1, "gh")
        mock_issue_json.side_effect = [
            {"number": 11, "state": "OPEN"},
            {"number": 22, "state": "CLOSED"},
        ]

        states = prefetch_issue_states([11, 22])

        assert mock_issue_json.call_count == 2
        assert states[11] == IssueState.OPEN
        assert states[22] == IssueState.CLOSED


class TestGhCall:
    """Tests for _gh_call function."""

    def setup_method(self) -> None:
        """Reset circuit breaker before each test."""
        from hephaestus.resilience import reset_all_circuit_breakers

        reset_all_circuit_breakers()

    @patch("hephaestus.github.client.run_subprocess")
    def test_successful_call(self, mock_run: Any) -> None:
        """Test successful gh call."""
        mock_result = Mock()
        mock_result.stdout = "success"
        mock_run.return_value = mock_result

        result = _gh_call(["issue", "view", "123"])

        assert result.stdout == "success"
        mock_run.assert_called_once()

    @patch("hephaestus.github.client.run_subprocess")
    @patch("hephaestus.github.client.wait_until")
    @patch("hephaestus.github.client.detect_rate_limit")
    def test_retry_on_rate_limit(self, mock_detect: Any, mock_wait: Any, mock_run: Any) -> None:
        """Test retry on rate limit."""
        # First call fails with rate limit, second succeeds
        mock_detect.return_value = 1234567890
        mock_run.side_effect = [
            subprocess.CalledProcessError(
                1, "gh", stderr="API rate limit exceeded. Resets at 1234567890"
            ),
            Mock(stdout="success"),
        ]

        result = _gh_call(["issue", "view", "123"])

        assert result.stdout == "success"
        assert mock_run.call_count == 2
        mock_wait.assert_called_once_with(1234567890)

    @patch("hephaestus.github.client.run_subprocess")
    def test_fail_fast_on_permission_error(self, mock_run: Any) -> None:
        """Test that permission errors fail fast without retry."""
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="403 Forbidden: permission denied"
        )

        with pytest.raises(subprocess.CalledProcessError):
            _gh_call(["issue", "view", "123"])

        # Should only call once, no retries
        assert mock_run.call_count == 1

    @patch("hephaestus.github.client.run_subprocess")
    def test_fail_fast_on_not_found(self, mock_run: Any) -> None:
        """Test that 404 errors fail fast without retry."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="404 Not Found")

        with pytest.raises(subprocess.CalledProcessError):
            _gh_call(["issue", "view", "123"])

        assert mock_run.call_count == 1

    @pytest.mark.parametrize(
        "stderr",
        [
            "gh: Could not resolve to an Issue with the number of 119.",
            "GraphQL: Could not resolve to an issue or pull request with the number of 5528.",
        ],
    )
    @patch("hephaestus.github.client.run_subprocess")
    @patch("hephaestus.github.client.time.sleep")
    def test_fail_fast_on_could_not_resolve_issue(
        self, _mock_sleep: Any, mock_run: Any, stderr: str
    ) -> None:
        """A permanent GitHub issue-resolution failure must not retry."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr=stderr)

        with pytest.raises(subprocess.CalledProcessError):
            _gh_call(["issue", "view", "119"], max_retries=6)

        assert mock_run.call_count == 1

    @patch("hephaestus.github.client.run_subprocess")
    def test_fail_fast_on_bad_request(self, mock_run: Any) -> None:
        """Test that 400 errors fail fast without retry."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="400 Bad Request")

        with pytest.raises(subprocess.CalledProcessError):
            _gh_call(["issue", "view", "123"])

        assert mock_run.call_count == 1

    @patch("hephaestus.github.client.run_subprocess")
    def test_fail_fast_on_unprocessable_entity(self, mock_run: Any) -> None:
        """422 Unprocessable Entity must fail fast without retry.

        Regression (#1040): a malformed review POST (e.g. an inline comment on a
        line outside the diff hunk) returns ``gh: Unprocessable Entity (HTTP 422)``,
        a deterministic validation error. It was retried 5x with exponential
        backoff (~31s wasted) before raising and being mis-reported as a NOGO.
        """
        _github_api_module._GH_BREAKER.reset()
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="gh: Unprocessable Entity (HTTP 422)"
        )

        with pytest.raises(subprocess.CalledProcessError):
            _gh_call(["api", "-X", "POST", "repos/o/r/pulls/1/reviews", "--input", "x.json"])

        assert mock_run.call_count == 1

    @patch("hephaestus.github.client.run_subprocess")
    def test_fail_fast_on_graphql_schema_error(self, mock_run: Any) -> None:
        """GraphQL schema errors (bad argument / unused variable) must fail fast.

        Regression (#1040): a wrong mutation field produced
        ``gh: InputObject '...' doesn't accept argument '...'`` /
        ``Variable $x is declared by ... but not used`` — permanent schema errors
        that were nonetheless retried 5x before raising.
        """
        _github_api_module._GH_BREAKER.reset()
        mock_run.side_effect = subprocess.CalledProcessError(
            1,
            "gh",
            stderr=(
                "gh: InputObject 'AddPullRequestReviewCommentInput' doesn't accept "
                "argument 'pullRequestReviewThreadId'\n"
                "Variable $threadId is declared by AddReply but not used"
            ),
        )

        with pytest.raises(subprocess.CalledProcessError):
            _gh_call(["api", "graphql", "-f", "query=mutation {...}"])

        assert mock_run.call_count == 1

    @patch("hephaestus.github.client.run_subprocess")
    @patch("hephaestus.github.client.time.sleep")
    def test_retry_on_transient_error(self, mock_sleep: Any, mock_run: Any) -> None:
        """Test retry on transient errors with jitter.

        The resilient_call inner loop now provides jitter-based backoff instead
        of the old exponential backoff. This test verifies that transient errors
        are retried (not failed fast).
        """
        # Fail twice with transient error, then succeed
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "gh", stderr="Connection reset"),
            subprocess.CalledProcessError(1, "gh", stderr="Connection reset"),
            Mock(stdout="success"),
        ]

        result = _gh_call(["issue", "view", "123"], max_retries=3)

        assert result.stdout == "success"
        assert mock_run.call_count == 3
        # Verify that retries occurred (time.sleep was called with jittered delays)
        assert len(mock_sleep.call_args_list) > 0, "Expected sleep calls for jittered backoff"

    @patch("hephaestus.github.client.run_subprocess")
    def test_claude_usage_limit_detection(self, mock_run: Any) -> None:
        """Test detection of Claude usage limit (A5-01/A5-02).

        The error must carry a Claude-specific phrase; a plain "usage limit"
        without the "Claude" prefix must no longer trigger the detector so
        GitHub's own API-rate-limit messages are not misidentified.
        """
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="Claude AI usage limit exceeded for your account"
        )

        # ClaudeUsageCapError is a RuntimeError subclass, so existing
        # 'except RuntimeError' callers continue to work.
        from hephaestus.automation.github_api import ClaudeUsageCapError

        with pytest.raises(ClaudeUsageCapError):
            _gh_call(["issue", "view", "123"])

    @patch("hephaestus.github.client.run_subprocess")
    def test_github_usage_limit_not_misidentified(self, mock_run: Any) -> None:
        """GitHub's own 'usage limit' message must not raise ClaudeUsageCapError (A5-01).

        It should be treated as a transient error and retried instead.
        """
        success = Mock()
        success.stdout = "success"
        success.returncode = 0
        # First call fails with a bare "usage limit" (GitHub's message, not Claude's);
        # second call succeeds to confirm it was retried.
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "gh", stderr="API usage limit exceeded"),
            success,
        ]

        with patch("hephaestus.github.client.time.sleep"):
            result = _gh_call(["issue", "view", "123"], max_retries=2)

        assert result.stdout == "success"

    @patch("hephaestus.github.client.run_subprocess")
    def test_token_scope_error_is_non_transient(self, mock_run: Any, caplog: Any) -> None:
        """The GraphQL "Resource not accessible by …" error fails fast.

        Regression test for the log-spam incident where this error was treated
        as transient, causing 3× retries that each logged the full
        multi-kilobyte ``--body`` argument.
        """
        stderr = "GraphQL: Resource not accessible by personal access token (addComment)"
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr=stderr)

        with caplog.at_level("ERROR", logger="hephaestus.automation.github_api"):
            with pytest.raises(subprocess.CalledProcessError):
                _gh_call(["issue", "comment", "584", "--body-file", "/tmp/x"])

        # Must not retry: exactly one underlying gh invocation.
        assert mock_run.call_count == 1
        # Must log the actionable remediation message.
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "token lacks required scopes" in joined
        assert "GITHUB_TOKEN=" in joined
        assert "gh auth status" in joined

    @patch("hephaestus.github.client.run_subprocess")
    def test_token_scope_error_for_integration_also_non_transient(self, mock_run: Any) -> None:
        """GitHub-App variant of the scope error is recognised too."""
        stderr = "GraphQL: Resource not accessible by integration (addComment)"
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr=stderr)

        with pytest.raises(subprocess.CalledProcessError):
            _gh_call(["issue", "comment", "1", "--body-file", "/tmp/x"])

        assert mock_run.call_count == 1

    @patch("hephaestus.github.client._gh_call_impl")
    def test_circuit_breaker_wraps_gh_call_impl(self, mock_impl: Any) -> None:
        """Test that _gh_call routes through the circuit breaker to _gh_call_impl.

        Verifies that the circuit breaker calls _gh_call_impl with the
        correct arguments forwarded from _gh_call.
        """
        _github_api_module._GH_BREAKER.reset()
        mock_result = Mock(spec=subprocess.CompletedProcess)
        mock_result.stdout = "success"
        mock_impl.return_value = mock_result

        result = _gh_call(["issue", "view", "123"], max_retries=6)

        assert result.stdout == "success"
        # _gh_call_impl should be called once via the circuit breaker
        assert mock_impl.call_count == 1
        # Check that args and kwargs are forwarded correctly
        call_args, call_kwargs = mock_impl.call_args
        assert call_args[0] == ["issue", "view", "123"]
        assert call_kwargs["max_retries"] == 6

    @patch("hephaestus.github.client.run_subprocess")
    def test_non_transient_errors_not_retried_by_resilient_call(self, mock_run: Any) -> None:
        """Test that non-transient errors bypass resilient_call retries.

        When _gh_invoke_once detects a non-transient error (403, 404, etc),
        it should raise _NonTransientGhError which is NOT in
        TRANSIENT_SUBPROCESS_ERRORS, so resilient_call does not retry it.
        """
        # First attempt: non-transient 404 error
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="404 Not Found")

        with pytest.raises(subprocess.CalledProcessError):
            _gh_call(["issue", "view", "123"], max_retries=6)

        # Should only call once despite max_retries=6
        assert mock_run.call_count == 1

    @patch("hephaestus.github.client.run_subprocess")
    @patch("hephaestus.github.client.time.sleep")
    def test_transient_errors_retried_with_jitter(self, mock_sleep: Any, mock_run: Any) -> None:
        """Test that transient errors are retried with jitter.

        When _gh_invoke_once raises a transient CalledProcessError,
        resilient_call should catch it and retry with jitter (not simple
        exponential backoff). The inner loop has jitter=True.
        """
        # Fail 2 times with transient error, then succeed
        # This allows inner resilient_call with max_retries=2 (3 attempts total) to succeed
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "gh", stderr="Connection reset"),
            subprocess.CalledProcessError(1, "gh", stderr="Connection reset"),
            Mock(stdout="success"),
        ]

        result = _gh_call(["issue", "view", "123"], max_retries=6)

        assert result.stdout == "success"
        # Should have been retried by inner resilient_call (up to 2 retries = 3 total attempts)
        assert mock_run.call_count == 3

    @patch("hephaestus.github.client.run_subprocess")
    @patch("hephaestus.github.client.wait_until")
    @patch("hephaestus.github.client.detect_rate_limit")
    def test_rate_limit_errors_propagate_correctly(
        self, mock_detect: Any, mock_wait: Any, mock_run: Any
    ) -> None:
        """Test that rate-limit errors propagate without retry by resilient_call.

        GitHubRateLimitError is NOT in TRANSIENT_SUBPROCESS_ERRORS, so
        resilient_call propagates it immediately. The outer _gh_call loop
        then calls _handle_rate_limit_attempt.
        """
        mock_detect.return_value = 1234567890
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="API rate limit exceeded"
        )

        # Outer loop should enter rate-limit handler, not exhaust inner retries
        with pytest.raises(GitHubRateLimitError):
            _gh_call(["issue", "view", "123"], max_retries=1, retry_on_rate_limit=False)

        # Should detect rate limit on first inner attempt
        assert mock_detect.called

    @patch("hephaestus.github.client.run_subprocess")
    def test_claude_usage_cap_errors_propagate_correctly(self, mock_run: Any) -> None:
        """Test that ClaudeUsageCapError is not retried by resilient_call.

        ClaudeUsageCapError is NOT in TRANSIENT_SUBPROCESS_ERRORS, so it
        propagates immediately without retries from resilient_call.
        """
        from hephaestus.automation.github_api import ClaudeUsageCapError

        # Use a pattern matching the Claude CLI's actual usage cap message
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="You're out of extra usage · resets 5pm (America/Los_Angeles)"
        )

        with pytest.raises(ClaudeUsageCapError):
            _gh_call(["issue", "view", "123"], max_retries=6)

        # Should fail on first attempt, not retried by resilient_call
        assert mock_run.call_count == 1

    @patch("hephaestus.github.client.run_subprocess")
    def test_circuit_breaker_integration(self, mock_run: Any) -> None:
        """Test that circuit breaker is integrated with resilient_call.

        The circuit breaker is created/retrieved by resilient_call using the
        _GH_CB_NAME constant, and tracks failures across multiple _gh_call
        attempts.
        """
        # Mock successful call
        mock_result = Mock(spec=subprocess.CompletedProcess)
        mock_result.stdout = "success"
        mock_run.return_value = mock_result

        result = _gh_call(["issue", "view", "123"])

        assert result.stdout == "success"

    @patch("hephaestus.github.client.run_subprocess")
    def test_non_transient_error_original_exception_type_preserved(self, mock_run: Any) -> None:
        """Test that non-transient errors preserve original CalledProcessError type.

        When _NonTransientGhError is raised by _gh_invoke_once, the outer
        loop must unwrap it and re-raise the original CalledProcessError
        so that callers' `except subprocess.CalledProcessError` clauses work.
        """
        original_error = subprocess.CalledProcessError(1, "gh", stderr="403 Forbidden")
        mock_run.side_effect = original_error

        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            _gh_call(["issue", "view", "123"])

        # Must be the original CalledProcessError type, not a wrapper
        assert isinstance(exc_info.value, subprocess.CalledProcessError)
        assert exc_info.value.returncode == 1

    @patch("hephaestus.github.client.run_subprocess")
    def test_max_retries_exhaustion_outer_loop(self, mock_run: Any) -> None:
        """Test that outer loop exhausts max_retries correctly.

        With max_retries=6 and 3 inner retries per outer iteration, worst case
        is 18 total attempts. Transient failures that exhaust inner retries
        should try again in outer loop until exhausted.
        """
        # Every invocation fails with transient error
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="Connection reset")

        with pytest.raises(subprocess.CalledProcessError):
            _gh_call(["issue", "view", "123"], max_retries=2)

        # max_retries=2 outer iterations × 3 inner retries = 6 total attempts
        # (range(2) = [0, 1], each iteration tries resilient_call with max_retries=2)
        assert mock_run.call_count >= 2  # At least the outer iterations

    @patch("hephaestus.github.client.time")
    @patch("hephaestus.github.client.run_subprocess")
    def test_secondary_rate_limit_retries_with_15s_base_backoff(
        self, mock_run: Any, mock_time: Any
    ) -> None:
        """Secondary rate limit triggers 15s base backoff, not 1s generic retry.

        When stderr contains 'exceeded a secondary rate limit', _gh_call_impl
        must route through _handle_rate_limit_attempt(base_wait_seconds=15)
        instead of the generic 2**attempt path, so the first wait is 15s.
        """
        secondary_msg = (
            "gh: You have exceeded a secondary rate limit. "
            "Please wait a few minutes before you try again."
        )
        success = Mock(spec=subprocess.CompletedProcess)
        success.stdout = "ok"
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "gh", stderr=secondary_msg),
            success,
        ]
        mock_time.sleep = Mock()
        mock_time.monotonic = __import__("time").monotonic
        mock_time.time = __import__("time").time

        result = _gh_call(["issue", "view", "123"], max_retries=6)

        assert result.stdout == "ok"
        # Filter out sub-second throttle sleeps; only the secondary-rate-limit
        # wait (>=1s) matters here.  First such sleep must be 15s (base), not 1s.
        sleep_calls = [c[0][0] for c in mock_time.sleep.call_args_list if c[0][0] >= 1]
        assert sleep_calls, "Expected at least one rate-limit sleep call"
        first_wait = sleep_calls[0]
        assert first_wait == 15, f"Expected 15s base wait, got {first_wait}s"

    @patch("hephaestus.github.client.time")
    @patch("hephaestus.github.client.run_subprocess")
    def test_secondary_rate_limit_raises_rate_limit_error_when_retry_disabled(
        self, mock_run: Any, mock_time: Any
    ) -> None:
        """Secondary rate limit with retry_on_rate_limit=False raises GitHubRateLimitError."""
        secondary_msg = (
            "gh: You have exceeded a secondary rate limit. "
            "Please wait a few minutes before you try again."
        )
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr=secondary_msg)
        mock_time.sleep = Mock()
        mock_time.monotonic = __import__("time").monotonic
        mock_time.time = __import__("time").time

        with pytest.raises(GitHubRateLimitError):
            _gh_call(["issue", "view", "123"], max_retries=6, retry_on_rate_limit=False)

    @patch("hephaestus.github.client.time")
    @patch("hephaestus.github.client.run_subprocess")
    def test_secondary_rate_limit_backoff_doubles_each_attempt(
        self, mock_run: Any, mock_time: Any
    ) -> None:
        """Secondary rate limit backoff doubles: 15s, 30s, capped at 300s."""
        secondary_msg = (
            "gh: You have exceeded a secondary rate limit. "
            "Please wait a few minutes before you try again."
        )
        success = Mock(spec=subprocess.CompletedProcess)
        success.stdout = "ok"
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "gh", stderr=secondary_msg),
            subprocess.CalledProcessError(1, "gh", stderr=secondary_msg),
            success,
        ]
        mock_time.sleep = Mock()
        mock_time.monotonic = __import__("time").monotonic
        mock_time.time = __import__("time").time

        result = _gh_call(["issue", "view", "123"], max_retries=6)

        assert result.stdout == "ok"
        # Filter out sub-second throttle sleeps; only the secondary-rate-limit
        # waits (>=15s) matter for this assertion.
        sleep_calls = [c[0][0] for c in mock_time.sleep.call_args_list if c[0][0] >= 1]
        assert len(sleep_calls) >= 2, f"Expected >=2 rate-limit sleeps, got {sleep_calls}"
        assert sleep_calls[0] == 15, f"Expected 15s (attempt 0), got {sleep_calls[0]}"
        assert sleep_calls[1] == 30, f"Expected 30s (attempt 1), got {sleep_calls[1]}"


# NOTE on patch targets: tests in TestGhCall patch "hephaestus.github.client.run_subprocess"
# because _gh_call now routes through hephaestus.github.client._gh_call_impl, which calls
# run_subprocess imported from hephaestus.utils.helpers. All other tests patch
# "hephaestus.automation.github_api._gh_call" to intercept at a higher level, bypassing
# the gh CLI entirely.
class TestGhIssueComment:
    """Tests for gh_issue_comment function."""

    @patch("hephaestus.automation.github_api._gh_call")
    def test_successful_comment(self, mock_gh_call: Any) -> None:
        """Comment body is passed via --body-file, not inline --body.

        See ``_body_file``: large bodies on the command line bloat error logs
        and risk argv-size limits, so we route every comment through a
        tempfile.
        """
        mock_gh_call.return_value = Mock()

        gh_issue_comment(123, "Test comment")

        mock_gh_call.assert_called_once()
        call_args = mock_gh_call.call_args[0][0]
        # Expect: ["issue", "comment", "123", "--body-file", "<tmp path>"]
        assert call_args[:4] == ["issue", "comment", "123", "--body-file"]
        assert isinstance(call_args[4], str) and call_args[4]
        # Inline body must not appear anywhere in argv.
        assert "Test comment" not in call_args

    @patch("hephaestus.automation.github_api._gh_call")
    def test_failed_comment(self, mock_gh_call: Any) -> None:
        """Test failed comment posting."""
        mock_gh_call.side_effect = subprocess.CalledProcessError(1, "gh")

        with pytest.raises(RuntimeError, match="Failed to post comment"):
            gh_issue_comment(123, "Test comment")

    @patch("hephaestus.automation.github_api._gh_call")
    def test_comment_body_argv_does_not_contain_large_body(self, mock_gh_call: Any) -> None:
        """A large body (e.g. an implementation plan) never appears inline."""
        mock_gh_call.return_value = Mock()
        large_body = "x" * 50_000

        gh_issue_comment(456, large_body)

        call_args = mock_gh_call.call_args[0][0]
        assert "--body-file" in call_args
        assert "--body" not in call_args  # the inline flag must not be used
        for arg in call_args:
            assert large_body not in arg


class TestGhIssueCreate:
    """Tests for gh_issue_create function."""

    @patch("hephaestus.automation.github_api._gh_call")
    def test_successful_creation(self, mock_gh_call: Any) -> None:
        """Test successful issue creation."""
        mock_result = Mock()
        mock_result.stdout = "https://github.com/owner/repo/issues/789"
        mock_gh_call.return_value = mock_result

        issue_number = gh_issue_create(
            title="Test issue",
            body="Test body",
        )

        assert issue_number == 789
        mock_gh_call.assert_called_once()
        call_args = mock_gh_call.call_args[0][0]
        # Body is now passed via --body-file <tmp path>, not inline --body.
        assert call_args[:4] == ["issue", "create", "--title", "Test issue"]
        assert call_args[4] == "--body-file"
        assert isinstance(call_args[5], str) and call_args[5]
        assert "Test body" not in call_args

    @patch("hephaestus.automation.github_api._gh_call")
    def test_title_null_byte_stripped_from_argv(self, mock_gh_call: Any) -> None:
        """#1661: a NUL in the title must not reach the --title argv element."""
        mock_result = Mock()
        mock_result.stdout = "https://github.com/owner/repo/issues/791"
        mock_gh_call.return_value = mock_result

        gh_issue_create(title="bad\x00title", body="Body")

        call_args = mock_gh_call.call_args[0][0]
        assert call_args[:4] == ["issue", "create", "--title", "badtitle"]
        assert all("\x00" not in str(arg) for arg in call_args)

    @patch("hephaestus.automation.github_api.gh_list_labels", return_value={"bug", "enhancement"})
    @patch("hephaestus.automation.github_api._gh_call")
    def test_creation_with_labels(self, mock_gh_call: Any, mock_labels: Any) -> None:
        """Test issue creation with labels that already exist."""
        mock_result = Mock()
        mock_result.stdout = "https://github.com/owner/repo/issues/790"
        mock_gh_call.return_value = mock_result

        issue_number = gh_issue_create(
            title="Test issue",
            body="Test body",
            labels=["bug", "enhancement"],
        )

        assert issue_number == 790
        call_args = mock_gh_call.call_args[0][0]
        assert "--label" in call_args
        assert "bug" in call_args
        assert "enhancement" in call_args

    @patch("hephaestus.automation.github_api._gh_call")
    def test_creation_without_labels(self, mock_gh_call: Any) -> None:
        """Test issue creation without labels skips label validation."""
        mock_result = Mock()
        mock_result.stdout = "https://github.com/owner/repo/issues/791"
        mock_gh_call.return_value = mock_result

        issue_number = gh_issue_create(
            title="Test issue",
            body="Test body",
            labels=None,
        )

        assert issue_number == 791
        call_args = mock_gh_call.call_args[0][0]
        assert "--label" not in call_args

    @patch("hephaestus.automation.github_api._gh_call")
    def test_failed_creation(self, mock_gh_call: Any) -> None:
        """Test failed issue creation."""
        mock_gh_call.side_effect = subprocess.CalledProcessError(1, "gh")

        with pytest.raises(RuntimeError, match="Failed to create issue"):
            gh_issue_create("Test", "Body")

    @patch("hephaestus.automation.github_api.gh_create_label")
    @patch("hephaestus.automation.github_api.gh_list_labels", return_value={"bug"})
    @patch("hephaestus.automation.github_api._gh_call")
    def test_creation_auto_creates_missing_label(
        self, mock_gh_call: Any, mock_labels: Any, mock_create_label: Any
    ) -> None:
        """Missing labels are created before issue creation."""
        mock_result = Mock()
        mock_result.stdout = "https://github.com/owner/repo/issues/792"
        mock_gh_call.return_value = mock_result

        issue_number = gh_issue_create(
            title="Test issue",
            body="Test body",
            labels=["bug", "testing"],
        )

        assert issue_number == 792
        mock_create_label.assert_called_once_with("testing")

    @patch("hephaestus.automation.github_api.gh_create_label")
    @patch("hephaestus.automation.github_api.gh_list_labels", return_value={"bug"})
    @patch("hephaestus.automation.github_api._gh_call")
    def test_creation_retries_after_label_not_found_error(
        self, mock_gh_call: Any, mock_labels: Any, mock_create_label: Any
    ) -> None:
        """If issue create fails with label-not-found despite pre-create, retries once."""
        err = subprocess.CalledProcessError(1, "gh")
        err.stderr = "could not add label: 'testing' not found"
        err.stdout = ""
        success = Mock()
        success.stdout = "https://github.com/owner/repo/issues/793"
        # First call to _gh_call (issue create) raises, second succeeds
        mock_gh_call.side_effect = [err, success]

        issue_number = gh_issue_create(
            title="Test issue",
            body="Test body",
            labels=["testing"],
        )

        assert issue_number == 793
        # gh_create_label called twice: once in _ensure_labels_exist, once in retry path
        assert mock_create_label.call_count == 2

    @patch("hephaestus.automation.github_api.gh_list_labels", return_value={"bug"})
    @patch("hephaestus.automation.github_api._gh_call")
    def test_creation_propagates_non_label_error(self, mock_gh_call: Any, mock_labels: Any) -> None:
        """Non-label-related errors propagate without label retry."""
        err = subprocess.CalledProcessError(1, "gh")
        err.stderr = "403 Forbidden"
        err.stdout = ""
        mock_gh_call.side_effect = err

        with pytest.raises(RuntimeError, match="Failed to create issue"):
            gh_issue_create("Test", "Body", labels=["bug"])


class TestGhListLabels:
    """Tests for gh_list_labels and gh_create_label."""

    def setup_method(self) -> None:
        """Reset module-level label cache before each test."""
        _github_api_module._label_cache.clear()

    @patch("hephaestus.automation.github_api._gh_call")
    def test_returns_set_of_label_names(self, mock_gh_call: Any) -> None:
        """Returns the set of existing label names."""
        mock_result = Mock()
        mock_result.stdout = json.dumps([{"name": "bug"}, {"name": "enhancement"}])
        mock_gh_call.return_value = mock_result

        labels = gh_list_labels()

        assert labels == {"bug", "enhancement"}

    @patch("hephaestus.automation.github_api._gh_call")
    def test_caches_result(self, mock_gh_call: Any) -> None:
        """Subsequent calls without refresh use the cache."""
        mock_result = Mock()
        mock_result.stdout = json.dumps([{"name": "bug"}])
        mock_gh_call.return_value = mock_result

        gh_list_labels()
        gh_list_labels()

        assert mock_gh_call.call_count == 1

    @patch("hephaestus.automation.github_api._gh_call")
    def test_refresh_bypasses_cache(self, mock_gh_call: Any) -> None:
        """refresh=True re-fetches even when cache is populated."""
        mock_result = Mock()
        mock_result.stdout = json.dumps([{"name": "bug"}])
        mock_gh_call.return_value = mock_result

        gh_list_labels()
        gh_list_labels(refresh=True)

        assert mock_gh_call.call_count == 2

    @patch("hephaestus.automation.github_api._gh_call")
    def test_create_label_calls_gh(self, mock_gh_call: Any) -> None:
        """gh_create_label passes --force and the label name."""
        mock_gh_call.return_value = Mock()

        gh_create_label("testing")

        args = mock_gh_call.call_args[0][0]
        assert args[0:2] == ["label", "create"]
        assert "testing" in args
        assert "--force" in args

    @patch("hephaestus.automation.github_api.labels._label_cache_key", return_value="o/r")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_create_label_updates_cache(self, mock_gh_call: Any, _key: Any) -> None:
        """gh_create_label augments the current repo's cache entry."""
        _github_api_module._label_cache.get_or_compute("o/r", lambda: {"bug"})
        mock_gh_call.return_value = Mock()

        gh_create_label("testing")

        assert "testing" in gh_list_labels()


class TestGhListLabelsRepoKeyedConcurrency:
    """#1858: label cache is repo-keyed and lock-guarded."""

    def setup_method(self) -> None:
        _github_api_module._label_cache.clear()

    @patch("hephaestus.automation.github_api.labels._label_cache_key")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_distinct_repos_do_not_share_cache(self, mock_gh_call: Any, mock_key: Any) -> None:
        """Distinct repos must not reuse each other's cached label sets."""

        def _labels_for(_argv: Any, *a: Any, **k: Any) -> Any:
            payload = {"x/x": [{"name": "state:skip"}], "y/y": [{"name": "bug"}]}[
                mock_key.return_value
            ]
            return Mock(stdout=json.dumps(payload))

        mock_gh_call.side_effect = _labels_for
        mock_key.return_value = "x/x"
        assert gh_list_labels() == {"state:skip"}
        mock_key.return_value = "y/y"
        assert gh_list_labels() == {"bug"}  # Y must not reuse X's entry

    def test_concurrent_cross_repo_access_is_isolated(self) -> None:
        """Concurrent threads on different repos never see cross-repo cache interference."""
        import threading

        results: dict[str, set[str]] = {}
        # Force both threads to be *inside* gh_list_labels simultaneously so the
        # test actually exercises concurrent cache access (a non-thread-safe
        # cache could otherwise pass by running the threads sequentially).
        overlap = threading.Barrier(2, timeout=5)

        def mock_key_fn() -> str:
            """Return the currently-patched repo slug based on thread context."""
            import threading

            return threading.current_thread().name

        def mock_gh_call(argv: Any) -> Any:
            """Return label payload based on the calling thread's repo slug."""
            slug = mock_key_fn()
            payload = {"t1": [{"name": "state:skip"}], "t2": [{"name": "bug"}]}[slug]
            overlap.wait()  # both threads block here until both are mid-fetch
            return Mock(stdout=json.dumps(payload))

        def _run(slug: str) -> None:
            t = threading.current_thread()
            t.name = slug
            results[slug] = gh_list_labels(refresh=True)

        with patch(
            "hephaestus.automation.github_api.labels._label_cache_key", side_effect=mock_key_fn
        ):
            with patch("hephaestus.automation.github_api._gh_call", side_effect=mock_gh_call):
                t1 = threading.Thread(target=_run, args=("t1",), name="t1")
                t2 = threading.Thread(target=_run, args=("t2",), name="t2")
                t1.start()
                t2.start()
                t1.join(timeout=5)
                t2.join(timeout=5)

        assert "state:skip" in results["t1"] and "bug" in results["t2"]

    @patch("hephaestus.automation.github_api.labels._label_cache_key", return_value="")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_unresolved_repo_key_is_empty_string(self, mock_gh_call: Any, _key: Any) -> None:
        """RuntimeError from get_repo_info degrades to the '' key, not a crash."""
        mock_gh_call.return_value = Mock(stdout=json.dumps([{"name": "bug"}]))
        assert gh_list_labels() == {"bug"}


class TestGhIssueAddLabels:
    """Tests for gh_issue_add_labels (#704)."""

    def teardown_method(self) -> None:
        _github_api_module._label_cache.clear()

    @patch("hephaestus.automation.github_api._gh_call")
    def test_no_labels_is_noop(self, mock_gh_call: Any) -> None:
        gh_issue_add_labels(42, [])
        mock_gh_call.assert_not_called()

    @patch("hephaestus.automation.github_api._gh_call")
    @patch(
        "hephaestus.automation.github_api.gh_list_labels",
        return_value={"bug", "state:plan-go"},
    )
    @patch("hephaestus.automation.github_api.gh_create_label")
    def test_existing_label_skips_create(
        self, mock_create: Any, _mock_list: Any, mock_gh_call: Any
    ) -> None:
        """A label that already exists in the repo is not re-created."""
        gh_issue_add_labels(42, ["state:plan-go"])
        mock_create.assert_not_called()
        # Exactly one edit call to add the label.
        assert mock_gh_call.call_count == 1
        args = mock_gh_call.call_args[0][0]
        assert args[:3] == ["issue", "edit", "42"]
        assert "--add-label" in args
        assert "state:plan-go" in args

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.gh_list_labels", return_value={"bug"})
    @patch("hephaestus.automation.github_api.gh_create_label")
    def test_missing_label_is_auto_created(
        self, mock_create: Any, _mock_list: Any, mock_gh_call: Any
    ) -> None:
        """A label not yet present in the repo is created before the edit call."""
        gh_issue_add_labels(42, ["state:plan-go"])
        mock_create.assert_called_once_with("state:plan-go")

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.gh_list_labels", return_value={"bug"})
    @patch("hephaestus.automation.github_api.gh_create_label")
    def test_multiple_labels_share_one_edit_call(
        self, mock_create: Any, _mock_list: Any, mock_gh_call: Any
    ) -> None:
        """All labels go in a single ``gh issue edit`` invocation."""
        gh_issue_add_labels(42, ["state:plan-go", "state:plan-no-go"])
        assert mock_gh_call.call_count == 1
        args = mock_gh_call.call_args[0][0]
        # Two --add-label flags, one per label.
        assert args.count("--add-label") == 2
        assert "state:plan-go" in args
        assert "state:plan-no-go" in args


class TestSkipEpics:
    """Tests for skip_epics — idempotent ``state:skip`` tagging of excluded epics."""

    def teardown_method(self) -> None:
        _github_api_module._label_cache.clear()

    @patch("hephaestus.automation.github_api.gh_issue_add_labels")
    def test_tags_unskipped_epics(self, mock_add: Any) -> None:
        """Each epic without state:skip gets exactly one add-label call."""
        skip_epics({10: ["epic"], 11: ["roadmap", "bug"]})
        assert mock_add.call_count == 2
        mock_add.assert_any_call(10, ["state:skip"])
        mock_add.assert_any_call(11, ["state:skip"])

    @patch("hephaestus.automation.github_api.gh_issue_add_labels")
    def test_skips_already_skipped_epic(self, mock_add: Any) -> None:
        """An epic already carrying state:skip is not re-tagged (no API write)."""
        skip_epics({10: ["epic", "state:skip"]})
        mock_add.assert_not_called()

    @patch("hephaestus.automation.github_api.gh_issue_add_labels")
    def test_mixed_skipped_and_unskipped(self, mock_add: Any) -> None:
        skip_epics({10: ["epic", "state:skip"], 11: ["roadmap"]})
        mock_add.assert_called_once_with(11, ["state:skip"])

    @patch("hephaestus.automation.github_api.gh_issue_add_labels")
    def test_empty_mapping_is_noop(self, mock_add: Any) -> None:
        skip_epics({})
        mock_add.assert_not_called()


class TestGhIssueRemoveLabels:
    """Tests for gh_issue_remove_labels (#704)."""

    @patch("hephaestus.automation.github_api._gh_call")
    def test_no_labels_is_noop(self, mock_gh_call: Any) -> None:
        gh_issue_remove_labels(42, [])
        mock_gh_call.assert_not_called()

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.gh_list_labels", return_value={"state:plan-no-go"})
    def test_single_label_remove(self, _mock_list: Any, mock_gh_call: Any) -> None:
        gh_issue_remove_labels(42, ["state:plan-no-go"])
        assert mock_gh_call.call_count == 1
        args = mock_gh_call.call_args[0][0]
        assert args[:3] == ["issue", "edit", "42"]
        assert "--remove-label" in args
        assert "state:plan-no-go" in args

    @patch("hephaestus.automation.github_api._gh_call")
    @patch(
        "hephaestus.automation.github_api.gh_list_labels",
        return_value={"state:plan-no-go", "state:needs-plan"},
    )
    def test_multiple_labels_share_one_call(self, _mock_list: Any, mock_gh_call: Any) -> None:
        gh_issue_remove_labels(42, ["state:plan-no-go", "state:needs-plan"])
        assert mock_gh_call.call_count == 1
        args = mock_gh_call.call_args[0][0]
        assert args.count("--remove-label") == 2

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.gh_list_labels", return_value={"state:plan-no-go"})
    def test_missing_repo_labels_are_ignored(self, _mock_list: Any, mock_gh_call: Any) -> None:
        gh_issue_remove_labels(42, ["state:plan-go", "state:needs-plan"])
        mock_gh_call.assert_not_called()

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.gh_list_labels", side_effect=RuntimeError("boom"))
    def test_label_list_failure_attempts_requested_removals(
        self, _mock_list: Any, mock_gh_call: Any
    ) -> None:
        gh_issue_remove_labels(42, ["state:plan-go", "state:needs-plan"])
        args = mock_gh_call.call_args[0][0]
        assert args[:3] == ["issue", "edit", "42"]
        assert args.count("--remove-label") == 2
        assert "state:plan-go" in args
        assert "state:needs-plan" in args


class TestGhListOpenIssues:
    """Tests for gh_list_open_issues."""

    @patch("hephaestus.automation.github_api._gh_call")
    def test_returns_sorted_issue_numbers(self, mock_gh_call: Any) -> None:
        """Returns issue numbers sorted ascending."""
        mock_result = Mock()
        mock_result.stdout = json.dumps([{"number": 5}, {"number": 1}, {"number": 3}])
        mock_gh_call.return_value = mock_result

        issues = gh_list_open_issues()

        assert issues == [1, 3, 5]

    @patch("hephaestus.automation.github_api._gh_call")
    def test_empty_repo_returns_empty_list(self, mock_gh_call: Any) -> None:
        """Returns empty list when no open issues."""
        mock_result = Mock()
        mock_result.stdout = json.dumps([])
        mock_gh_call.return_value = mock_result

        assert gh_list_open_issues() == []

    @patch("hephaestus.automation.github_api._gh_call")
    def test_failure_raises_runtime_error(self, mock_gh_call: Any) -> None:
        """Wraps gh CLI errors in RuntimeError."""
        mock_gh_call.side_effect = subprocess.CalledProcessError(1, "gh")

        with pytest.raises(RuntimeError, match="Failed to list open issues"):
            gh_list_open_issues()


_POLICY_BODY = "## Summary\nfoo\n\nCloses #1\n"


def _unarmed_pr_state() -> Mock:
    """Build the verified unarmed state returned after PR creation."""
    return Mock(returncode=0, stdout=json.dumps({"state": "OPEN", "autoMergeRequest": None}))


class TestGhPrCreate:
    """Tests for gh_pr_create function."""

    @patch("hephaestus.automation.github_api._gh_call")
    def test_existing_pr_lookup_rejects_empty_successful_output(self, mock_gh_call: Any) -> None:
        """An empty successful discovery response cannot be reinterpreted as no PR."""
        mock_gh_call.return_value = Mock(stdout="")

        with pytest.raises(RuntimeError, match="could not verify existing PR state"):
            _github_api_module._find_open_pr_for_head("768-auto-impl", "main")

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_fresh_pr_creation_contains_open_prs_on_other_bases(
        self, mock_gh_call: Any, _mock_signed: Any
    ) -> None:
        """A head branch's other-base PR is contained before a fresh PR is created."""
        mock_gh_call.side_effect = [
            Mock(stdout=json.dumps([{"number": 962, "state": "OPEN", "baseRefName": "release"}])),
            Mock(
                returncode=0,
                stdout=json.dumps({"state": "OPEN", "autoMergeRequest": {"enabledAt": "now"}}),
            ),
            Mock(returncode=0, stdout="", stderr=""),
            Mock(returncode=0, stdout=json.dumps({"state": "OPEN", "autoMergeRequest": None})),
            Mock(stdout="https://github.com/owner/repo/pull/456"),
            _unarmed_pr_state(),
        ]

        assert gh_pr_create(branch="768-auto-impl", title="Test PR", body=_POLICY_BODY) == 456

        calls = [call.args[0] for call in mock_gh_call.call_args_list]
        assert "--base" not in calls[0]
        assert calls[1:4] == [
            ["pr", "view", "962", "--json", "state,autoMergeRequest"],
            ["pr", "merge", "962", "--disable-auto"],
            ["pr", "view", "962", "--json", "state,autoMergeRequest"],
        ]
        assert calls[4][0:2] == ["pr", "create"]

    @patch("hephaestus.automation.github_api._gh_call")
    def test_existing_pr_lookup_rejects_multiple_open_prs_on_the_requested_base(
        self, mock_gh_call: Any
    ) -> None:
        """Ambiguous open PR discovery fails closed instead of choosing arbitrarily."""
        mock_gh_call.return_value = Mock(
            stdout=json.dumps(
                [
                    {"number": 962, "state": "OPEN", "baseRefName": "main"},
                    {"number": 963, "state": "OPEN", "baseRefName": "main"},
                ]
            )
        )

        with pytest.raises(RuntimeError, match="could not verify existing PR state"):
            _github_api_module._find_open_pr_for_head("768-auto-impl", "main")

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_ambiguous_target_prs_are_contained_before_creation_is_refused(
        self, mock_gh_call: Any, _mock_signed: Any
    ) -> None:
        """Ambiguity never skips containment of the already-open PRs."""
        mock_gh_call.side_effect = [
            Mock(
                stdout=json.dumps(
                    [
                        {"number": 962, "state": "OPEN", "baseRefName": "main"},
                        {"number": 963, "state": "OPEN", "baseRefName": "main"},
                    ]
                )
            ),
            _unarmed_pr_state(),
            _unarmed_pr_state(),
        ]

        with pytest.raises(RuntimeError, match="could not verify existing PR state"):
            gh_pr_create(branch="768-auto-impl", title="Test PR", body=_POLICY_BODY)

        assert [call.args[0][:3] for call in mock_gh_call.call_args_list[1:]] == [
            ["pr", "view", "962"],
            ["pr", "view", "963"],
        ]

    @patch("hephaestus.automation.github_api._gh_call")
    def test_existing_pr_lookup_rejects_unknown_state(self, mock_gh_call: Any) -> None:
        """An unrecognised PR state cannot be reinterpreted as non-open."""
        mock_gh_call.return_value = Mock(
            stdout=json.dumps([{"number": 962, "state": "OPNE", "baseRefName": "main"}])
        )

        with pytest.raises(RuntimeError, match="could not verify existing PR state"):
            _github_api_module._find_open_pr_for_head("768-auto-impl", "main")

    @patch("hephaestus.automation.github_api._gh_call")
    def test_existing_pr_lookup_rejects_blank_base_name(self, mock_gh_call: Any) -> None:
        """A blank base cannot be used to select or contain an existing PR."""
        mock_gh_call.return_value = Mock(
            stdout=json.dumps([{"number": 962, "state": "OPEN", "baseRefName": "  "}])
        )

        with pytest.raises(RuntimeError, match="could not verify existing PR state"):
            _github_api_module._find_open_prs_for_head("768-auto-impl")

    @patch("hephaestus.automation.github_api._gh_call")
    def test_existing_pr_lookup_rejects_a_full_page(self, mock_gh_call: Any) -> None:
        """A capped head query cannot claim every existing PR was contained."""
        mock_gh_call.return_value = Mock(
            stdout=json.dumps([{"state": "CLOSED", "baseRefName": "release"}] * 1000)
        )

        with pytest.raises(RuntimeError, match="could not verify existing PR state"):
            _github_api_module._find_open_pr_for_head("768-auto-impl", "main")

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_successful_pr_creation_defers_auto_merge(
        self, mock_gh_call: Any, _mock_signed: Any
    ) -> None:
        """Test successful PR creation."""
        list_result = Mock()
        list_result.stdout = "[]"  # no existing PR on the head
        mock_create_result = Mock()
        mock_create_result.stdout = "https://github.com/owner/repo/pull/456"
        mock_gh_call.side_effect = [list_result, mock_create_result, _unarmed_pr_state()]

        pr_number = gh_pr_create(
            branch="feature-branch",
            title="Test PR",
            body=_POLICY_BODY,
        )

        assert pr_number == 456
        # Pre-flight `pr list`, creation, then verified unarmed read-back.
        assert mock_gh_call.call_count == 3
        assert "--base" in mock_gh_call.call_args_list[1].args[0]

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_fresh_pr_creation_propagates_a_failed_auto_merge_readback(
        self, mock_gh_call: Any, _mock_signed: Any
    ) -> None:
        """A fresh compatibility PR is not returned without verified containment."""
        mock_gh_call.side_effect = [
            Mock(stdout="[]"),
            Mock(stdout="https://github.com/owner/repo/pull/456"),
            Mock(
                returncode=0,
                stdout=json.dumps({"state": "OPEN", "autoMergeRequest": {"enabledAt": "now"}}),
            ),
            Mock(returncode=0, stdout="", stderr=""),
            Mock(
                returncode=0,
                stdout=json.dumps({"state": "OPEN", "autoMergeRequest": {"enabledAt": "still"}}),
            ),
        ]

        with pytest.raises(RuntimeError, match="could not verify auto-merge disabled"):
            gh_pr_create(branch="feature-branch", title="Test PR", body=_POLICY_BODY)

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_existing_pr_lookup_rejects_an_open_pr_without_a_numeric_number(
        self, mock_gh_call: Any, _mock_signed: Any
    ) -> None:
        """A malformed open-PR lookup cannot fall through to fresh creation."""
        mock_gh_call.return_value = Mock(stdout=json.dumps([{"state": "OPEN", "number": None}]))

        with pytest.raises(RuntimeError, match="could not verify existing PR state"):
            gh_pr_create(branch="feature-branch", title="Test PR", body=_POLICY_BODY)

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_existing_pr_lookup_rejects_a_record_without_state(
        self, mock_gh_call: Any, _mock_signed: Any
    ) -> None:
        """A partially parsed existing PR cannot be reinterpreted as no PR."""
        mock_gh_call.return_value = Mock(stdout=json.dumps([{"number": 962}]))

        with pytest.raises(RuntimeError, match="could not verify existing PR state"):
            gh_pr_create(branch="feature-branch", title="Test PR", body=_POLICY_BODY)

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_pr_creation_without_auto_merge(self, mock_gh_call: Any, _mock_signed: Any) -> None:
        """Test PR creation without auto-merge."""
        list_result = Mock()
        list_result.stdout = "[]"  # no existing PR on the head
        mock_result = Mock()
        mock_result.stdout = "https://github.com/owner/repo/pull/789"
        mock_gh_call.side_effect = [list_result, mock_result, _unarmed_pr_state()]

        pr_number = gh_pr_create(
            branch="feature-branch",
            title="Test PR",
            body=_POLICY_BODY,
            auto_merge=False,
        )

        assert pr_number == 789
        # Pre-flight dedup list + create + verified unarmed read-back.
        assert mock_gh_call.call_count == 3

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_pr_creation_with_fallback_parsing(self, mock_gh_call: Any, _mock_signed: Any) -> None:
        """Test PR number extraction fallback."""
        mock_result = Mock()
        # URL without /pull/ pattern
        mock_result.stdout = "https://github.com/owner/repo/123"
        list_result = Mock(stdout="[]")
        mock_gh_call.side_effect = [list_result, mock_result, _unarmed_pr_state()]

        pr_number = gh_pr_create(
            branch="feature-branch",
            title="Test PR",
            body=_POLICY_BODY,
            auto_merge=False,
        )

        assert pr_number == 123

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_gh_pr_create_returns_existing_open_pr_without_creating(
        self, mock_gh_call: Any, _mock_signed: Any
    ) -> None:
        """An OPEN PR already on the head is reused, not duplicated (issue #1018)."""
        list_result = Mock()
        list_result.stdout = json.dumps([{"number": 962, "state": "OPEN", "baseRefName": "main"}])
        state_result = Mock()
        state_result.returncode = 0
        state_result.stdout = json.dumps({"state": "OPEN", "autoMergeRequest": None})
        mock_gh_call.side_effect = [list_result, state_result]

        pr_number = gh_pr_create(
            branch="768-auto-impl",
            title="Test PR",
            body=_POLICY_BODY,
        )

        assert pr_number == 962
        # The pre-flight `pr list` and containment read ran; no `pr create`.
        assert mock_gh_call.call_count == 2
        for call in mock_gh_call.call_args_list:
            assert "create" not in call.args[0]

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_gh_pr_create_disables_auto_merge_before_reusing_existing_pr(
        self, mock_gh_call: Any, _mock_signed: Any
    ) -> None:
        """Reusing a pre-#2054 PR contains any existing auto-merge arm first."""
        list_result = Mock(
            stdout=json.dumps([{"number": 962, "state": "OPEN", "baseRefName": "main"}])
        )
        mock_gh_call.side_effect = [
            list_result,
            Mock(
                returncode=0,
                stdout=json.dumps({"state": "OPEN", "autoMergeRequest": {"enabledAt": "now"}}),
            ),
            Mock(returncode=0, stdout="", stderr=""),
            Mock(returncode=0, stdout=json.dumps({"state": "OPEN", "autoMergeRequest": None})),
        ]

        assert gh_pr_create(branch="768-auto-impl", title="Test PR", body=_POLICY_BODY) == 962
        assert [call.args[0][:3] for call in mock_gh_call.call_args_list[1:]] == [
            ["pr", "view", "962"],
            ["pr", "merge", "962"],
            ["pr", "view", "962"],
        ]

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_gh_pr_create_proceeds_when_only_closed_pr_exists(
        self, mock_gh_call: Any, _mock_signed: Any
    ) -> None:
        """A closed-only head still gets a fresh PR (issue #1018)."""
        list_result = Mock()
        list_result.stdout = json.dumps([{"number": 942, "state": "CLOSED", "baseRefName": "main"}])
        create_result = Mock()
        create_result.stdout = "https://github.com/owner/repo/pull/967"
        mock_gh_call.side_effect = [list_result, create_result, _unarmed_pr_state()]

        pr_number = gh_pr_create(
            branch="768-auto-impl",
            title="Test PR",
            body=_POLICY_BODY,
        )

        assert pr_number == 967
        # The create call WAS made.
        assert any("create" in c.args[0] for c in mock_gh_call.call_args_list)

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_gh_pr_create_proceeds_when_no_existing_pr(
        self, mock_gh_call: Any, _mock_signed: Any
    ) -> None:
        """No PR on the head → create as usual (issue #1018)."""
        list_result = Mock()
        list_result.stdout = "[]"
        create_result = Mock()
        create_result.stdout = "https://github.com/owner/repo/pull/100"
        mock_gh_call.side_effect = [list_result, create_result, _unarmed_pr_state()]

        pr_number = gh_pr_create(
            branch="feature-branch",
            title="Test PR",
            body=_POLICY_BODY,
        )

        assert pr_number == 100
        assert any("create" in c.args[0] for c in mock_gh_call.call_args_list)

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_pr_creation_with_auto_merge_flag_remains_unarmed(
        self, mock_gh_call: Any, _mock_signed: Any
    ) -> None:
        """The deprecated compatibility flag cannot bypass the strict gate."""
        list_result = Mock()
        list_result.stdout = "[]"  # no existing PR on the head
        mock_create_result = Mock()
        mock_create_result.stdout = "https://github.com/owner/repo/pull/456"

        mock_gh_call.side_effect = [list_result, mock_create_result, _unarmed_pr_state()]

        assert (
            gh_pr_create(
                branch="feature-branch",
                title="Test PR",
                body=_POLICY_BODY,
                auto_merge=True,
            )
            == 456
        )
        assert mock_gh_call.call_count == 3

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_rejects_body_without_closes(self, mock_gh_call: Any, _mock_signed: Any) -> None:
        """A PR body lacking 'Closes #N' must raise before any gh call."""
        with pytest.raises(ValueError, match="Closes #N"):
            gh_pr_create(
                branch="feature-branch",
                title="Test PR",
                body="## Summary\nNo issue link here\n",
                auto_merge=True,
            )
        mock_gh_call.assert_not_called()

    @pytest.mark.parametrize(
        "body",
        [
            "## Summary\nfix\n\nFixes #1\n",
            "## Summary\nfix\n\nResolves #1\n",
            "## Summary\nfix\n\ncloses #1\n",
            "## Summary\nfix\n\nCloses: #1\n",
            "## Summary\nfix\n\nSee Closes #1 mid-line\n",
        ],
    )
    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_rejects_close_variants(self, mock_gh_call: Any, _mock_signed: Any, body: str) -> None:
        """Only the literal 'Closes #N' on its own line satisfies policy."""
        with pytest.raises(ValueError, match="Closes #N"):
            gh_pr_create(
                branch="feature-branch",
                title="Test PR",
                body=body,
                auto_merge=True,
            )
        mock_gh_call.assert_not_called()

    @patch("hephaestus.automation.github_api._gh_commit_is_verified", return_value=False)
    @patch("hephaestus.automation.github_api.run")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_rejects_unsigned_commit(
        self, mock_gh_call: Any, mock_run: Any, _mock_verified: Any
    ) -> None:
        """An 'N' commit that GitHub also reports unverified must abort PR creation."""
        # First run() call: git fetch (best-effort, contextlib.suppress); ignored
        # Second run() call: git log --format='%H %G?' against origin/<base>
        fetch_result = Mock(returncode=0, stdout="", stderr="")
        log_result = Mock(returncode=0, stdout="aaa111bbb N\nccc222ddd G\n", stderr="")
        mock_run.side_effect = [fetch_result, log_result]

        with pytest.raises(ValueError, match="Unsigned or invalid commits"):
            gh_pr_create(
                branch="feature-branch",
                title="Test PR",
                body=_POLICY_BODY,
                auto_merge=True,
            )
        # PR creation (_gh_call) must not run once signing fails.
        mock_gh_call.assert_not_called()

    @patch("hephaestus.automation.github_api._gh_commit_is_verified", return_value=False)
    @patch("hephaestus.automation.github_api.run")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_rejects_bad_signature(
        self, mock_gh_call: Any, mock_run: Any, _mock_verified: Any
    ) -> None:
        """A 'B' commit that GitHub also reports unverified must abort PR creation."""
        fetch_result = Mock(returncode=0, stdout="", stderr="")
        log_result = Mock(returncode=0, stdout="aaa111bbb B\n", stderr="")
        mock_run.side_effect = [fetch_result, log_result]

        with pytest.raises(ValueError, match="Unsigned or invalid commits"):
            gh_pr_create(
                branch="feature-branch",
                title="Test PR",
                body=_POLICY_BODY,
                auto_merge=True,
            )
        mock_gh_call.assert_not_called()

    @patch("hephaestus.automation.github_api.run")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_accepts_good_untrusted_signature(self, mock_gh_call: Any, mock_run: Any) -> None:
        """'U' (good sig, untrusted key) is accepted; GitHub re-validates server-side."""
        fetch_result = Mock(returncode=0, stdout="", stderr="")
        log_result = Mock(returncode=0, stdout="aaa111bbb U\nccc222ddd G\n", stderr="")
        mock_run.side_effect = [fetch_result, log_result]

        list_result = Mock(stdout="[]")  # no existing PR on the head
        mock_create_result = Mock(stdout="https://github.com/owner/repo/pull/42")
        mock_gh_call.side_effect = [list_result, mock_create_result, _unarmed_pr_state()]

        pr_number = gh_pr_create(
            branch="feature-branch",
            title="Test PR",
            body=_POLICY_BODY,
        )
        assert pr_number == 42
        # Pre-flight dedup list + create + verified unarmed read-back.
        assert mock_gh_call.call_count == 3


class TestAssertBranchCommitsSignedApiFallback:
    """SSH-signed commits the local checkout can't verify must not false-NOGO.

    When a commit is SSH-signed but ``gpg.ssh.allowedSignersFile`` is not
    configured locally, ``git log --format=%G?`` returns ``N`` (or ``E``) even
    though GitHub has authoritatively verified the signature. The local check
    must consult the GitHub commit-verification API before declaring a policy
    violation, since GitHub's ``verified`` flag is the source of truth at PR
    time (the same rationale that makes ``U`` acceptable). Regression for the
    implementer false-NOGO on pre-existing SSH-signed branches.
    """

    @patch("hephaestus.automation.github_api._gh_commit_is_verified")
    @patch("hephaestus.automation.github_api.run")
    def test_local_unverifiable_but_github_verified_is_accepted(
        self, mock_run: Any, mock_verified: Any
    ) -> None:
        from hephaestus.automation.github_api import _assert_branch_commits_signed

        fetch_result = Mock(returncode=0, stdout="", stderr="")
        # Local can't verify the SSH signature -> 'N'.
        log_result = Mock(returncode=0, stdout="aaa111bbb N\n", stderr="")
        mock_run.side_effect = [fetch_result, log_result]
        # GitHub says it's verified.
        mock_verified.return_value = True

        # Must NOT raise.
        _assert_branch_commits_signed("feature-branch", base="main")
        mock_verified.assert_called_once_with("aaa111bbb")

    @patch("hephaestus.automation.github_api._gh_commit_is_verified")
    @patch("hephaestus.automation.github_api.run")
    def test_local_unverifiable_and_github_unverified_still_raises(
        self, mock_run: Any, mock_verified: Any
    ) -> None:
        from hephaestus.automation.github_api import _assert_branch_commits_signed

        fetch_result = Mock(returncode=0, stdout="", stderr="")
        log_result = Mock(returncode=0, stdout="aaa111bbb N\n", stderr="")
        mock_run.side_effect = [fetch_result, log_result]
        # GitHub also says unverified (genuinely unsigned) -> policy violation.
        mock_verified.return_value = False

        with pytest.raises(ValueError, match="Unsigned or invalid commits"):
            _assert_branch_commits_signed("feature-branch", base="main")

    @patch("hephaestus.automation.github_api._gh_commit_is_verified")
    @patch("hephaestus.automation.github_api.run")
    def test_good_local_signature_skips_api_call(self, mock_run: Any, mock_verified: Any) -> None:
        from hephaestus.automation.github_api import _assert_branch_commits_signed

        fetch_result = Mock(returncode=0, stdout="", stderr="")
        log_result = Mock(returncode=0, stdout="aaa111bbb G\n", stderr="")
        mock_run.side_effect = [fetch_result, log_result]

        _assert_branch_commits_signed("feature-branch", base="main")
        # 'G' is locally good — no API round-trip needed.
        mock_verified.assert_not_called()

    @patch("hephaestus.automation.github_api._gh_commit_is_verified")
    @patch("hephaestus.automation.github_api.run")
    def test_branch_only_resolvable_as_origin_ref_does_not_crash(
        self, mock_run: Any, mock_verified: Any
    ) -> None:
        """Regression #2108: bare <branch> absent, origin/<branch> present."""
        from hephaestus.automation.github_api import _assert_branch_commits_signed

        fetch_result = Mock(returncode=0, stdout="", stderr="")
        # First candidate origin/main..origin/branch resolves; commits are signed.
        log_ok = Mock(returncode=0, stdout="5eb2be44 G\n", stderr="")
        mock_run.side_effect = [fetch_result, log_ok]

        # Must NOT raise CalledProcessError or ValueError.
        _assert_branch_commits_signed("2107-auto-impl", base="main")
        mock_verified.assert_not_called()

    @patch("hephaestus.automation.github_api._gh_commit_is_verified")
    @patch("hephaestus.automation.github_api.run")
    def test_no_range_resolves_defers_without_crashing(
        self, mock_run: Any, mock_verified: Any
    ) -> None:
        """Regression #2108: no candidate range resolves -> warn, no crash."""
        from hephaestus.automation.github_api import _assert_branch_commits_signed

        fetch_result = Mock(returncode=0, stdout="", stderr="")
        fail = Mock(returncode=128, stdout="", stderr="fatal: ambiguous argument")
        # fetch + 4 candidate ranges all fail.
        mock_run.side_effect = [fetch_result, fail, fail, fail, fail]

        # Must NOT raise; defers to GitHub verification.
        _assert_branch_commits_signed("2107-auto-impl", base="main")
        mock_verified.assert_not_called()

    @patch("hephaestus.automation.github_api._gh_commit_is_verified")
    @patch("hephaestus.automation.github_api.run")
    def test_genuinely_unsigned_still_raises_after_resolution(
        self, mock_run: Any, mock_verified: Any
    ) -> None:
        """Unsigned commits on a resolvable range still raise (unchanged)."""
        from hephaestus.automation.github_api import _assert_branch_commits_signed

        fetch_result = Mock(returncode=0, stdout="", stderr="")
        log_bad = Mock(returncode=0, stdout="deadbeef N\n", stderr="")
        mock_run.side_effect = [fetch_result, log_bad]
        mock_verified.return_value = False

        with pytest.raises(ValueError, match="Unsigned or invalid commits"):
            _assert_branch_commits_signed("2107-auto-impl", base="main")


class TestGhCommitIsVerified:
    """``_gh_commit_is_verified`` fail-safe behavior (#1426)."""

    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_returns_true_when_github_reports_verified(self, mock_gh: Any, _mock_info: Any) -> None:
        from hephaestus.automation.github_api import _gh_commit_is_verified

        mock_gh.return_value = Mock(stdout="true\n")
        assert _gh_commit_is_verified("deadbeef") is True

    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch(
        "hephaestus.automation.github_api._gh_call",
        side_effect=RuntimeError("api down"),
    )
    def test_returns_false_on_lookup_failure(self, _mock_gh: Any, _mock_info: Any) -> None:
        """#1426: a gh lookup failure is logged and fail-safe returns False.

        Covers the formerly ``# pragma: no cover`` fallback handler; the caller
        falls back to the strict local verdict (treated as unverified).
        """
        from hephaestus.automation.github_api import _gh_commit_is_verified

        assert _gh_commit_is_verified("deadbeef") is False


class TestWriteSecureCompatibility:
    """Compatibility coverage for the historical github_api import path."""

    def test_import_path_is_canonical_io_helper(self) -> None:
        """The historical named import should resolve to the canonical helper."""
        module = __import__("hephaestus.automation.github_api", fromlist=["write_secure"])
        assert module.write_secure is io_utils.write_secure


class TestGhCallThrottle:
    """Tests for the per-thread `gh` call throttle inside _gh_call."""

    @pytest.fixture(autouse=True)
    def _reset_throttle(self, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
        # Reset the per-thread throttle state and force a known rate so tests
        # don't inherit clock state from earlier suite calls.
        client_module._GH_THROTTLE = threading.local()
        monkeypatch.setenv("GH_RATE_LIMIT_PER_SEC", "5")
        configure_gh_global_throttle(rate=0, burst=10)
        yield
        configure_gh_global_throttle(rate=10.0, burst=30.0)

    @patch("hephaestus.github.client.run_subprocess")
    def test_consecutive_calls_are_paced_to_min_interval(self, mock_run: Any) -> None:
        """Pace consecutive calls to the configured min interval.

        At 5 calls/sec, two back-to-back calls in the same thread must be
        separated by at least ~0.2s.
        """
        mock_run.return_value = Mock(stdout="", stderr="", returncode=0)

        import time as _time

        t0 = _time.monotonic()
        _gh_call(["api", "/rate_limit"])
        _gh_call(["api", "/rate_limit"])
        elapsed = _time.monotonic() - t0

        # Allow a small slack below the theoretical 0.2s for clock granularity.
        assert elapsed >= 0.18, f"throttle did not pace; elapsed={elapsed:.3f}s"

    @patch("hephaestus.github.client.run_subprocess")
    def test_throttle_disabled_when_rate_zero(
        self, mock_run: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GH_RATE_LIMIT_PER_SEC=0 disables pacing entirely."""
        monkeypatch.setenv("GH_RATE_LIMIT_PER_SEC", "0")
        mock_run.return_value = Mock(stdout="", stderr="", returncode=0)

        with patch("hephaestus.github.client.time.sleep") as mock_sleep:
            for _ in range(5):
                _gh_call(["api", "/rate_limit"])

        assert mock_run.call_count == 5
        mock_sleep.assert_not_called()

    @patch("hephaestus.github.client.run_subprocess")
    def test_buckets_are_per_thread(self, mock_run: Any) -> None:
        """Each thread has its own bucket.

        Two threads each making one call should not block each other.
        """
        import threading as _t
        import time as _time

        mock_run.return_value = Mock(stdout="", stderr="", returncode=0)

        # Pre-warm thread A's bucket so a second call from A would block.
        _gh_call(["api", "/rate_limit"])

        elapsed_b: list[float] = []

        def thread_b() -> None:
            t0 = _time.monotonic()
            _gh_call(["api", "/rate_limit"])
            elapsed_b.append(_time.monotonic() - t0)

        worker = _t.Thread(target=thread_b)
        worker.start()
        worker.join()

        # Thread B has its own bucket and should not have been throttled.
        assert elapsed_b[0] < 0.05, (
            f"per-thread isolation broken; thread B waited {elapsed_b[0]:.3f}s"
        )


class TestGraphQLRateLimitDetection:
    """Tests for GitHubRateLimitError raised from GraphQL JSON payloads."""

    def test_raises_on_rate_limited_type(self) -> None:
        data = {"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}
        with patch(
            "hephaestus.automation.github_api.gh_rate_limit_reset_epoch",
            return_value=1700000000,
        ):
            with pytest.raises(GitHubRateLimitError) as ei:
                _check_graphql_errors(data, "test")
        assert ei.value.reset_epoch == 1700000000

    def test_raises_on_rate_limit_in_message(self) -> None:
        data = {"errors": [{"message": "API rate limit exceeded for user 1"}]}
        with patch(
            "hephaestus.automation.github_api.gh_rate_limit_reset_epoch",
            return_value=None,
        ):
            with pytest.raises(GitHubRateLimitError) as ei:
                _check_graphql_errors(data, "ctx")
        # Probe returned None → reset_epoch sentinel 0
        assert ei.value.reset_epoch == 0

    def test_raises_runtimeerror_for_other_errors(self) -> None:
        data = {"errors": [{"type": "FORBIDDEN", "message": "no perms"}]}
        with pytest.raises(RuntimeError) as ei:
            _check_graphql_errors(data, "ctx")
        assert not isinstance(ei.value, GitHubRateLimitError)

    def test_no_raise_when_no_errors(self) -> None:
        _check_graphql_errors({"data": {"viewer": {"login": "x"}}}, "ctx")


class TestGhCallRateLimitFromStdout:
    """Tests for _gh_call detecting rate-limit messages on stdout.

    These tests mock both the per-thread and cross-process throttles, the
    real wait callsite, and ``gh_rate_limit_reset_epoch`` in the namespace
    where ``detect_rate_limit`` looks it up. Skipping any one of those
    mocks lets the test reach real I/O or a wait loop — the latter, in
    combination with mocking ``time.sleep`` at the module level, can
    runaway-print until the process hits OOM (because ``time`` is shared
    and ``wait_until`` polls in ``while True``).
    """

    @patch("hephaestus.github.rate_limit.gh_rate_limit_reset_epoch")
    @patch("hephaestus.github.client.gh_global_throttle_acquire")
    @patch("hephaestus.github.client.run_subprocess")
    @patch("hephaestus.github.client.wait_until")
    def test_retries_on_graphql_message_in_stdout(
        self,
        mock_wait: Any,
        mock_run: Any,
        _mock_throttle: Any,
        mock_probe: Any,
    ) -> None:
        """Cover detection of GraphQL rate-limit messages on stderr.

        ``gh issue list`` emits the GraphQL message on stderr; the JSON
        payload may also appear on stdout in some failure modes — both must
        be inspected.
        """
        mock_probe.return_value = 1_700_000_000  # arbitrary past epoch
        mock_run.side_effect = [
            subprocess.CalledProcessError(
                1,
                "gh",
                "",
                "GraphQL: API rate limit already exceeded for user ID 1",
            ),
            Mock(stdout="ok"),
        ]
        result = _gh_call(["issue", "list"])
        assert result.stdout == "ok"
        assert mock_run.call_count == 2
        mock_wait.assert_called_once()

    @patch("hephaestus.github.rate_limit.gh_rate_limit_reset_epoch")
    @patch("hephaestus.github.client.gh_global_throttle_acquire")
    @patch("hephaestus.github.client.run_subprocess")
    @patch("hephaestus.github.client.wait_until")
    @patch("hephaestus.github.client.time.sleep")
    def test_raises_after_exhausting_retries(
        self,
        _mock_sleep: Any,
        _mock_wait: Any,
        mock_run: Any,
        _mock_throttle: Any,
        mock_probe: Any,
    ) -> None:
        mock_probe.return_value = None  # forces detect_rate_limit -> 0 sentinel
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", "", "GraphQL: API rate limit already exceeded for user ID 1"
        )
        with pytest.raises(GitHubRateLimitError):
            _gh_call(["issue", "list"], max_retries=2)


# Valid field set for `gh pr checks --json`, captured from the gh CLI schema.
# Any field the code requests MUST be a subset of this, or gh rejects the whole
# call with "Unknown JSON field" (the bug behind issue #654).
_GH_PR_CHECKS_VALID_FIELDS = {
    "bucket",
    "completedAt",
    "description",
    "event",
    "link",
    "name",
    "startedAt",
    "state",
    "workflow",
}


class TestGhPrChecks:
    """Tests for gh_pr_checks: schema validity and bucket->contract mapping."""

    def test_dry_run_returns_empty(self) -> None:
        assert gh_pr_checks(123, dry_run=True) == []

    @patch("hephaestus.automation.github_api._gh_call")
    def test_requested_json_fields_are_valid_schema(self, mock_gh_call: Any) -> None:
        """The --json field list must be a subset of gh's real schema."""
        mock_result = Mock()
        mock_result.stdout = "[]"
        mock_gh_call.return_value = mock_result

        gh_pr_checks(123)

        args = mock_gh_call.call_args.args[0]
        json_idx = args.index("--json")
        requested = set(args[json_idx + 1].split(","))
        invalid = requested - _GH_PR_CHECKS_VALID_FIELDS
        assert not invalid, f"gh pr checks requested invalid --json field(s): {invalid}"

    @patch("hephaestus.automation.github_api._gh_call")
    def test_maps_bucket_to_status_and_conclusion(self, mock_gh_call: Any) -> None:
        """state/bucket from gh map onto the status/conclusion contract."""
        mock_result = Mock()
        mock_result.stdout = json.dumps(
            [
                {"name": "pass-check", "state": "SUCCESS", "bucket": "pass", "workflow": ""},
                {"name": "fail-check", "state": "FAILURE", "bucket": "fail", "workflow": "CI"},
                {"name": "skip-check", "state": "SKIPPED", "bucket": "skipping", "workflow": ""},
                {"name": "pend-check", "state": "PENDING", "bucket": "pending", "workflow": ""},
                {"name": "cancel-check", "state": "CANCELLED", "bucket": "cancel", "workflow": ""},
            ]
        )
        mock_gh_call.return_value = mock_result

        checks = gh_pr_checks(456)
        by_name = {c["name"]: c for c in checks}

        assert by_name["pass-check"]["status"] == "completed"
        assert by_name["pass-check"]["conclusion"] == "success"
        assert by_name["fail-check"]["status"] == "completed"
        assert by_name["fail-check"]["conclusion"] == "failure"
        assert by_name["skip-check"]["status"] == "completed"
        assert by_name["skip-check"]["conclusion"] == "skipped"
        assert by_name["cancel-check"]["status"] == "completed"
        assert by_name["cancel-check"]["conclusion"] == "failure"
        # Pending checks are not yet concluded.
        assert by_name["pend-check"]["status"] == "in_progress"
        assert by_name["pend-check"]["conclusion"] is None
        # Every check exposes the contract keys consumed by ci_driver.
        for c in checks:
            assert set(c) == {"name", "status", "conclusion", "required"}

    @patch("hephaestus.automation.github_api._gh_call")
    def test_unknown_bucket_treated_as_pending(self, mock_gh_call: Any) -> None:
        """An unrecognised bucket degrades to in_progress, never a false 'completed'."""
        mock_result = Mock()
        mock_result.stdout = json.dumps(
            [{"name": "weird", "state": "QUEUED", "bucket": "something-new", "workflow": ""}]
        )
        mock_gh_call.return_value = mock_result

        (check,) = gh_pr_checks(789)
        assert check["status"] == "in_progress"
        assert check["conclusion"] is None

    @patch("hephaestus.automation.github_api._gh_call")
    def test_no_checks_reported_returns_empty(self, mock_gh_call: Any) -> None:
        """Regression for #827: gh's 'no checks reported' stderr maps to ``[]``.

        ``gh pr checks`` exits non-zero with stderr ``no checks reported on the
        '<branch>' branch`` when the PR has no check runs registered yet (fresh
        PR, no workflows configured, etc.). Previously this aborted the entire
        CI drive with an unexpected-error log. The driver now treats it as the
        empty-result case.
        """
        mock_gh_call.side_effect = subprocess.CalledProcessError(
            1,
            ["gh", "pr", "checks"],
            output="",
            stderr="no checks reported on the '45-ecosystem-health' branch\n",
        )

        assert gh_pr_checks(289) == []

    @patch("hephaestus.automation.github_api._gh_call")
    def test_no_checks_path_suppresses_error_logging(self, mock_gh_call: Any) -> None:
        """#1587: gh_pr_checks calls _gh_call with log_on_error=False.

        Combined with the 'no checks reported' non-transient pattern in
        github.client, this makes the expected post-push empty state fail FAST
        with no ERROR log and no exponential-backoff retry.
        """
        mock_result = Mock()
        mock_result.stdout = "[]"
        mock_gh_call.return_value = mock_result

        gh_pr_checks(289)

        assert mock_gh_call.call_args.kwargs.get("log_on_error") is False

    @patch("hephaestus.automation.github_api._gh_call")
    def test_other_called_process_error_still_raises(self, mock_gh_call: Any) -> None:
        """Auth / network / unrelated gh failures still propagate."""
        # We only swallow the exact "no checks reported" stderr — every other
        # CalledProcessError still surfaces so genuine failures fail loud.
        mock_gh_call.side_effect = subprocess.CalledProcessError(
            128,
            ["gh", "pr", "checks"],
            output="",
            stderr="HTTP 401: Bad credentials\n",
        )

        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            gh_pr_checks(289)
        assert exc_info.value.returncode == 128


class TestFetchIssueCommentIdsRepoScoping:
    """``_fetch_issue_comment_ids`` must query the OWNING repo, not the ambient CWD (#1795).

    The multi-repo loop calls this helper with issue numbers belonging to other
    repositories. Resolving the repo from the process CWD sent every query to
    Hephaestus, producing 404s (or, where issue numbers collide, silently
    reading an unrelated issue/PR in the wrong repo).
    """

    @staticmethod
    def _graphql_vars(argv: list[str]) -> dict[str, str]:
        """Extract the ``-F key=value`` GraphQL variables from a gh argv."""
        return dict(argv[i + 1].split("=", 1) for i, tok in enumerate(argv) if tok == "-F")

    @patch("hephaestus.automation.github_api.get_repo_info")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_explicit_repo_is_queried_not_ambient_cwd(
        self, mock_gh_call: Any, mock_repo_info: Any
    ) -> None:
        """An explicit ``repo=`` scopes the query and bypasses ambient resolution."""
        mock_repo_info.return_value = ("HomericIntelligence", "Hephaestus")
        mock_gh_call.return_value = Mock(
            stdout=json.dumps({"data": {"repository": {"issue": {"comments": {"nodes": []}}}}})
        )

        _github_api_module._fetch_issue_comment_ids(188, repo=("HomericIntelligence", "Myrmidons"))

        argv = mock_gh_call.call_args[0][0]
        variables = self._graphql_vars(argv)
        assert variables["owner"] == "HomericIntelligence"
        assert variables["name"] == "Myrmidons", (
            "query must target the owning repo, not the ambient CWD repo"
        )
        assert variables["number"] == "188"
        mock_repo_info.assert_not_called()

    @patch("hephaestus.automation.github_api.get_repo_info")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_omitted_repo_falls_back_to_ambient_cwd(
        self, mock_gh_call: Any, mock_repo_info: Any
    ) -> None:
        """Back-compat: no ``repo=`` keeps the historical ambient behaviour."""
        mock_repo_info.return_value = ("HomericIntelligence", "Hephaestus")
        mock_gh_call.return_value = Mock(
            stdout=json.dumps({"data": {"repository": {"issue": {"comments": {"nodes": []}}}}})
        )

        _github_api_module._fetch_issue_comment_ids(42)

        variables = self._graphql_vars(mock_gh_call.call_args[0][0])
        assert variables["name"] == "Hephaestus"
        mock_repo_info.assert_called_once()


class TestUpsertAndDeleteComment:
    """Idempotent plan/review comment lifecycle (one comment per role)."""

    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("o", "r"))
    @patch("hephaestus.automation.github_api._fetch_issue_comment_ids", return_value=[])
    @patch("hephaestus.automation.github_api.gh_issue_comment")
    def test_upsert_creates_when_absent(
        self, mock_create: Any, _mock_fetch: Any, _mock_repo: Any
    ) -> None:
        rv = gh_issue_upsert_comment(5, "# Implementation Plan", "# Implementation Plan\nbody")
        mock_create.assert_called_once_with(5, "# Implementation Plan\nbody")
        assert rv is None  # fresh create: id not parsed

    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("o", "r"))
    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.gh_issue_comment")
    def test_upsert_patches_existing(
        self, mock_create: Any, mock_gh_call: Any, _mock_repo: Any
    ) -> None:
        with patch(
            "hephaestus.automation.github_api._fetch_issue_comment_ids",
            return_value=[{"databaseId": 99, "body": "# Implementation Plan\nold"}],
        ):
            rv = gh_issue_upsert_comment(5, "# Implementation Plan", "# Implementation Plan\nnew")
        # No fresh comment created; a PATCH call was issued for id 99.
        mock_create.assert_not_called()
        assert rv == 99
        patched = any(
            "PATCH" in str(c) and "issues/comments/99" in str(c)
            for c in mock_gh_call.call_args_list
        )
        assert patched, mock_gh_call.call_args_list

    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("o", "r"))
    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.gh_issue_comment")
    def test_upsert_deletes_older_duplicates(
        self, _mock_create: Any, mock_gh_call: Any, _mock_repo: Any
    ) -> None:
        # Three legacy plan comments → newest (id 3) patched, 1 and 2 deleted.
        with patch(
            "hephaestus.automation.github_api._fetch_issue_comment_ids",
            return_value=[
                {"databaseId": 1, "body": "# Implementation Plan\na"},
                {"databaseId": 2, "body": "# Implementation Plan\nb"},
                {"databaseId": 3, "body": "# Implementation Plan\nc"},
            ],
        ):
            rv = gh_issue_upsert_comment(5, "# Implementation Plan", "# Implementation Plan\nnew")
        assert rv == 3
        calls = [str(c) for c in mock_gh_call.call_args_list]
        assert any("DELETE" in c and "comments/1" in c for c in calls), calls
        assert any("DELETE" in c and "comments/2" in c for c in calls), calls
        assert any("PATCH" in c and "comments/3" in c for c in calls), calls

    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("o", "r"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_delete_comment_calls_rest_delete(self, mock_gh_call: Any, _mock_repo: Any) -> None:
        gh_issue_delete_comment(42)
        (args,) = mock_gh_call.call_args.args
        assert "DELETE" in args
        assert "/repos/o/r/issues/comments/42" in args


class TestGhPrReviewPost:
    """gh_pr_review_post: post a review and return *this* review's thread IDs."""

    @staticmethod
    def _gh_call_side_effect(
        review_id: str, threads: list[dict[str, Any]], diff_text: str = ""
    ) -> Any:
        """Build a side_effect covering the _gh_call invocations.

        1. ``gh pr diff {n}`` → unified diff text used for hunk validation
           (empty by default, so validation fails open and posts unchanged).
        2. ``POST /pulls/{n}/reviews`` (REST) → ``{id, node_id}``. The review
           body is delivered via ``--input <file>`` and natively supports
           ``line``/``side`` inline comments and empty comment lists.
        3. ``reviewThreads`` follow-up GraphQL query, matched on the returned
           review node id.
        """

        def _side_effect(args: list[str], *_: Any, **__: Any) -> Any:
            joined = " ".join(args)
            result = Mock()
            if "/reviews" in joined:
                # REST review POST returns numeric id + GraphQL node_id.
                result.stdout = json.dumps({"id": 999, "node_id": review_id})
            elif "reviewThreads" in joined:
                result.stdout = json.dumps(
                    {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": threads}}}}}
                )
            elif "diff" in args:
                # No diff text by default → diff-hunk validation fails open
                # (comments are posted unchanged), preserving prior behaviour.
                result.stdout = diff_text
            else:  # pragma: no cover - defensive
                result.stdout = "{}"
            return result

        return _side_effect

    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_mutation_does_not_reference_nonexistent_field(
        self, mock_gh_call: Any, _mock_repo: Any
    ) -> None:
        """No GraphQL query sent must select the invalid field.

        Regression: ``Field 'pullRequestReviewThread' doesn't exist on type
        'PullRequestReviewComment'`` failed every review post.
        """
        mock_gh_call.side_effect = self._gh_call_side_effect("REVIEW_1", [])

        gh_pr_review_post(
            pr_number=7,
            comments=[{"path": "a.py", "line": 1, "side": "RIGHT", "body": "fix"}],
            summary="Findings",
        )

        for call in mock_gh_call.call_args_list:
            sent_args = call.args[0] if call.args else call.kwargs.get("args", [])
            assert "pullRequestReviewThread" not in " ".join(sent_args)

    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_returns_only_this_reviews_unresolved_threads(
        self, mock_gh_call: Any, _mock_repo: Any
    ) -> None:
        """Threads from this review (unresolved) are returned; others are excluded."""
        threads = [
            # Belongs to our review, unresolved → kept.
            {
                "id": "T_mine_open",
                "isResolved": False,
                "comments": {"nodes": [{"pullRequestReview": {"id": "REVIEW_1"}}]},
            },
            # Our review but already resolved → excluded.
            {
                "id": "T_mine_resolved",
                "isResolved": True,
                "comments": {"nodes": [{"pullRequestReview": {"id": "REVIEW_1"}}]},
            },
            # Pre-existing human-reviewer thread (#375) → excluded.
            {
                "id": "T_foreign",
                "isResolved": False,
                "comments": {"nodes": [{"pullRequestReview": {"id": "REVIEW_OTHER"}}]},
            },
        ]
        mock_gh_call.side_effect = self._gh_call_side_effect("REVIEW_1", threads)

        thread_ids = gh_pr_review_post(
            pr_number=7,
            comments=[{"path": "a.py", "line": 1, "side": "RIGHT", "body": "fix"}],
            summary="Findings",
        )

        assert thread_ids == ["T_mine_open"]

    @patch("hephaestus.automation.github_api._gh_call")
    def test_dry_run_posts_nothing(self, mock_gh_call: Any) -> None:
        thread_ids = gh_pr_review_post(pr_number=7, comments=[], summary="x", dry_run=True)
        assert thread_ids == []
        mock_gh_call.assert_not_called()

    @staticmethod
    def _review_post_call(mock_gh_call: Any) -> Any:
        """Return the _gh_call invocation that POSTed the review via REST.

        The review body is delivered via ``gh api -X POST .../reviews --input
        <file>``; ``--input`` files are unlinked after the call, so the body must
        be inspected from inside the side_effect, not here.
        """
        for call in mock_gh_call.call_args_list:
            sent_args = call.args[0] if call.args else call.kwargs.get("args", [])
            if any(isinstance(a, str) and a.endswith("/reviews") for a in sent_args):
                return call
        raise AssertionError("review POST was never sent")

    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_comments_sent_as_typed_json_body_not_stringified_field(
        self, mock_gh_call: Any, _mock_repo: Any
    ) -> None:
        """Inline comments must reach gh as a typed JSON body, never as a stringified ``-f`` field.

        Regression: ``gh api graphql -f comments='[{...}]'`` sent the array as a
        *string*, so GitHub rejected every review post ("Variable $comments ...
        was provided invalid value") and the loop saw a spurious NOGO. The review
        is now POSTed to ``/pulls/{n}/reviews`` with the body delivered via
        ``--input`` (a typed JSON body), and ``line``/``side`` are sent verbatim.
        """
        mock_gh_call.side_effect = self._gh_call_side_effect("REVIEW_1", [])

        gh_pr_review_post(
            pr_number=7,
            comments=[{"path": "a.py", "line": 1, "side": "RIGHT", "body": "fix"}],
            summary="Findings",
        )

        sent_args = self._review_post_call(mock_gh_call).args[0]
        # No stringified comments field anywhere in argv.
        assert not any(isinstance(a, str) and a.startswith("comments=") for a in sent_args), (
            "comments must not be passed as a stringified field"
        )
        assert not any(
            isinstance(a, str) and a.startswith("-f") and "comments" in a for a in sent_args
        )
        # The typed body is delivered via --input to the REST reviews endpoint.
        assert "--input" in sent_args, "review body must be sent via --input"
        assert any(isinstance(a, str) and a.endswith("/reviews") for a in sent_args)

    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_empty_comments_still_posts_summary_review(
        self, mock_gh_call: Any, _mock_repo: Any
    ) -> None:
        """An empty ``comments`` list must post a summary-only review, not crash.

        Regression: even ``comments=[]`` failed under the stringified GraphQL form
        (``Expected "[]" to be a key-value object``), so summary-only NOGO reviews
        could never post and the loop always saw a spurious failure.
        """
        mock_gh_call.side_effect = self._gh_call_side_effect("REVIEW_1", [])

        # Must not raise.
        gh_pr_review_post(pr_number=7, comments=[], summary="Summary only")

        # The review POST was still sent.
        self._review_post_call(mock_gh_call)

    # ------------------------------------------------------------------
    # #1039: filter inline comments to lines present in the diff hunks.
    # ------------------------------------------------------------------

    # A minimal unified diff: a.py gains lines 1-3 on the RIGHT side.
    _SAMPLE_DIFF = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,3 @@\n+one\n+two\n+three\n"
    )

    @staticmethod
    def _posted_comments(mock_write: Any) -> list[dict[str, Any]]:
        """Extract the ``comments`` array from the review body write_secure saw."""
        for call in mock_write.call_args_list:
            # write_secure(path, body) — body is the JSON review payload.
            body = call.args[1] if len(call.args) > 1 else call.kwargs.get("content", "")
            if isinstance(body, str) and '"comments"' in body:
                comments = json.loads(body)["comments"]
                assert isinstance(comments, list)
                return comments
        raise AssertionError("no review body was written")

    @patch("hephaestus.automation.github_api.write_secure")
    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_out_of_hunk_comment_is_filtered_summary_still_posts(
        self, mock_gh_call: Any, _mock_repo: Any, mock_write: Any
    ) -> None:
        """A comment on a line outside the diff hunk must be dropped, not posted.

        Regression (#1039): unvalidated out-of-hunk comments made GitHub reject
        the whole review with HTTP 422, which the loop saw as a spurious NOGO. The
        in-hunk comment must survive and the review must still post.
        """
        mock_gh_call.side_effect = self._gh_call_side_effect(
            "REVIEW_1", [], diff_text=self._SAMPLE_DIFF
        )

        gh_pr_review_post(
            pr_number=7,
            comments=[
                {"path": "a.py", "line": 2, "side": "RIGHT", "body": "valid"},
                {"path": "a.py", "line": 999, "side": "RIGHT", "body": "out of hunk"},
                {"path": "missing.py", "line": 1, "side": "RIGHT", "body": "wrong file"},
            ],
            summary="Findings",
        )

        posted = self._posted_comments(mock_write)
        bodies = {c["body"] for c in posted}
        assert bodies == {"valid"}

    @patch("hephaestus.automation.github_api.write_secure")
    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_all_comments_out_of_hunk_posts_summary_only(
        self, mock_gh_call: Any, _mock_repo: Any, mock_write: Any
    ) -> None:
        """If every comment is out of hunk, the review still posts (summary only)."""
        mock_gh_call.side_effect = self._gh_call_side_effect(
            "REVIEW_1", [], diff_text=self._SAMPLE_DIFF
        )

        gh_pr_review_post(
            pr_number=7,
            comments=[{"path": "a.py", "line": 999, "side": "RIGHT", "body": "nope"}],
            summary="Findings",
        )

        posted = self._posted_comments(mock_write)
        assert posted == []
        # The review POST was still sent.
        self._review_post_call(mock_gh_call)

    @patch("hephaestus.automation.github_api.write_secure")
    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_empty_diff_fails_open_posts_all_comments(
        self, mock_gh_call: Any, _mock_repo: Any, mock_write: Any
    ) -> None:
        """When the diff cannot be fetched, validation fails open (posts unchanged).

        Dropping comments because the diff was unavailable would be worse than a
        possible 422 — so an empty/failed diff must leave comments untouched.
        """
        mock_gh_call.side_effect = self._gh_call_side_effect("REVIEW_1", [], diff_text="")

        gh_pr_review_post(
            pr_number=7,
            comments=[{"path": "a.py", "line": 42, "side": "RIGHT", "body": "keep me"}],
            summary="Findings",
        )

        posted = self._posted_comments(mock_write)
        assert {c["body"] for c in posted} == {"keep me"}

    # ------------------------------------------------------------------
    # #1083: a line that already has a (bot) review comment is EDITED, not
    # duplicated.
    # ------------------------------------------------------------------

    @patch("hephaestus.automation.github_api.gh_pr_update_review_comment")
    @patch("hephaestus.automation.github_api.gh_pr_inline_comment_index")
    @patch("hephaestus.automation.github_api.write_secure")
    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_existing_line_comment_is_edited_not_duplicated(
        self,
        mock_gh_call: Any,
        _mock_repo: Any,
        mock_write: Any,
        mock_index: Any,
        mock_update: Any,
    ) -> None:
        """A comment on a line that already has a bot comment edits it in place.

        #1085 C1: the edit must PRESERVE the original body (the GraphQL update
        mutation replaces the body, so the caller must concatenate
        existing + new), not clobber it with only the new suffix.
        """
        mock_gh_call.side_effect = self._gh_call_side_effect(
            "REVIEW_1", [], diff_text=self._SAMPLE_DIFF
        )
        # a.py:2 already has a bot comment (id + existing body); a.py:3 does not.
        mock_index.return_value = {
            ("a.py", 2): ("COMMENT_NODE_A2", "original note on line 2", True),
        }

        gh_pr_review_post(
            pr_number=7,
            comments=[
                {"path": "a.py", "line": 2, "side": "RIGHT", "body": "more on line 2"},
                {"path": "a.py", "line": 3, "side": "RIGHT", "body": "fresh on line 3"},
            ],
            summary="Findings",
            dedupe_existing=True,
        )

        # Line 2 was edited in place targeting the right comment node.
        mock_update.assert_called_once()
        assert mock_update.call_args.args[0] == "COMMENT_NODE_A2"
        edited_body = mock_update.call_args.args[1]
        # The edit must contain BOTH the original and the new text (no clobber).
        assert "original note on line 2" in edited_body
        assert "more on line 2" in edited_body
        # Only the genuinely new line-3 comment is posted as a fresh thread.
        posted = self._posted_comments(mock_write)
        assert {c["body"] for c in posted} == {"fresh on line 3"}

    @patch("hephaestus.automation.github_api.gh_pr_update_review_comment")
    @patch("hephaestus.automation.github_api.gh_pr_inline_comment_index")
    @patch("hephaestus.automation.github_api.write_secure")
    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_same_line_duplicate_comment_is_skipped_not_appended(
        self,
        mock_gh_call: Any,
        _mock_repo: Any,
        mock_write: Any,
        mock_index: Any,
        mock_update: Any,
    ) -> None:
        """A materially duplicate same-line review comment is not appended again."""
        mock_gh_call.side_effect = self._gh_call_side_effect(
            "REVIEW_1", [], diff_text=self._SAMPLE_DIFF
        )
        mock_index.return_value = {
            (
                "a.py",
                2,
            ): (
                "COMMENT_NODE_A2",
                "This regression coverage only exercises the Claude result-envelope path. "
                "The production diff also changed the Codex stdout path, so add a Codex test.",
                True,
            ),
        }

        gh_pr_review_post(
            pr_number=7,
            comments=[
                {
                    "path": "a.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": (
                        "This regression test only exercises the Claude JSON-envelope path. "
                        "The production fix also changed the Codex stdout path, so add a "
                        "Codex-path test."
                    ),
                }
            ],
            summary="Findings",
            dedupe_existing=True,
        )

        mock_update.assert_not_called()
        mock_write.assert_not_called()
        assert not any(
            "repos/owner/repo/pulls/7/reviews" in str(arg)
            for call in mock_gh_call.call_args_list
            for arg in (call.args[0] if call.args else call.kwargs.get("args", []))
        )

    @patch("hephaestus.automation.github_api.gh_pr_update_review_comment")
    @patch("hephaestus.automation.github_api.gh_pr_inline_comment_index")
    @patch("hephaestus.automation.github_api.write_secure")
    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_semantic_same_line_duplicate_comment_is_skipped_not_appended(
        self,
        mock_gh_call: Any,
        _mock_repo: Any,
        mock_write: Any,
        mock_index: Any,
        mock_update: Any,
    ) -> None:
        """Issue #1116: wording drift must not create another additional note."""
        mock_gh_call.side_effect = self._gh_call_side_effect(
            "REVIEW_1", [], diff_text=self._SAMPLE_DIFF
        )
        mock_index.return_value = {
            (
                "tests/unit/automation/test_pr_reviewer_posting.py",
                540,
            ): (
                "COMMENT_NODE_540",
                "This regression coverage only exercises the Claude result-envelope path. "
                "The production diff also changed the Codex stdout path, so add a Codex "
                "test where `summary` lacks a verdict and `review_text`/stdout contains "
                "`Verdict: GO` or `Verdict: NOGO`; otherwise the second producer path can "
                "regress without this suite catching it.",
                True,
            ),
        }

        gh_pr_review_post(
            pr_number=1116,
            comments=[
                {
                    "path": "tests/unit/automation/test_pr_reviewer_posting.py",
                    "line": 540,
                    "side": "RIGHT",
                    "body": (
                        "This regression test only exercises the Claude JSON-envelope path. "
                        "The production fix also changed the Codex stdout path, so please add "
                        "a Codex-path test that returns prose with `Verdict: GO`/`NOGO` and a "
                        "verdict-free JSON summary, then asserts `review_text` preserves the "
                        "raw stdout."
                    ),
                }
            ],
            summary="Findings",
            dedupe_existing=True,
        )

        mock_update.assert_not_called()
        assert self._posted_comments(mock_write) == []

    @patch("hephaestus.automation.github_api.gh_pr_update_review_comment")
    @patch("hephaestus.automation.github_api.gh_pr_inline_comment_index")
    @patch("hephaestus.automation.github_api.write_secure")
    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_finding_matching_only_a_resolved_thread_is_reposted(
        self,
        mock_gh_call: Any,
        _mock_repo: Any,
        mock_write: Any,
        mock_index: Any,
        mock_update: Any,
    ) -> None:
        """#1152 reverses #1116: a finding matching only a RESOLVED thread re-posts.

        A resolved thread is supposed to mean the finding was fixed and verified.
        If the reviewer re-raises it, the resolution was wrong (e.g. the old gate
        force-resolved it without addressing), so it must re-surface as a fresh
        thread rather than be suppressed — otherwise the GO gate sees zero
        unresolved threads and the PR converges with the issue unfixed.

        ``gh_pr_inline_comment_index`` (UNRESOLVED threads only) returns ``{}``
        here, so the line has no open thread and the finding posts fresh.
        """
        mock_gh_call.side_effect = self._gh_call_side_effect(
            "REVIEW_1", [], diff_text=self._SAMPLE_DIFF
        )
        # No UNRESOLVED thread on the line — the only prior comment was resolved.
        mock_index.return_value = {}

        gh_pr_review_post(
            pr_number=1116,
            comments=[
                {
                    "path": "a.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "Real finding the old gate force-resolved without fixing.",
                }
            ],
            summary="Findings",
            dedupe_existing=True,
        )

        # The finding was NOT edited into a (nonexistent) open thread...
        mock_update.assert_not_called()
        # ...it was posted fresh as a new review.
        assert any(
            "repos/owner/repo/pulls/1116/reviews" in str(arg)
            for call in mock_gh_call.call_args_list
            for arg in (call.args[0] if call.args else call.kwargs.get("args", []))
        )

    @patch("hephaestus.automation.github_api.gh_pr_update_review_comment")
    @patch("hephaestus.automation.github_api.gh_pr_inline_comment_index")
    @patch("hephaestus.automation.github_api.write_secure")
    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_actual_1116_codex_review_restatements_are_skipped(
        self,
        mock_gh_call: Any,
        _mock_repo: Any,
        mock_write: Any,
        mock_index: Any,
        mock_update: Any,
    ) -> None:
        """Issue #1116: observed Codex/Claude coverage restatements are duplicates."""
        mock_gh_call.side_effect = self._gh_call_side_effect(
            "REVIEW_1", [], diff_text=self._SAMPLE_DIFF
        )
        mock_index.return_value = {
            (
                "tests/unit/automation/test_pr_reviewer_posting.py",
                540,
            ): (
                "COMMENT_NODE_540",
                "This regression coverage only exercises the Claude result-envelope path. "
                "The production diff also changed the Codex stdout path, so add a Codex "
                "test where `summary` lacks a verdict and `review_text`/stdout contains "
                "`Verdict: GO` or `Verdict: NOGO`; otherwise the second producer path can "
                "regress without this suite catching it.",
                True,
            ),
        }

        duplicate_bodies = [
            (
                "This regression only exercises the Claude JSON-envelope path. Production "
                "also changed the Codex path to populate `review_text` from "
                "`run_codex_text().stdout`; add a sibling test that patches "
                "`is_codex`/`run_codex_text` and asserts a verdict-free `summary` still "
                "returns verdict-bearing `review_text` for `--agent=codex`."
            ),
            (
                "This regression test only exercises the Claude JSON-envelope path. The "
                "production fix also changed the Codex stdout path, so please add a "
                "Codex-path test that returns prose with `Verdict: GO`/`NOGO` and a "
                "verdict-free JSON summary, then asserts `review_text` preserves the raw "
                "stdout."
            ),
            (
                "This regression test only exercises the Claude JSON-envelope path. The "
                "production change also added `review_text` on the separate Codex stdout "
                "branch selected by `is_codex(agent)`. Add a Codex case that patches "
                "`is_codex=True` and `run_codex_text(stdout=...)`, then asserts `summary` "
                "remains the JSON field while `review_text` contains the verdict-bearing "
                "prose."
            ),
        ]

        for body in duplicate_bodies:
            gh_pr_review_post(
                pr_number=1116,
                comments=[
                    {
                        "path": "tests/unit/automation/test_pr_reviewer_posting.py",
                        "line": 540,
                        "side": "RIGHT",
                        "body": body,
                    }
                ],
                summary="Findings",
                dedupe_existing=True,
            )

        mock_update.assert_not_called()
        assert self._posted_comments(mock_write) == []

    @patch("hephaestus.automation.github_api.gh_pr_update_review_comment")
    @patch("hephaestus.automation.github_api.gh_pr_inline_comment_index")
    @patch("hephaestus.automation.github_api.write_secure")
    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_same_line_contract_restatement_is_skipped_not_appended(
        self,
        mock_gh_call: Any,
        _mock_repo: Any,
        mock_write: Any,
        mock_index: Any,
        mock_update: Any,
    ) -> None:
        """Issue #1116: repeated summary/review_text contract comments are duplicates."""
        mock_gh_call.side_effect = self._gh_call_side_effect(
            "REVIEW_1", [], diff_text=self._SAMPLE_DIFF
        )
        mock_index.return_value = {
            (
                "tests/unit/automation/test_pr_reviewer_posting.py",
                589,
            ): (
                "COMMENT_NODE_589",
                "This test proves `review_pr_inline()` returns `review_text`, but it does "
                "not assert the other half of the contract: GitHub still receives the JSON "
                "`summary` as the review body. Capture this mock and assert "
                '`gh_pr_review_post(..., summary="a defect (no verdict token here)")` so a '
                "future regression cannot post the full verdict prose.",
                True,
            ),
        }

        gh_pr_review_post(
            pr_number=1116,
            comments=[
                {
                    "path": "tests/unit/automation/test_pr_reviewer_posting.py",
                    "line": 589,
                    "side": "RIGHT",
                    "body": (
                        "This test verifies that `review_pr_inline()` returns the prose, but "
                        "it does not assert the other half of the contract: GitHub should "
                        "still receive the JSON `summary` as the review body. Capture the "
                        "`gh_pr_review_post` mock and assert "
                        '`summary == "a defect (no verdict token here)"` so a future change '
                        "cannot accidentally post `review_text` instead."
                    ),
                }
            ],
            summary="Findings",
            dedupe_existing=True,
        )

        mock_update.assert_not_called()
        assert self._posted_comments(mock_write) == []

    @patch("hephaestus.automation.github_api.gh_pr_update_review_comment")
    @patch("hephaestus.automation.github_api.gh_pr_inline_comment_index")
    @patch("hephaestus.automation.github_api.write_secure")
    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_dedupe_disabled_posts_everything(
        self,
        mock_gh_call: Any,
        _mock_repo: Any,
        mock_write: Any,
        mock_index: Any,
        mock_update: Any,
    ) -> None:
        """dedupe_existing=False keeps the legacy post-everything behavior."""
        mock_gh_call.side_effect = self._gh_call_side_effect(
            "REVIEW_1", [], diff_text=self._SAMPLE_DIFF
        )

        gh_pr_review_post(
            pr_number=7,
            comments=[{"path": "a.py", "line": 2, "side": "RIGHT", "body": "x"}],
            summary="Findings",
            dedupe_existing=False,
        )

        mock_index.assert_not_called()
        mock_update.assert_not_called()
        assert {c["body"] for c in self._posted_comments(mock_write)} == {"x"}

    @patch("hephaestus.automation.github_api.gh_pr_update_review_comment")
    @patch("hephaestus.automation.github_api.gh_pr_inline_comment_index")
    @patch("hephaestus.automation.github_api.write_secure")
    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_edit_in_place_falls_back_to_fresh_on_error(
        self,
        mock_gh_call: Any,
        _mock_repo: Any,
        mock_write: Any,
        mock_index: Any,
        mock_update: Any,
    ) -> None:
        """#1085: if the in-place edit raises, the comment is posted fresh instead."""
        mock_gh_call.side_effect = self._gh_call_side_effect(
            "REVIEW_1", [], diff_text=self._SAMPLE_DIFF
        )
        mock_index.return_value = {("a.py", 2): ("NODE", "old body", True)}
        mock_update.side_effect = OSError("network down")

        gh_pr_review_post(
            pr_number=7,
            comments=[{"path": "a.py", "line": 2, "side": "RIGHT", "body": "retry me"}],
            summary="Findings",
            dedupe_existing=True,
        )

        mock_update.assert_called_once()
        # Edit failed → the comment is posted as a fresh inline comment.
        assert {c["body"] for c in self._posted_comments(mock_write)} == {"retry me"}


class TestEditOrKeepUneditableComment:
    """#1327: a foreign (uneditable) first comment triggers an editable shadow post.

    When the first comment of an unresolved thread belongs to another app/account
    (Copilot, CodeQL), GitHub forbids editing it. ``_edit_or_keep_comments`` must
    NOT silently keep the foreign comment — it posts OUR OWN editable comment on
    the same line, indexes it, and edits THAT comment on every later re-raise.
    """

    @patch("hephaestus.automation.github_api.gh_pr_wont_fix_line_index", return_value=set())
    @patch("hephaestus.automation.github_api.gh_pr_update_review_comment")
    @patch("hephaestus.automation.github_api.gh_pr_review_post")
    @patch("hephaestus.automation.github_api.gh_pr_inline_comment_index")
    def test_uneditable_viewer_flag_posts_new_editable_shadow_comment(
        self,
        mock_index: Any,
        mock_post: Any,
        mock_update: Any,
        _mock_wont_fix: Any,
    ) -> None:
        from hephaestus.automation.github_api import _edit_or_keep_comments

        # First fetch: foreign comment (viewerCanUpdate False). Re-fetch after the
        # shadow post: our own editable comment now occupies the line.
        mock_index.side_effect = [
            {("a.py", 2, "RIGHT"): ("FOREIGN_NODE", "copilot finding", False)},
            {("a.py", 2, "RIGHT"): ("OUR_NODE", "our finding", True)},
        ]

        kept = _edit_or_keep_comments(
            7, [{"path": "a.py", "line": 2, "side": "RIGHT", "body": "our finding"}]
        )

        # A NEW editable comment was posted (not silently skipped)...
        mock_post.assert_called_once()
        posted_comments = mock_post.call_args.args[1]
        assert posted_comments == [
            {"path": "a.py", "line": 2, "side": "RIGHT", "body": "our finding"}
        ]
        # ...with dedupe disabled so it does not re-enter the dedupe loop.
        assert mock_post.call_args.kwargs["dedupe_existing"] is False
        # The foreign comment was left untouched (no edit mutation).
        mock_update.assert_not_called()
        # The finding was consumed (edited via shadow), not returned for re-post.
        assert kept == []

    @patch("hephaestus.automation.github_api.gh_pr_wont_fix_line_index", return_value=set())
    @patch("hephaestus.automation.github_api.gh_pr_update_review_comment")
    @patch("hephaestus.automation.github_api.gh_pr_review_post")
    @patch("hephaestus.automation.github_api.gh_pr_inline_comment_index")
    def test_subsequent_same_line_finding_edits_the_new_shadow_comment(
        self,
        mock_index: Any,
        mock_post: Any,
        mock_update: Any,
        _mock_wont_fix: Any,
    ) -> None:
        """After shadowing a foreign comment, a second same-line finding edits OUR node."""
        from hephaestus.automation.github_api import _edit_or_keep_comments

        mock_index.side_effect = [
            {("a.py", 2, "RIGHT"): ("FOREIGN_NODE", "copilot finding", False)},
            {("a.py", 2, "RIGHT"): ("OUR_NODE", "first our note", True)},
        ]

        _edit_or_keep_comments(
            7,
            [
                {"path": "a.py", "line": 2, "side": "RIGHT", "body": "first our note"},
                {"path": "a.py", "line": 2, "side": "RIGHT", "body": "a wholly different note"},
            ],
        )

        # The shadow was posted once for the first finding; the second finding was
        # an in-place edit of OUR newly-indexed editable comment, not the foreign.
        mock_post.assert_called_once()
        mock_update.assert_called_once()
        assert mock_update.call_args.args[0] == "OUR_NODE"
        edited_body = mock_update.call_args.args[1]
        assert "first our note" in edited_body
        assert "a wholly different note" in edited_body

    @patch("hephaestus.automation.github_api.gh_pr_wont_fix_line_index", return_value=set())
    @patch("hephaestus.automation.github_api.gh_pr_update_review_comment")
    @patch("hephaestus.automation.github_api.gh_pr_review_post")
    @patch("hephaestus.automation.github_api.gh_pr_inline_comment_index")
    def test_not_editable_mutation_error_falls_back_to_shadow_post(
        self,
        mock_index: Any,
        mock_post: Any,
        mock_update: Any,
        _mock_wont_fix: Any,
    ) -> None:
        """A "Body is not editable" mutation error (stale viewer flag) shadow-posts."""
        from hephaestus.automation.github_api import _edit_or_keep_comments

        # viewerCanUpdate optimistically True, but the edit mutation rejects it.
        mock_index.side_effect = [
            {("a.py", 2, "RIGHT"): ("FOREIGN_NODE", "copilot finding", True)},
            {("a.py", 2, "RIGHT"): ("OUR_NODE", "our finding", True)},
        ]
        mock_update.side_effect = subprocess.SubprocessError("gh: Body is not editable")

        kept = _edit_or_keep_comments(
            7, [{"path": "a.py", "line": 2, "side": "RIGHT", "body": "our finding"}]
        )

        # The edit was attempted (probe) then a shadow comment was posted.
        mock_update.assert_called_once()
        mock_post.assert_called_once()
        # The finding was consumed by the shadow comment, not re-posted fresh.
        assert kept == []

    @patch("hephaestus.automation.github_api.gh_pr_wont_fix_line_index", return_value=set())
    @patch("hephaestus.automation.github_api.gh_pr_update_review_comment")
    @patch("hephaestus.automation.github_api.gh_pr_review_post")
    @patch("hephaestus.automation.github_api.gh_pr_inline_comment_index")
    def test_uneditable_viewer_flag_emits_no_error_log(
        self,
        mock_index: Any,
        mock_post: Any,
        mock_update: Any,
        _mock_wont_fix: Any,
        caplog: Any,
    ) -> None:
        """#1368: foreign-comment (viewerCanUpdate=False) path emits no ERROR logs.

        When ``viewerCanUpdate`` is False the proactive path posts a shadow
        comment directly — the ``updatePullRequestReviewComment`` mutation is
        NEVER called, so no failure is generated.  This test confirms the update
        mock is not called AND that no ERROR-level records are emitted.
        """
        from hephaestus.automation.github_api import _edit_or_keep_comments

        mock_index.side_effect = [
            {("a.py", 2, "RIGHT"): ("FOREIGN_NODE", "copilot finding", False)},
            {("a.py", 2, "RIGHT"): ("OUR_NODE", "our finding", True)},
        ]

        with caplog.at_level("ERROR"):
            kept = _edit_or_keep_comments(
                7, [{"path": "a.py", "line": 2, "side": "RIGHT", "body": "our finding"}]
            )

        # Proactive path: update mutation must not be called for a foreign comment.
        mock_update.assert_not_called()
        # Recovery still happens: shadow comment is posted.
        mock_post.assert_called_once()
        # Finding is consumed, not returned for re-post.
        assert kept == []
        # No ERROR-level noise for an expected, fully-recovered condition.
        error_records = [r for r in caplog.records if r.levelno >= 40]
        assert error_records == [], (
            f"Expected no ERROR logs but got: {[r.getMessage() for r in error_records]}"
        )

    @patch("hephaestus.github.client.run_subprocess")
    def test_update_review_comment_does_not_log_error_on_not_editable(
        self,
        mock_run: Any,
        caplog: Any,
    ) -> None:
        """#1368: gh_pr_update_review_comment passes log_on_error=False to _gh_call.

        When GitHub rejects the update with "Body is not editable" the call
        raises (so the caller can shadow-post), but must NOT emit ERROR-level
        logs — the failure is expected and the caller recovers it.  Genuine
        unexpected failures elsewhere should still log at ERROR (checked via
        the existing test_token_scope_error_is_non_transient test).
        """
        from hephaestus.automation.github_api import gh_pr_update_review_comment

        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="gh: Body is not editable"
        )

        with caplog.at_level("ERROR"):
            with pytest.raises(subprocess.CalledProcessError):
                gh_pr_update_review_comment("NODE_ID", "some body")

        error_records = [r for r in caplog.records if r.levelno >= 40]
        assert error_records == [], (
            f"Expected no ERROR logs for expected 'not editable' failure but got: "
            f"{[r.getMessage() for r in error_records]}"
        )


class TestGhPrInlineCommentIndex:
    """gh_pr_inline_comment_index returns (path,line) → (node_id, body) (#1085)."""

    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_indexes_unresolved_threads_with_body(self, mock_gh_call: Any, _mock_repo: Any) -> None:
        result = Mock()
        result.stdout = json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "isResolved": False,
                                        "path": "a.py",
                                        "line": 2,
                                        "side": "RIGHT",
                                        "comments": {
                                            "nodes": [
                                                {
                                                    "id": "N1",
                                                    "body": "keep me",
                                                    "viewerCanUpdate": True,
                                                }
                                            ]
                                        },
                                    },
                                    {  # unresolved but foreign (Copilot) → not editable
                                        "isResolved": False,
                                        "path": "c.py",
                                        "line": 4,
                                        "side": "RIGHT",
                                        "comments": {
                                            "nodes": [
                                                {
                                                    "id": "N3",
                                                    "body": "copilot note",
                                                    "viewerCanUpdate": False,
                                                }
                                            ]
                                        },
                                    },
                                    {
                                        "isResolved": True,
                                        "path": "b.py",
                                        "line": 9,
                                        "comments": {"nodes": [{"id": "N2", "body": "resolved"}]},
                                    },
                                ]
                            }
                        }
                    }
                }
            }
        )
        mock_gh_call.return_value = result

        index = gh_pr_inline_comment_index(7)

        # Unresolved threads are indexed with id + body + viewerCanUpdate; the
        # resolved thread is excluded. The foreign (Copilot) comment carries
        # editable=False so the caller posts its own shadow comment (#1327).
        assert index == {
            ("a.py", 2): ("N1", "keep me", True),
            ("a.py", 2, "RIGHT"): ("N1", "keep me", True),
            ("c.py", 4): ("N3", "copilot note", False),
            ("c.py", 4, "RIGHT"): ("N3", "copilot note", False),
        }

    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_fails_open_on_bad_json(self, mock_gh_call: Any, _mock_repo: Any) -> None:
        result = Mock()
        result.stdout = "not json{"
        mock_gh_call.return_value = result
        assert gh_pr_inline_comment_index(7) == {}


class TestWontFixLineIndex:
    """gh_pr_wont_fix_line_index / dedup suppression of intentional-design findings (#1163)."""

    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    @patch("hephaestus.automation.github_api._gh_call")
    def test_indexes_only_resolved_marker_threads(self, mock_gh_call: Any, _mock_repo: Any) -> None:
        from hephaestus.automation.github_api import gh_pr_wont_fix_line_index
        from hephaestus.automation.protocol import WONT_FIX_MARKER

        result = Mock()
        result.stdout = json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {  # resolved + marker → indexed
                                        "isResolved": True,
                                        "path": "a.py",
                                        "line": 2,
                                        "side": "RIGHT",
                                        "comments": {
                                            "nodes": [
                                                {"id": "N1", "body": "orig"},
                                                {"id": "N2", "body": f"{WONT_FIX_MARKER} — stub"},
                                            ]
                                        },
                                    },
                                    {  # resolved but NO marker → not indexed
                                        "isResolved": True,
                                        "path": "b.py",
                                        "line": 5,
                                        "side": "RIGHT",
                                        "comments": {"nodes": [{"id": "N3", "body": "fixed"}]},
                                    },
                                    {  # marker but UNRESOLVED → not indexed
                                        "isResolved": False,
                                        "path": "c.py",
                                        "line": 9,
                                        "side": "RIGHT",
                                        "comments": {
                                            "nodes": [{"id": "N4", "body": WONT_FIX_MARKER}]
                                        },
                                    },
                                ]
                            }
                        }
                    }
                }
            }
        )
        mock_gh_call.return_value = result

        keys = gh_pr_wont_fix_line_index(7)

        assert ("a.py", 2, "RIGHT") in keys
        assert ("a.py", 2) in keys
        assert ("b.py", 5, "RIGHT") not in keys
        assert ("c.py", 9, "RIGHT") not in keys

    @patch("hephaestus.automation.github_api.gh_pr_wont_fix_line_index")
    @patch("hephaestus.automation.github_api.gh_pr_inline_comment_index", return_value={})
    def test_dedup_suppresses_finding_on_wont_fix_line(
        self, _mock_index: Any, mock_wont_fix: Any
    ) -> None:
        from hephaestus.automation.github_api import _edit_or_keep_comments

        mock_wont_fix.return_value = {("a.py", 2, "RIGHT"), ("a.py", 2)}
        comments = [
            {"path": "a.py", "line": 2, "side": "RIGHT", "body": "re-raised intentional finding"},
            {"path": "z.py", "line": 9, "side": "RIGHT", "body": "genuinely new finding"},
        ]

        kept = _edit_or_keep_comments(123, comments)

        # The won't-fix line is dropped; the unrelated finding survives.
        assert [c["path"] for c in kept] == ["z.py"]


class TestValidReviewPositions:
    """_valid_review_positions / _filter_comments_to_diff: diff-hunk parsing (#1039)."""

    _DIFF = (
        "diff --git a/mod.py b/mod.py\n"
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -10,3 +10,4 @@ def f():\n"
        " context_a\n"
        "-removed_old\n"
        "+added_new\n"
        "+added_new2\n"
        " context_b\n"
    )

    def test_right_side_includes_added_and_context_lines(self) -> None:
        from hephaestus.automation.github_api import _valid_review_positions

        positions = _valid_review_positions(self._DIFF)
        right = {line for (line, side) in positions["mod.py"] if side == "RIGHT"}
        # New-file numbering starts at 10: context_a=10, added_new=11,
        # added_new2=12, context_b=13.
        assert right == {10, 11, 12, 13}

    def test_left_side_includes_removed_and_context_lines(self) -> None:
        from hephaestus.automation.github_api import _valid_review_positions

        positions = _valid_review_positions(self._DIFF)
        left = {line for (line, side) in positions["mod.py"] if side == "LEFT"}
        # Old-file numbering starts at 10: context_a=10, removed_old=11,
        # context_b=12.
        assert left == {10, 11, 12}

    def test_filter_drops_unknown_path_and_line(self) -> None:
        from hephaestus.automation.github_api import _filter_comments_to_diff

        comments = [
            {"path": "mod.py", "line": 11, "side": "RIGHT", "body": "ok"},
            {"path": "mod.py", "line": 500, "side": "RIGHT", "body": "bad line"},
            {"path": "other.py", "line": 11, "side": "RIGHT", "body": "bad path"},
        ]
        kept = _filter_comments_to_diff(comments, self._DIFF)
        assert [c["body"] for c in kept] == ["ok"]

    def test_filter_defaults_side_to_right(self) -> None:
        from hephaestus.automation.github_api import _filter_comments_to_diff

        # No explicit side → treated as RIGHT (the gh_pr_review_post default).
        comments = [{"path": "mod.py", "line": 11, "body": "ok"}]
        kept = _filter_comments_to_diff(comments, self._DIFF)
        assert len(kept) == 1

    def test_empty_diff_returns_comments_unchanged(self) -> None:
        from hephaestus.automation.github_api import _filter_comments_to_diff

        comments = [{"path": "mod.py", "line": 11, "side": "RIGHT", "body": "ok"}]
        assert _filter_comments_to_diff(comments, "") == comments


class TestGhPrResolveThread:
    """gh_pr_resolve_thread: reply to and resolve a PR review thread."""

    @staticmethod
    def _graphql_queries(mock_gh_call: Any) -> list[str]:
        """Return the ``query=`` payload of every ``gh api graphql`` invocation."""
        queries: list[str] = []
        for call in mock_gh_call.call_args_list:
            sent_args = call.args[0] if call.args else call.kwargs.get("args", [])
            for arg in sent_args:
                if isinstance(arg, str) and arg.startswith("query="):
                    queries.append(arg)
        return queries

    @patch("hephaestus.automation.github_api._gh_call")
    def test_reply_uses_thread_reply_mutation_not_deprecated_comment(
        self, mock_gh_call: Any
    ) -> None:
        """The reply step must use ``addPullRequestReviewThreadReply``.

        Regression (#999): the reply mutation used the deprecated
        ``addPullRequestReviewComment(input: {pullRequestReviewThreadId: ...})``,
        whose input type has no ``pullRequestReviewThreadId`` field. GitHub
        rejected it on every call (``InputObject 'AddPullRequestReviewCommentInput'
        doesn't accept argument 'pullRequestReviewThreadId'``), so threads were
        never replied-to or resolved.
        """
        mock_gh_call.return_value = Mock(stdout="{}")

        gh_pr_resolve_thread("PRRT_abc123", "Addressed in code.")

        queries = self._graphql_queries(mock_gh_call)
        joined = "\n".join(queries)
        # The correct, non-deprecated mutation must be used for the reply.
        assert "addPullRequestReviewThreadReply" in joined
        # The deprecated mutation must never be sent.
        assert "addPullRequestReviewComment" not in joined

    @patch("hephaestus.automation.github_api._gh_call")
    def test_reply_then_resolve_pass_thread_id(self, mock_gh_call: Any) -> None:
        """Both the reply and resolve steps run, passing the thread id through."""
        mock_gh_call.return_value = Mock(stdout="{}")

        gh_pr_resolve_thread("PRRT_xyz", "Fixed.")

        queries = self._graphql_queries(mock_gh_call)
        joined = "\n".join(queries)
        assert "addPullRequestReviewThreadReply" in joined
        assert "resolveReviewThread" in joined
        # threadId is forwarded as a -f field on both calls.
        thread_id_fields = [
            call
            for call in mock_gh_call.call_args_list
            if any(
                isinstance(a, str) and a == "threadId=PRRT_xyz"
                for a in (call.args[0] if call.args else call.kwargs.get("args", []))
            )
        ]
        assert len(thread_id_fields) == 2

    @patch("hephaestus.automation.github_api._gh_call")
    def test_resolve_without_reply_does_not_add_thread_comment(self, mock_gh_call: Any) -> None:
        """Stale-thread cleanup can resolve without adding duplicate review noise."""
        mock_gh_call.return_value = Mock(stdout="{}")

        gh_pr_resolve_thread("PRRT_quiet")

        queries = self._graphql_queries(mock_gh_call)
        joined = "\n".join(queries)
        assert "resolveReviewThread" in joined
        assert "addPullRequestReviewThreadReply" not in joined
        assert mock_gh_call.call_count == 1

    @patch("hephaestus.automation.github_api._gh_call")
    def test_dry_run_sends_nothing(self, mock_gh_call: Any) -> None:
        gh_pr_resolve_thread("PRRT_abc", "reply", dry_run=True)
        mock_gh_call.assert_not_called()
