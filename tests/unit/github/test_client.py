#!/usr/bin/env python3
"""Tests for hephaestus.github.client.gh_call public contract."""

import subprocess
from collections.abc import Generator
from unittest.mock import Mock, patch

import pytest

from hephaestus.github.client import (
    _GH_BREAKER,
    ClaudeUsageCapError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    gh_call,
    gh_cli_timeout,
)


@pytest.fixture(autouse=True)
def _reset_breaker() -> Generator[None, None, None]:
    """Reset the GitHub API circuit breaker before each test."""
    _GH_BREAKER.reset()
    yield
    _GH_BREAKER.reset()


class TestGhCallCircuitBreaker:
    """Test CircuitBreaker wrapping of gh_call."""

    @patch("hephaestus.github.client._gh_call_impl")
    def test_breaker_transitions_to_open_on_failures(self, mock_impl: Mock) -> None:
        """Circuit breaker transitions from CLOSED to OPEN after 5 consecutive failures."""
        # Simulate 5xx failures
        mock_impl.side_effect = subprocess.CalledProcessError(
            500, "gh", stderr="Internal Server Error"
        )

        # First 5 calls should fail with CalledProcessError
        for _i in range(5):
            with pytest.raises(subprocess.CalledProcessError):
                gh_call(["issue", "list"])

        # Verify the mock was called 5 times
        assert mock_impl.call_count == 5

        # 6th call should fail with GitHubUnavailableError (circuit now OPEN)
        with pytest.raises(GitHubUnavailableError):
            gh_call(["issue", "list"])

        # Circuit is open: mock should NOT be called again (fail-fast)
        assert mock_impl.call_count == 5

    @patch("hephaestus.github.client._gh_call_impl")
    def test_circuit_breaker_open_error_is_runtime_error(self, mock_impl: Mock) -> None:
        """GitHubUnavailableError is a RuntimeError subclass."""
        mock_impl.side_effect = subprocess.CalledProcessError(
            500, "gh", stderr="Internal Server Error"
        )

        # Trigger 5 failures to open the breaker
        for _ in range(5):
            with pytest.raises(subprocess.CalledProcessError):
                gh_call(["issue", "list"])

        # The error raised when breaker is open should be a RuntimeError
        with pytest.raises(RuntimeError):
            gh_call(["issue", "list"])

        # And specifically a GitHubUnavailableError
        with pytest.raises(GitHubUnavailableError):
            gh_call(["issue", "list"])


class TestGhCallRateLimit:
    """Test rate limit handling in gh_call."""

    @patch("hephaestus.github.client._gh_call_impl")
    def test_propagates_rate_limit_error(self, mock_impl: Mock) -> None:
        """gh_call propagates GitHubRateLimitError with reset_epoch."""
        mock_impl.side_effect = GitHubRateLimitError("rate limited", reset_epoch=1234)
        with pytest.raises(GitHubRateLimitError) as exc_info:
            gh_call(["api", "/repos/owner/repo"])
        assert exc_info.value.reset_epoch == 1234


class TestGhCallClaudeCap:
    """Test Claude usage cap handling in gh_call."""

    @patch("hephaestus.github.client._gh_call_impl")
    def test_propagates_claude_cap(self, mock_impl: Mock) -> None:
        """gh_call propagates ClaudeUsageCapError with reset_epoch."""
        mock_impl.side_effect = ClaudeUsageCapError("cap exceeded", reset_epoch=5678)
        with pytest.raises(ClaudeUsageCapError) as exc_info:
            gh_call(["api", "/x"])
        assert exc_info.value.reset_epoch == 5678


class TestGhCliTimeout:
    """Test gh_cli_timeout configuration."""

    def test_gh_cli_timeout_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gh_cli_timeout returns 120 by default."""
        monkeypatch.delenv("HEPH_GH_TIMEOUT", raising=False)
        assert gh_cli_timeout() == 120

    def test_gh_cli_timeout_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gh_cli_timeout respects HEPH_GH_TIMEOUT environment variable."""
        monkeypatch.setenv("HEPH_GH_TIMEOUT", "60")
        assert gh_cli_timeout() == 60

    def test_gh_cli_timeout_invalid_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gh_cli_timeout falls back to 120 on non-integer HEPH_GH_TIMEOUT."""
        monkeypatch.setenv("HEPH_GH_TIMEOUT", "not_a_number")
        assert gh_cli_timeout() == 120


class TestGhCallPublicExports:
    """Test that gh_call is properly exported from hephaestus.github."""

    def test_gh_call_exported_from_package(self) -> None:
        """gh_call is exported from hephaestus.github.__init__."""
        import hephaestus.github as github_pkg

        assert hasattr(github_pkg, "gh_call")
        assert github_pkg.gh_call is gh_call

    def test_error_classes_exported_from_package(self) -> None:
        """Error classes are exported from hephaestus.github.__init__."""
        import hephaestus.github as github_pkg

        assert hasattr(github_pkg, "GitHubRateLimitError")
        assert hasattr(github_pkg, "GitHubUnavailableError")
        assert hasattr(github_pkg, "ClaudeUsageCapError")
        assert github_pkg.GitHubRateLimitError is GitHubRateLimitError
        assert github_pkg.GitHubUnavailableError is GitHubUnavailableError
        assert github_pkg.ClaudeUsageCapError is ClaudeUsageCapError


class TestNonTransientErrorClassification:
    """_is_non_transient_error: deterministic gh failures must not be retried."""

    def test_body_not_editable_is_non_transient(self) -> None:
        """#1327: editing a foreign-owned comment never succeeds on retry.

        Without this classification the deterministic "Body is not editable"
        rejection was retried ~6× per finding (66× in one observed run) before
        the caller could fall back to posting its own editable comment.
        """
        from hephaestus.github.client import _is_non_transient_error

        assert _is_non_transient_error("gh: Body is not editable") is True

    def test_transient_5xx_is_not_non_transient(self) -> None:
        """A 500 is retryable, so it must NOT be flagged non-transient."""
        from hephaestus.github.client import _is_non_transient_error

        assert _is_non_transient_error("Internal Server Error (HTTP 500)") is False

    def test_graphql_syntax_error_is_non_transient(self) -> None:
        """#1350: a malformed GraphQL query is a parse error, never retryable.

        A stray ``repr()`` once emitted single-quoted string literals
        (``owner:'H...'``), which gh rejected with
        ``Expected VALUE, actual: UNKNOWN_CHAR``. Such syntax errors can never
        succeed on retry, so they must fail fast instead of being retried ~6×.
        """
        from hephaestus.github.client import _is_non_transient_error

        assert (
            _is_non_transient_error(
                'gh: Expected VALUE, actual: UNKNOWN_CHAR ("H") at [1, 24]',
            )
            is True
        )
