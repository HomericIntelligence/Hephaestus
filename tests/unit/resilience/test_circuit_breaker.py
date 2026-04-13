"""Tests for circuit breaker pattern implementation."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitBreakerState,
    get_circuit_breaker,
    reset_all_circuit_breakers,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    """Reset circuit breaker registry before each test."""
    reset_all_circuit_breakers()


class TestCircuitBreakerStates:
    """Tests for circuit breaker state transitions."""

    def test_initial_state_is_closed(self) -> None:
        """Circuit breaker starts in CLOSED state."""
        cb = CircuitBreaker("test")
        assert cb.state == CircuitBreakerState.CLOSED

    def test_stays_closed_on_success(self) -> None:
        """Successful calls keep circuit in CLOSED state."""
        cb = CircuitBreaker("test", failure_threshold=3)
        result = cb.call(lambda: "ok")
        assert result == "ok"
        assert cb.state == CircuitBreakerState.CLOSED

    def test_opens_after_failure_threshold(self) -> None:
        """Circuit opens after reaching failure threshold."""
        cb = CircuitBreaker("test", failure_threshold=3)
        failing_func = MagicMock(side_effect=RuntimeError("fail"))

        for _ in range(3):
            with pytest.raises(RuntimeError):
                cb.call(failing_func)

        assert cb.state == CircuitBreakerState.OPEN

    def test_stays_closed_below_threshold(self) -> None:
        """Circuit stays closed when failures are below threshold."""
        cb = CircuitBreaker("test", failure_threshold=3)
        failing_func = MagicMock(side_effect=RuntimeError("fail"))

        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(failing_func)

        assert cb.state == CircuitBreakerState.CLOSED

    def test_success_resets_failure_count(self) -> None:
        """Successful call resets the failure counter."""
        cb = CircuitBreaker("test", failure_threshold=3)
        failing_func = MagicMock(side_effect=RuntimeError("fail"))

        # Two failures
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(failing_func)

        # One success resets counter
        cb.call(lambda: "ok")

        # Two more failures should not open (counter reset)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(failing_func)

        assert cb.state == CircuitBreakerState.CLOSED

    def test_open_to_half_open_after_recovery_timeout(self) -> None:
        """Circuit transitions from OPEN to HALF_OPEN after recovery timeout."""
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.1)
        failing_func = MagicMock(side_effect=RuntimeError("fail"))

        with pytest.raises(RuntimeError):
            cb.call(failing_func)

        assert cb.state == CircuitBreakerState.OPEN

        # Wait for recovery timeout
        time.sleep(0.15)

        assert cb.state == CircuitBreakerState.HALF_OPEN

    def test_half_open_to_closed_on_success(self) -> None:
        """Successful call in HALF_OPEN transitions to CLOSED."""
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.1)

        # Open the circuit
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        assert cb.state == CircuitBreakerState.OPEN

        # Wait for recovery
        time.sleep(0.15)

        # Successful call should close
        result = cb.call(lambda: "recovered")
        assert result == "recovered"
        assert cb.state == CircuitBreakerState.CLOSED

    def test_half_open_to_open_on_failure(self) -> None:
        """Failed call in HALF_OPEN transitions back to OPEN."""
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.1)

        # Open the circuit
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        # Wait for recovery
        time.sleep(0.15)

        # Fail again in half-open
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("still failing")))

        assert cb.state == CircuitBreakerState.OPEN


class TestCircuitBreakerOpenError:
    """Tests for open circuit behavior."""

    def test_raises_circuit_breaker_open(self) -> None:
        """Open circuit raises CircuitBreakerOpenError."""
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=60.0)

        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        with pytest.raises(CircuitBreakerOpenError, match="Circuit breaker 'test' is open"):
            cb.call(lambda: "should not run")

    def test_circuit_breaker_open_has_recovery_time(self) -> None:
        """CircuitBreakerOpenError exception includes time until recovery."""
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=60.0)

        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            cb.call(lambda: "should not run")

        assert exc_info.value.time_until_recovery > 0
        assert exc_info.value.name == "test"

    def test_half_open_max_calls_exceeded(self) -> None:
        """Exceeding half_open_max_calls raises CircuitBreakerOpenError."""
        cb = CircuitBreaker(
            "test",
            failure_threshold=1,
            recovery_timeout=0.1,
            half_open_max_calls=1,
        )

        # Open the circuit
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        # Wait for recovery
        time.sleep(0.15)

        # First half-open call fails, re-opening circuit
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("half-open fail")))

        # Now it's OPEN again, so next call should be rejected
        with pytest.raises(CircuitBreakerOpenError):
            cb.call(lambda: "should not run")


class TestCircuitBreakerReset:
    """Tests for circuit breaker reset."""

    def test_reset_to_closed(self) -> None:
        """Reset returns circuit to CLOSED state."""
        cb = CircuitBreaker("test", failure_threshold=1)

        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        assert cb.state == CircuitBreakerState.OPEN

        cb.reset()
        assert cb.state == CircuitBreakerState.CLOSED

    def test_reset_clears_failure_count(self) -> None:
        """Reset clears the failure counter."""
        cb = CircuitBreaker("test", failure_threshold=3)

        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(MagicMock(side_effect=RuntimeError("fail")))

        cb.reset()

        # After reset, need full threshold failures to open again
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(MagicMock(side_effect=RuntimeError("fail")))

        assert cb.state == CircuitBreakerState.CLOSED


class TestCircuitBreakerRegistry:
    """Tests for the global circuit breaker registry."""

    def test_get_creates_new(self) -> None:
        """get_circuit_breaker creates a new instance for unknown names."""
        cb = get_circuit_breaker("new_service")
        assert cb.name == "new_service"
        assert cb.state == CircuitBreakerState.CLOSED

    def test_get_returns_singleton(self) -> None:
        """get_circuit_breaker returns same instance for same name."""
        cb1 = get_circuit_breaker("service_a")
        cb2 = get_circuit_breaker("service_a")
        assert cb1 is cb2

    def test_get_different_names_different_instances(self) -> None:
        """Different names return different instances."""
        cb1 = get_circuit_breaker("service_a")
        cb2 = get_circuit_breaker("service_b")
        assert cb1 is not cb2

    def test_reset_all_clears_registry(self) -> None:
        """reset_all_circuit_breakers clears the registry."""
        cb1 = get_circuit_breaker("service_a")
        get_circuit_breaker("service_b")

        reset_all_circuit_breakers()

        # Getting same name creates new instance
        cb3 = get_circuit_breaker("service_a")
        assert cb3 is not cb1


class TestCircuitBreakerThreadSafety:
    """Tests for thread safety of circuit breaker."""

    def test_concurrent_calls_are_thread_safe(self) -> None:
        """Circuit breaker handles concurrent calls without corruption."""
        cb = CircuitBreaker("thread_test", failure_threshold=10)
        call_count = 0
        lock = threading.Lock()

        def counting_func() -> str:
            nonlocal call_count
            with lock:
                call_count += 1
            return "ok"

        threads = []
        for _ in range(20):
            t = threading.Thread(target=lambda: cb.call(counting_func))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        assert call_count == 20
        assert cb.state == CircuitBreakerState.CLOSED

    def test_concurrent_failures_open_circuit(self) -> None:
        """Concurrent failures correctly trigger circuit opening."""
        cb = CircuitBreaker("thread_test", failure_threshold=5)
        errors: list[Exception] = []

        def failing_func() -> None:
            raise RuntimeError("concurrent failure")

        def attempt() -> None:
            try:
                cb.call(failing_func)
            except (RuntimeError, CircuitBreakerOpenError) as e:
                errors.append(e)

        threads = [threading.Thread(target=attempt) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert cb.state == CircuitBreakerState.OPEN
        # Some should be RuntimeError, some CircuitBreakerOpenError
        assert len(errors) == 10


class TestCircuitBreakerSuccessThreshold:
    """Tests for success_threshold — N consecutive successes required to close from HALF_OPEN."""

    def test_success_threshold_default_one_closes_immediately(self) -> None:
        """Default success_threshold=1: single success in HALF_OPEN closes the circuit."""
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.1)

        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        time.sleep(0.15)
        assert cb.state == CircuitBreakerState.HALF_OPEN

        cb.call(lambda: "ok")
        assert cb.state == CircuitBreakerState.CLOSED

    def test_success_threshold_two_requires_two_successes(self) -> None:
        """success_threshold=2: first success keeps circuit HALF_OPEN, second closes it."""
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.1, success_threshold=2)

        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        time.sleep(0.15)
        assert cb.state == CircuitBreakerState.HALF_OPEN

        cb.call(lambda: 1)
        assert cb.state == CircuitBreakerState.HALF_OPEN  # still half-open

        cb.call(lambda: 2)
        assert cb.state == CircuitBreakerState.CLOSED

    def test_failure_in_half_open_resets_success_counter(self) -> None:
        """A failure in HALF_OPEN re-opens the circuit and resets the success counter."""
        cb = CircuitBreaker(
            "test",
            failure_threshold=1,
            recovery_timeout=0.1,
            half_open_max_calls=3,
            success_threshold=2,
        )

        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        time.sleep(0.15)

        cb.call(lambda: "one success")  # half-open success counter = 1

        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail again")))

        assert cb.state == CircuitBreakerState.OPEN

    def test_success_threshold_reset_clears_counter(self) -> None:
        """reset() clears the half-open success counter."""
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.1, success_threshold=2)

        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        time.sleep(0.15)
        cb.call(lambda: "one")  # counter = 1, still HALF_OPEN
        assert cb.state == CircuitBreakerState.HALF_OPEN

        cb.reset()
        assert cb.state == CircuitBreakerState.CLOSED

        # After reset, need to go through full cycle again
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))
        assert cb.state == CircuitBreakerState.OPEN


class TestCircuitBreakerTimeMocking:
    """Tests for OPEN→HALF_OPEN transition timing using mocked monotonic clock."""

    @patch("hephaestus.resilience.circuit_breaker.time.monotonic")
    def test_open_to_half_open_exactly_at_timeout(self, mock_monotonic: MagicMock) -> None:
        """Circuit transitions to HALF_OPEN at exactly recovery_timeout elapsed."""
        mock_monotonic.return_value = 100.0

        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=30.0)

        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        # Not yet — 20s elapsed
        mock_monotonic.return_value = 120.0
        assert cb.state == CircuitBreakerState.OPEN

        # Exactly at timeout — 30s elapsed
        mock_monotonic.return_value = 130.0
        assert cb.state == CircuitBreakerState.HALF_OPEN

    @patch("hephaestus.resilience.circuit_breaker.time.monotonic")
    def test_well_past_timeout_still_half_open(self, mock_monotonic: MagicMock) -> None:
        """Circuit stays HALF_OPEN (not CLOSED) even long after timeout, until a call succeeds."""
        mock_monotonic.return_value = 0.0

        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=10.0)

        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        mock_monotonic.return_value = 100.0
        assert cb.state == CircuitBreakerState.HALF_OPEN


@pytest.mark.parametrize(
    ("failure_threshold", "recovery_timeout", "success_threshold"),
    [
        (1, 0.1, 1),
        (3, 0.1, 1),
        (5, 0.1, 2),
        (10, 0.1, 3),
    ],
)
def test_parametrized_thresholds_full_cycle(
    failure_threshold: int, recovery_timeout: float, success_threshold: int
) -> None:
    """Various threshold configurations follow the full CLOSED→OPEN→HALF_OPEN→CLOSED cycle."""
    reset_all_circuit_breakers()
    cb = CircuitBreaker(
        "param_test",
        failure_threshold=failure_threshold,
        recovery_timeout=recovery_timeout,
        half_open_max_calls=success_threshold,  # allow enough probes
        success_threshold=success_threshold,
    )

    for _ in range(failure_threshold):
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

    assert cb.state == CircuitBreakerState.OPEN

    time.sleep(recovery_timeout + 0.05)
    assert cb.state == CircuitBreakerState.HALF_OPEN

    for _ in range(success_threshold):
        cb.call(lambda: "ok")

    assert cb.state == CircuitBreakerState.CLOSED
