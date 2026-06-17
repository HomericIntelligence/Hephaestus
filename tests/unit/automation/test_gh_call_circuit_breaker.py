#!/usr/bin/env python3
"""Tests for CircuitBreaker wrapping of _gh_call."""

import subprocess
from collections.abc import Generator
from unittest.mock import Mock, patch

import pytest

import hephaestus.automation.github_api as github_api_module
import hephaestus.github.client as client_module


@pytest.fixture(autouse=True)
def _reset_breaker() -> Generator[None, None, None]:
    """Reset the GitHub API circuit breaker before each test."""
    client_module._GH_BREAKER.reset()
    yield
    client_module._GH_BREAKER.reset()


class TestGhCallCircuitBreaker:
    """Test CircuitBreaker wrapping of _gh_call."""

    @patch("hephaestus.github.client._gh_call_impl")
    def test_breaker_transitions_to_open_on_failures(self, mock_impl: Mock) -> None:
        """Circuit breaker transitions from CLOSED to OPEN after 5 consecutive failures."""
        # Simulate 5xx failures
        mock_impl.side_effect = subprocess.CalledProcessError(
            500, "gh", stderr="Internal Server Error"
        )

        # First 5 calls should fail with CalledProcessError (not caught by breaker)
        for _i in range(5):
            with pytest.raises(subprocess.CalledProcessError):
                github_api_module._gh_call(["issue", "list"])

        # Verify the mock was called 5 times
        assert mock_impl.call_count == 5

        # 6th call should fail with GitHubUnavailableError (circuit now OPEN)
        with pytest.raises(github_api_module.GitHubUnavailableError):
            github_api_module._gh_call(["issue", "list"])

        # Circuit is open: mock should NOT be called again (fail-fast)
        assert mock_impl.call_count == 5

    @patch("hephaestus.github.client._gh_call_impl")
    def test_circuit_breaker_open_error_is_runtime_error(self, mock_impl: Mock) -> None:
        """GitHubUnavailableError is a RuntimeError subclass (for existing handlers)."""
        mock_impl.side_effect = subprocess.CalledProcessError(
            500, "gh", stderr="Internal Server Error"
        )

        # Trigger 5 failures to open the breaker
        for _ in range(5):
            with pytest.raises(subprocess.CalledProcessError):
                github_api_module._gh_call(["issue", "list"])

        # The error raised when breaker is open should be a RuntimeError
        with pytest.raises(RuntimeError):
            github_api_module._gh_call(["issue", "list"])

        # And specifically a GitHubUnavailableError
        with pytest.raises(github_api_module.GitHubUnavailableError):
            github_api_module._gh_call(["issue", "list"])

    @patch("hephaestus.github.client._gh_call_impl")
    def test_circuit_breaker_recovery_after_timeout(self, mock_impl: Mock) -> None:
        """Circuit breaker transitions to HALF_OPEN after recovery_timeout seconds.

        Note: This test uses time mocking to avoid a 60-second sleep.
        """
        import time

        # Simulate failure
        mock_impl.side_effect = subprocess.CalledProcessError(
            500, "gh", stderr="Internal Server Error"
        )

        # Trigger 5 failures to open the breaker
        for _ in range(5):
            with pytest.raises(subprocess.CalledProcessError):
                github_api_module._gh_call(["issue", "list"])

        # Verify breaker is open
        with pytest.raises(github_api_module.GitHubUnavailableError):
            github_api_module._gh_call(["issue", "list"])

        # Now simulate recovery by mocking the breaker's internal time check.
        # Mock time.monotonic to simulate timeout passage
        original_monotonic = time.monotonic
        elapsed_time = 0.0

        def mock_monotonic() -> float:
            return original_monotonic() + elapsed_time

        with patch("time.monotonic", side_effect=mock_monotonic):
            # Advance time past recovery_timeout (60 seconds)
            elapsed_time = 61.0

            # Change mock to succeed
            mock_impl.side_effect = None
            mock_impl.return_value = subprocess.CompletedProcess(
                ["gh", "issue", "list"], returncode=0, stdout="[]", stderr=""
            )

            # Call should succeed (breaker in HALF_OPEN allows 2 calls)
            result = github_api_module._gh_call(["issue", "list"])
            assert result.returncode == 0

            # Another call should also succeed and fully close the breaker
            result = github_api_module._gh_call(["issue", "list"])
            assert result.returncode == 0

    @patch("hephaestus.github.client._gh_call_impl")
    def test_breaker_does_not_wrap_successful_calls(self, mock_impl: Mock) -> None:
        """Circuit breaker does not interfere with successful _gh_call invocations."""
        expected_result = subprocess.CompletedProcess(
            ["gh", "issue", "list"], returncode=0, stdout="[]", stderr=""
        )
        mock_impl.return_value = expected_result

        # Multiple successful calls should work fine
        for _ in range(10):
            result = github_api_module._gh_call(["issue", "list"])
            assert result == expected_result

        # Mock should be called each time (breaker closed)
        assert mock_impl.call_count == 10

    @patch("hephaestus.github.client._gh_call_impl")
    def test_circuit_breaker_preserves_call_signature(self, mock_impl: Mock) -> None:
        """Circuit breaker correctly forwards all _gh_call_impl arguments."""
        expected_result = subprocess.CompletedProcess(
            ["gh", "issue", "list"], returncode=0, stdout="[]", stderr=""
        )
        mock_impl.return_value = expected_result

        # Call with custom arguments
        result = github_api_module._gh_call(
            ["issue", "list"],
            check=False,
            retry_on_rate_limit=False,
            max_retries=3,
        )

        assert result == expected_result

        # Verify the mock was called with correct arguments — log_on_error is now
        # forwarded by the circuit-breaker passthrough (added in #1368), so it
        # must appear in the expected call (default value True).
        mock_impl.assert_called_once_with(
            ["issue", "list"],
            check=False,
            retry_on_rate_limit=False,
            max_retries=3,
            log_on_error=True,
            timeout=None,
        )
