"""Tests for resilience module composing retry and circuit breaker."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from hephaestus.resilience.circuit_breaker import reset_all_circuit_breakers
from hephaestus.resilience.subprocess_resilience import (
    TRANSIENT_ERROR_PATTERNS,
    is_transient_subprocess_error,
    resilient_call,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    """Reset circuit breaker registry before each test."""
    reset_all_circuit_breakers()


class TestIsTransientSubprocessError:
    """Tests for is_transient_subprocess_error function."""

    def test_timeout_expired_is_not_transient(self) -> None:
        """TimeoutExpired is intentional, not transient."""
        error = subprocess.TimeoutExpired(cmd="test", timeout=30)
        assert is_transient_subprocess_error(error) is False

    def test_connection_error_is_transient(self) -> None:
        """ConnectionError is always transient."""
        error = ConnectionError("connection refused")
        assert is_transient_subprocess_error(error) is True

    def test_timeout_error_is_transient(self) -> None:
        """TimeoutError (Python builtin) is transient."""
        error = TimeoutError("operation timed out")
        assert is_transient_subprocess_error(error) is True

    def test_os_error_with_transient_pattern(self) -> None:
        """OSError with transient pattern is transient."""
        error = OSError("connection reset by peer")
        assert is_transient_subprocess_error(error) is True

    def test_os_error_without_transient_pattern(self) -> None:
        """OSError without transient pattern is not transient."""
        error = OSError("permission denied")
        assert is_transient_subprocess_error(error) is False

    def test_subprocess_error_with_transient_pattern(self) -> None:
        """SubprocessError with network-related message is transient."""
        error = subprocess.SubprocessError("early eof from server")
        assert is_transient_subprocess_error(error) is True

    def test_subprocess_error_without_transient_pattern(self) -> None:
        """SubprocessError without transient pattern is not transient."""
        error = subprocess.SubprocessError("invalid argument")
        assert is_transient_subprocess_error(error) is False

    def test_value_error_is_not_transient(self) -> None:
        """Non-subprocess errors are not transient."""
        error = ValueError("invalid value")
        assert is_transient_subprocess_error(error) is False

    def test_called_process_error_with_network_error(self) -> None:
        """CalledProcessError with network stderr is transient."""
        error = subprocess.CalledProcessError(
            returncode=1,
            cmd="git fetch",
            stderr="connection reset by peer",
        )
        assert is_transient_subprocess_error(error) is True


class TestTransientErrorPatterns:
    """Tests for transient error pattern list."""

    def test_patterns_are_lowercase(self) -> None:
        """All patterns should be lowercase for case-insensitive matching."""
        for pattern in TRANSIENT_ERROR_PATTERNS:
            assert pattern == pattern.lower(), f"Pattern not lowercase: {pattern}"

    def test_essential_patterns_present(self) -> None:
        """Essential transient patterns are in the list."""
        essential = [
            "connection reset",
            "connection refused",
            "timed out",
            "early eof",
            "503",
            "502",
            "504",
        ]
        for pattern in essential:
            assert pattern in TRANSIENT_ERROR_PATTERNS, f"Missing pattern: {pattern}"


class TestResilientCall:
    """Tests for resilient_call() — especially max_delay enforcement."""

    def test_succeeds_on_first_call(self) -> None:
        """resilient_call returns the function result on first success."""
        result = resilient_call(lambda: 42)
        assert result == 42

    @patch("time.sleep")
    def test_max_delay_is_honored(self, mock_sleep) -> None:
        """No individual retry sleep substantially exceeds max_delay.

        Sets max_delay=0.1 with a high initial_delay so that without capping
        the delays would far exceed 0.1 s (initial_delay=10.0 → first raw
        delay = 10.0 s).  Asserts every time.sleep() call stays within
        max_delay + 25% jitter headroom, which is the documented contract of
        retry_with_backoff: the cap is applied *before* jitter so the actual
        sleep value can reach max_delay * 1.25 at most.
        """
        call_count = 0

        def transient_flaky() -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("connection refused")

        max_delay = 0.1
        resilient_call(transient_flaky, max_retries=3, initial_delay=10.0, max_delay=max_delay)

        # Without max_delay the first sleep would be ~10 s; confirm it is
        # effectively capped near max_delay (allowing ±25% jitter on top).
        upper_bound = max_delay * 1.25 + 0.001  # 1 ms floating-point headroom
        for call in mock_sleep.call_args_list:
            assert call[0][0] <= upper_bound, (
                f"Sleep of {call[0][0]:.4f}s exceeds max_delay cap of {upper_bound:.4f}s; "
                "max_delay is not being honored."
            )

    @patch("time.sleep")
    def test_raises_after_max_retries(self, mock_sleep) -> None:
        """Raises the last exception when all retries are exhausted."""
        with pytest.raises(ConnectionError, match="always fails"):
            resilient_call(
                lambda: (_ for _ in ()).throw(ConnectionError("always fails")),
                max_retries=2,
                initial_delay=0.01,
                max_delay=0.1,
            )


class TestRetryPredicateWiring:
    """Tests that resilient_call actually invokes is_transient_subprocess_error.

    Before this wire-up, resilient_call only matched on the
    TRANSIENT_SUBPROCESS_ERRORS exception tuple, so non-transient OSErrors
    (e.g. permission denied) and intentional TimeoutExpired errors burned
    three pointless retries. The predicate gates retries on stderr-pattern
    content as well as exception type.
    """

    @patch("time.sleep")
    def test_non_transient_oserror_does_not_retry(self, mock_sleep) -> None:
        """OSError without a transient pattern is raised after a single call."""
        call_count = 0

        def permission_denied() -> None:
            nonlocal call_count
            call_count += 1
            raise OSError(13, "Permission denied")

        with pytest.raises(OSError, match="Permission denied"):
            resilient_call(
                permission_denied,
                max_retries=3,
                initial_delay=0.01,
                max_delay=0.1,
            )

        # Predicate rejects → propagate immediately, no retries, no sleeps.
        assert call_count == 1
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    def test_transient_stderr_pattern_retries(self, mock_sleep) -> None:
        """CalledProcessError with transient stderr is retried until success."""
        call_count = 0

        def flaky_subprocess() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise subprocess.CalledProcessError(
                    returncode=1,
                    cmd="git fetch",
                    stderr="fatal: Connection reset by peer",
                )
            return "ok"

        result = resilient_call(
            flaky_subprocess,
            max_retries=3,
            initial_delay=0.01,
            max_delay=0.1,
        )

        assert result == "ok"
        assert call_count == 3
        # Two retries before success → two sleeps.
        assert mock_sleep.call_count == 2

    @patch("time.sleep")
    def test_timeout_expired_does_not_retry(self, mock_sleep) -> None:
        """subprocess.TimeoutExpired is intentional and bypasses retry."""
        call_count = 0

        def times_out() -> None:
            nonlocal call_count
            call_count += 1
            raise subprocess.TimeoutExpired(cmd="long-job", timeout=30)

        with pytest.raises(subprocess.TimeoutExpired):
            resilient_call(
                times_out,
                max_retries=3,
                initial_delay=0.01,
                max_delay=0.1,
            )

        # Predicate returns False for TimeoutExpired → no retries, no sleeps.
        assert call_count == 1
        mock_sleep.assert_not_called()
