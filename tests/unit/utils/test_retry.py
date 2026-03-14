#!/usr/bin/env python3
"""Tests for retry utilities."""

from unittest.mock import MagicMock, patch

import pytest

from hephaestus.utils.retry import (
    is_network_error,
    retry_on_network_error,
    retry_with_backoff,
    retry_with_jitter,
)


class TestIsNetworkError:
    """Tests for is_network_error."""

    def test_connection_error_keyword(self):
        """Detects 'connection' keyword."""
        assert is_network_error(Exception("connection refused")) is True

    def test_timeout_keyword(self):
        """Detects 'timeout' keyword."""
        assert is_network_error(Exception("request timed out")) is True

    def test_rate_limit_keyword(self):
        """Detects 'rate limit' keyword."""
        assert is_network_error(Exception("rate limit exceeded")) is True

    def test_503_keyword(self):
        """Detects HTTP 503 status code in message."""
        assert is_network_error(Exception("HTTP 503 Service Unavailable")) is True

    def test_non_network_error(self):
        """Non-network errors return False."""
        assert is_network_error(ValueError("invalid input")) is False
        assert is_network_error(TypeError("wrong type")) is False

    def test_empty_error(self):
        """Empty error message returns False."""
        assert is_network_error(Exception("")) is False

    def test_name_resolution(self):
        """Detects 'name resolution' keyword."""
        assert is_network_error(Exception("name resolution failed")) is True


class TestRetryWithBackoff:
    """Tests for retry_with_backoff decorator."""

    def test_succeeds_on_first_try(self):
        """Function that succeeds immediately is called once."""
        mock_fn = MagicMock(return_value=42)
        decorated = retry_with_backoff(max_retries=3, initial_delay=0)(mock_fn)
        result = decorated()
        assert result == 42
        mock_fn.assert_called_once()

    @patch("time.sleep")
    def test_retries_on_exception(self, mock_sleep):
        """Function retries on failure before succeeding."""
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("fail")
            return "success"

        decorated = retry_with_backoff(max_retries=3, initial_delay=0.1, jitter=False)(flaky)
        result = decorated()
        assert result == "success"
        assert call_count == 3

    @patch("time.sleep")
    def test_raises_after_max_retries(self, mock_sleep):
        """Raises last exception after max retries exhausted."""
        mock_fn = MagicMock(side_effect=RuntimeError("always fails"))
        decorated = retry_with_backoff(max_retries=2, initial_delay=0.01, jitter=False)(mock_fn)

        with pytest.raises(RuntimeError, match="always fails"):
            decorated()

        assert mock_fn.call_count == 3  # initial + 2 retries

    @patch("time.sleep")
    def test_retry_on_specific_exception(self, mock_sleep):
        """Only retries on specified exception types."""
        mock_fn = MagicMock(side_effect=TypeError("type error"))
        decorated = retry_with_backoff(
            max_retries=3,
            initial_delay=0.01,
            retry_on=(ValueError,),  # NOT TypeError
            jitter=False,
        )(mock_fn)

        with pytest.raises(TypeError):
            decorated()

        # Should not retry on TypeError when retry_on=(ValueError,)
        mock_fn.assert_called_once()

    @patch("time.sleep")
    def test_logger_called_on_retry(self, mock_sleep):
        """Logger is called with retry info on each retry."""
        mock_logger = MagicMock()
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ValueError("fail")
            return "ok"

        decorated = retry_with_backoff(
            max_retries=3, initial_delay=0.01, jitter=False, logger=mock_logger
        )(flaky)
        decorated()
        mock_logger.assert_called_once()

    @patch("time.sleep")
    def test_jitter_does_not_break_retry(self, mock_sleep):
        """Retry with jitter=True still succeeds."""
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ValueError("fail")
            return "ok"

        decorated = retry_with_backoff(max_retries=3, initial_delay=0.1, jitter=True)(flaky)
        result = decorated()
        assert result == "ok"

    def test_preserves_function_name(self):
        """Decorated function preserves original name via functools.wraps."""

        def my_function():
            return 1

        decorated = retry_with_backoff()(my_function)
        assert decorated.__name__ == "my_function"


class TestRetryOnNetworkError:
    """Tests for retry_on_network_error convenience decorator."""

    @patch("time.sleep")
    def test_retries_on_connection_error(self, mock_sleep):
        """Retries when ConnectionError is raised."""
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("connection refused")
            return "ok"

        decorated = retry_on_network_error(max_retries=3, initial_delay=0.01)(flaky)
        result = decorated()
        assert result == "ok"
        assert call_count == 2

    @patch("time.sleep")
    def test_does_not_retry_non_network_errors(self, mock_sleep):
        """Does not retry on non-network errors (e.g., ValueError)."""
        mock_fn = MagicMock(side_effect=ValueError("bad value"))
        decorated = retry_on_network_error(max_retries=3, initial_delay=0.01)(mock_fn)

        with pytest.raises(ValueError):
            decorated()

        mock_fn.assert_called_once()


class TestRetryWithJitter:
    """Tests for retry_with_jitter function."""

    @patch("time.sleep")
    def test_succeeds_on_first_call(self, mock_sleep):
        """Succeeds without retrying when first call works."""
        result = retry_with_jitter(lambda: 99, max_retries=3, base_delay=0.01)
        assert result == 99
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    def test_retries_and_succeeds(self, mock_sleep):
        """Retries and eventually succeeds."""
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("fail")
            return "done"

        result = retry_with_jitter(flaky, max_retries=3, base_delay=0.01)
        assert result == "done"
        assert call_count == 3

    @patch("time.sleep")
    def test_raises_after_exhaustion(self, mock_sleep):
        """Raises exception when all retries fail."""
        with pytest.raises(RuntimeError, match="always fails"):
            retry_with_jitter(
                lambda: (_ for _ in ()).throw(RuntimeError("always fails")),
                max_retries=2,
                base_delay=0.01,
            )

    @patch("time.sleep")
    def test_max_delay_respected(self, mock_sleep):
        """Sleep is never called with more than max_delay + jitter."""
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 4:
                raise RuntimeError("fail")
            return "ok"

        retry_with_jitter(flaky, max_retries=4, base_delay=1.0, max_delay=2.0)
        # All sleep calls should be <= max_delay * 1.25 (jitter upper bound)
        for c in mock_sleep.call_args_list:
            assert c[0][0] <= 2.0 * 1.25 + 0.1
