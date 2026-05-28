"""Tests for GitHub API utilities."""

import json
import subprocess
from typing import Any
from unittest.mock import Mock, patch

import pytest

import hephaestus.automation.github_api as _github_api_module
from hephaestus.automation.github_api import (
    GitHubRateLimitError,
    _check_graphql_errors,
    _gh_call,
    fetch_issue_info,
    gh_create_label,
    gh_issue_comment,
    gh_issue_create,
    gh_issue_json,
    gh_list_labels,
    gh_list_open_issues,
    gh_pr_create,
    is_issue_closed,
    parse_issue_dependencies,
    prefetch_issue_states,
    write_secure,
)
from hephaestus.automation.models import IssueState


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

    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_repo_info_failure(self, mock_repo_info: Any) -> None:
        """Test when repo info fails."""
        mock_repo_info.side_effect = RuntimeError("Not in repo")

        states = prefetch_issue_states([123])

        assert states == {}

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_int_validation_in_graphql_query(self, mock_repo_info: Any, mock_gh_call: Any) -> None:
        """Test that issue numbers are cast to int in GraphQL query to prevent injection."""
        mock_repo_info.return_value = ("owner", "repo")

        mock_result = Mock()
        mock_result.stdout = json.dumps(
            {
                "data": {
                    "repository": {
                        "issue0": {"number": 123, "state": "OPEN"},
                    }
                }
            }
        )
        mock_gh_call.return_value = mock_result

        # Call with issue numbers
        states = prefetch_issue_states([123])

        # Verify the GraphQL query was called with int-casted numbers
        mock_gh_call.assert_called()
        call_args = mock_gh_call.call_args
        # The query should contain the int-casted number
        query_arg = call_args[0][0]
        assert any("issue(number: 123)" in arg for arg in query_arg)

        assert states[123] == IssueState.OPEN

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_non_numeric_issue_number_raises_error(self, mock_repo_info: Any, mock_gh_call: Any) -> None:
        """Test that non-numeric issue numbers raise ValueError during int() casting."""
        mock_repo_info.return_value = ("owner", "repo")

        # Try to pass a non-numeric string (type-hint violation)
        # This should raise a ValueError when int() is called on it
        with pytest.raises(ValueError):
            # Using "abc" as string will fail the int() cast
            prefetch_issue_states(["abc"])  # type: ignore


class TestGhCall:
    """Tests for _gh_call function."""

    def setup_method(self) -> None:
        """Reset circuit breaker before each test."""
        from hephaestus.resilience import reset_all_circuit_breakers

        reset_all_circuit_breakers()

    @patch("hephaestus.automation.github_api.run")
    def test_successful_call(self, mock_run: Any) -> None:
        """Test successful gh call."""
        mock_result = Mock()
        mock_result.stdout = "success"
        mock_run.return_value = mock_result

        result = _gh_call(["issue", "view", "123"])

        assert result.stdout == "success"
        mock_run.assert_called_once()

    @patch("hephaestus.automation.github_api.run")
    @patch("hephaestus.automation.github_api.wait_until")
    @patch("hephaestus.automation.github_api.detect_rate_limit")
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

    @patch("hephaestus.automation.github_api.run")
    def test_fail_fast_on_permission_error(self, mock_run: Any) -> None:
        """Test that permission errors fail fast without retry."""
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="403 Forbidden: permission denied"
        )

        with pytest.raises(subprocess.CalledProcessError):
            _gh_call(["issue", "view", "123"])

        # Should only call once, no retries
        assert mock_run.call_count == 1

    @patch("hephaestus.automation.github_api.run")
    def test_fail_fast_on_not_found(self, mock_run: Any) -> None:
        """Test that 404 errors fail fast without retry."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="404 Not Found")

        with pytest.raises(subprocess.CalledProcessError):
            _gh_call(["issue", "view", "123"])

        assert mock_run.call_count == 1

    @patch("hephaestus.automation.github_api.run")
    def test_fail_fast_on_bad_request(self, mock_run: Any) -> None:
        """Test that 400 errors fail fast without retry."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="400 Bad Request")

        with pytest.raises(subprocess.CalledProcessError):
            _gh_call(["issue", "view", "123"])

        assert mock_run.call_count == 1

    @patch("hephaestus.automation.github_api.run")
    @patch("hephaestus.automation.github_api.time.sleep")
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

    @patch("hephaestus.automation.github_api.run")
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

    @patch("hephaestus.automation.github_api.run")
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

        with patch("hephaestus.automation.github_api.time.sleep"):
            result = _gh_call(["issue", "view", "123"], max_retries=2)

        assert result.stdout == "success"

    @patch("hephaestus.automation.github_api.run")
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

    @patch("hephaestus.automation.github_api.run")
    def test_token_scope_error_for_integration_also_non_transient(self, mock_run: Any) -> None:
        """GitHub-App variant of the scope error is recognised too."""
        stderr = "GraphQL: Resource not accessible by integration (addComment)"
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr=stderr)

        with pytest.raises(subprocess.CalledProcessError):
            _gh_call(["issue", "comment", "1", "--body-file", "/tmp/x"])

        assert mock_run.call_count == 1

    @patch("hephaestus.automation.github_api.resilient_call")
    def test_resilient_call_invoked_with_correct_parameters(self, mock_resilient: Any) -> None:
        """Test that _gh_call invokes resilient_call with correct parameters.

        Verifies that the circuit breaker name, max_retries, and other
        parameters are passed correctly to resilient_call.
        """
        mock_result = Mock(spec=subprocess.CompletedProcess)
        mock_result.stdout = "success"
        mock_resilient.return_value = mock_result

        result = _gh_call(["issue", "view", "123"], max_retries=6)

        assert result.stdout == "success"
        # resilient_call should be called on outer loop
        assert mock_resilient.call_count == 1
        # Check that circuit_breaker_name was passed
        call_kwargs = mock_resilient.call_args[1]
        assert "circuit_breaker_name" in call_kwargs
        assert call_kwargs["circuit_breaker_name"] == "gh_cli"
        assert call_kwargs["max_retries"] == 2  # inner retries
        assert call_kwargs["initial_delay"] == 2.0

    @patch("hephaestus.automation.github_api.run")
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

    @patch("hephaestus.automation.github_api.run")
    @patch("hephaestus.automation.github_api.time.sleep")
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

    @patch("hephaestus.automation.github_api.run")
    @patch("hephaestus.automation.github_api.wait_until")
    @patch("hephaestus.automation.github_api.detect_rate_limit")
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

    @patch("hephaestus.automation.github_api.run")
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

    @patch("hephaestus.automation.github_api.run")
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

    @patch("hephaestus.automation.github_api.run")
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

    @patch("hephaestus.automation.github_api.run")
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


# NOTE on patch targets: tests in TestGhCall patch "hephaestus.automation.github_api.run"
# because _gh_call (defined in github_api) calls run() imported from .git_utils.
# All other tests patch "hephaestus.automation.github_api._gh_call" to intercept at
# a higher level, bypassing the gh CLI entirely.
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
        _github_api_module._label_cache = None

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

    @patch("hephaestus.automation.github_api._gh_call")
    def test_create_label_updates_cache(self, mock_gh_call: Any) -> None:
        """gh_create_label adds the new label to the cache if it exists."""
        _github_api_module._label_cache = {"bug"}
        mock_gh_call.return_value = Mock()

        gh_create_label("testing")

        assert "testing" in _github_api_module._label_cache


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


class TestGhPrCreate:
    """Tests for gh_pr_create function."""

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_successful_pr_creation(self, mock_gh_call: Any, _mock_signed: Any) -> None:
        """Test successful PR creation."""
        mock_create_result = Mock()
        mock_create_result.stdout = "https://github.com/owner/repo/pull/456"
        mock_merge_result = Mock()
        mock_gh_call.side_effect = [mock_create_result, mock_merge_result]

        pr_number = gh_pr_create(
            branch="feature-branch",
            title="Test PR",
            body=_POLICY_BODY,
            auto_merge=True,
        )

        assert pr_number == 456
        assert mock_gh_call.call_count == 2  # create + auto-merge

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_pr_creation_without_auto_merge(self, mock_gh_call: Any, _mock_signed: Any) -> None:
        """Test PR creation without auto-merge."""
        mock_result = Mock()
        mock_result.stdout = "https://github.com/owner/repo/pull/789"
        mock_gh_call.return_value = mock_result

        pr_number = gh_pr_create(
            branch="feature-branch",
            title="Test PR",
            body=_POLICY_BODY,
            auto_merge=False,
        )

        assert pr_number == 789
        assert mock_gh_call.call_count == 1  # Only create, no auto-merge

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_pr_creation_with_fallback_parsing(self, mock_gh_call: Any, _mock_signed: Any) -> None:
        """Test PR number extraction fallback."""
        mock_result = Mock()
        # URL without /pull/ pattern
        mock_result.stdout = "https://github.com/owner/repo/123"
        mock_gh_call.return_value = mock_result

        pr_number = gh_pr_create(
            branch="feature-branch",
            title="Test PR",
            body=_POLICY_BODY,
            auto_merge=False,
        )

        assert pr_number == 123

    @patch("hephaestus.automation.github_api._assert_branch_commits_signed")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_pr_creation_auto_merge_failure_raises(
        self, mock_gh_call: Any, _mock_signed: Any
    ) -> None:
        """Auto-merge is mandatory; a failure must raise, not warn."""
        mock_create_result = Mock()
        mock_create_result.stdout = "https://github.com/owner/repo/pull/456"

        mock_gh_call.side_effect = [
            mock_create_result,
            subprocess.CalledProcessError(1, "gh"),
        ]

        with pytest.raises(RuntimeError, match="Auto-merge could not be enabled"):
            gh_pr_create(
                branch="feature-branch",
                title="Test PR",
                body=_POLICY_BODY,
                auto_merge=True,
            )

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

    @patch("hephaestus.automation.github_api.run")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_rejects_unsigned_commit(self, mock_gh_call: Any, mock_run: Any) -> None:
        """An 'N' (no signature) commit must abort PR creation."""
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
        mock_gh_call.assert_not_called()

    @patch("hephaestus.automation.github_api.run")
    @patch("hephaestus.automation.github_api._gh_call")
    def test_rejects_bad_signature(self, mock_gh_call: Any, mock_run: Any) -> None:
        """A 'B' (bad signature) commit must abort PR creation."""
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

        mock_create_result = Mock(stdout="https://github.com/owner/repo/pull/42")
        mock_merge_result = Mock()
        mock_gh_call.side_effect = [mock_create_result, mock_merge_result]

        pr_number = gh_pr_create(
            branch="feature-branch",
            title="Test PR",
            body=_POLICY_BODY,
            auto_merge=True,
        )
        assert pr_number == 42


class TestWriteSecure:
    """Tests for write_secure function."""

    def test_write_new_file(self, tmp_path: Any) -> None:
        """Test writing to a new file."""
        test_file = tmp_path / "test.txt"
        content = "test content"

        write_secure(test_file, content)

        assert test_file.exists()
        assert test_file.read_text() == content

    def test_overwrite_existing_file(self, tmp_path: Any) -> None:
        """Test overwriting an existing file."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("old content")

        write_secure(test_file, "new content")

        assert test_file.read_text() == "new content"

    def test_create_parent_directories(self, tmp_path: Any) -> None:
        """Test that parent directories are created."""
        test_file = tmp_path / "subdir" / "nested" / "test.txt"

        write_secure(test_file, "content")

        assert test_file.exists()
        assert test_file.read_text() == "content"

    def test_atomic_write(self, tmp_path: Any) -> None:
        """Test that write is atomic (uses temp file + rename)."""
        test_file = tmp_path / "test.txt"

        # Write initial content
        test_file.write_text("original")

        # Verify temp file pattern during write
        write_secure(test_file, "updated")

        # Should have no temp files left
        temp_files = list(tmp_path.glob(".test.txt.*.tmp"))
        assert len(temp_files) == 0

        # Content should be updated
        assert test_file.read_text() == "updated"

    def test_cleanup_on_error(self, tmp_path: Any) -> None:
        """Test that temp files are cleaned up on error."""
        test_file = tmp_path / "test.txt"

        # Make parent directory read-only to cause error
        test_file.parent.chmod(0o444)

        try:
            with pytest.raises(OSError):
                write_secure(test_file, "content")

            # Temp files should be cleaned up
            temp_files = list(tmp_path.glob(".test.txt.*.tmp"))
            assert len(temp_files) == 0
        finally:
            # Restore permissions for cleanup
            test_file.parent.chmod(0o755)

    def test_writes_with_restrictive_permissions(self, tmp_path: Any) -> None:
        """github_api.write_secure delegates to io.write_secure → 0o600 perms.

        Regression for #443: the automation copy of write_secure used to write
        with default permissions; consolidating onto hephaestus.io.write_secure
        means state files are owner-only.
        """
        test_file = tmp_path / "state.json"
        write_secure(test_file, "{}")
        assert oct(test_file.stat().st_mode & 0o777) == oct(0o600)


class TestGhCallThrottle:
    """Tests for the per-thread `gh` call throttle inside _gh_call."""

    @pytest.fixture(autouse=True)
    def _reset_throttle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Reset the per-thread throttle state and force a known rate so tests
        # don't inherit clock state from earlier suite calls.
        _github_api_module._GH_THROTTLE = __import__("threading").local()
        monkeypatch.setenv("GH_RATE_LIMIT_PER_SEC", "5")

    @patch("hephaestus.automation.github_api.run")
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

    @patch("hephaestus.automation.github_api.run")
    def test_throttle_disabled_when_rate_zero(
        self, mock_run: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GH_RATE_LIMIT_PER_SEC=0 disables pacing entirely."""
        monkeypatch.setenv("GH_RATE_LIMIT_PER_SEC", "0")
        mock_run.return_value = Mock(stdout="", stderr="", returncode=0)

        import time as _time

        t0 = _time.monotonic()
        for _ in range(5):
            _gh_call(["api", "/rate_limit"])
        elapsed = _time.monotonic() - t0

        # 5 calls with no throttle should finish well under one min-interval.
        assert elapsed < 0.05, f"unexpected delay with throttle off; elapsed={elapsed:.3f}s"

    @patch("hephaestus.automation.github_api.run")
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
    @patch("hephaestus.automation.github_api.gh_global_throttle_acquire")
    @patch("hephaestus.automation.github_api.run")
    @patch("hephaestus.automation.github_api.wait_until")
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
    @patch("hephaestus.automation.github_api.gh_global_throttle_acquire")
    @patch("hephaestus.automation.github_api.run")
    @patch("hephaestus.automation.github_api.wait_until")
    @patch("hephaestus.automation.github_api.time.sleep")
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
