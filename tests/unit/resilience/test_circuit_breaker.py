"""Tests for circuit breaker pattern implementation."""

from __future__ import annotations

import contextlib
import threading
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitBreakerOpenReason,
    CircuitBreakerState,
    get_circuit_breaker,
    reset_all_circuit_breakers,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    """Reset circuit breaker registry before each test."""
    reset_all_circuit_breakers()


class TestCircuitBreakerIgnoredExceptions:
    """``ignore`` excludes caller-classified exceptions from the failure count (#2048).

    A circuit breaker exists to detect an unavailable SERVICE. Some exceptions
    prove the opposite — the service answered, and answered correctly, about a
    specific target (e.g. HTTP 404). Counting those toward the failure threshold
    lets a handful of deterministic, per-target errors open the breaker and fail
    every unrelated call behind it.
    """

    def test_ignored_exception_does_not_count_toward_threshold(self) -> None:
        """N > threshold ignored failures never open the breaker."""
        breaker = CircuitBreaker(
            "ignore-probe", failure_threshold=2, ignore=lambda exc: isinstance(exc, KeyError)
        )

        def boom() -> None:
            raise KeyError("404-ish: the service answered, the target is absent")

        for _ in range(5):
            with pytest.raises(KeyError):
                breaker.call(boom)

        assert breaker.state is CircuitBreakerState.CLOSED

    def test_ignored_exception_still_propagates(self) -> None:
        """Ignoring is about bookkeeping only — the caller still sees the error."""
        breaker = CircuitBreaker("ignore-raise", ignore=lambda exc: isinstance(exc, KeyError))

        with pytest.raises(KeyError, match="still raised"):
            breaker.call(lambda: (_ for _ in ()).throw(KeyError("still raised")))

    def test_non_ignored_exception_still_opens(self) -> None:
        """Real failures are unaffected by the exclusion predicate."""
        breaker = CircuitBreaker(
            "ignore-mixed", failure_threshold=2, ignore=lambda exc: isinstance(exc, KeyError)
        )

        def down() -> None:
            raise ConnectionError("service unreachable")

        for _ in range(2):
            with pytest.raises(ConnectionError):
                breaker.call(down)

        assert breaker.state is CircuitBreakerState.OPEN

    def test_ignored_exception_does_not_reset_failure_count(self) -> None:
        """An ignored error is neither a success nor a failure.

        Recording a *success* instead would zero the accumulated failure count
        and erase a genuine outage signal that was building.
        """
        breaker = CircuitBreaker(
            "ignore-nosuccess", failure_threshold=2, ignore=lambda exc: isinstance(exc, KeyError)
        )

        with pytest.raises(ConnectionError):
            breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("down")))
        with pytest.raises(KeyError):  # must NOT clear the failure above
            breaker.call(lambda: (_ for _ in ()).throw(KeyError("404")))
        with pytest.raises(ConnectionError):
            breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("down")))

        assert breaker.state is CircuitBreakerState.OPEN

    def test_ignored_exception_does_not_reopen_half_open_breaker(self) -> None:
        """In HALF_OPEN a single failure re-opens; an ignored error must not."""
        breaker = CircuitBreaker(
            "ignore-halfopen",
            failure_threshold=1,
            recovery_timeout=0.0,  # immediately eligible for HALF_OPEN
            ignore=lambda exc: isinstance(exc, KeyError),
        )
        with pytest.raises(ConnectionError):
            breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("down")))
        assert breaker.state is CircuitBreakerState.HALF_OPEN  # recovery_timeout=0

        with pytest.raises(KeyError):
            breaker.call(lambda: (_ for _ in ()).throw(KeyError("404")))

        assert breaker.state is CircuitBreakerState.HALF_OPEN  # not re-OPENed

    def test_default_ignores_nothing(self) -> None:
        """Back-compat: no ``ignore`` predicate → every exception counts."""
        breaker = CircuitBreaker("ignore-default", failure_threshold=2)

        for _ in range(2):
            with pytest.raises(KeyError):
                breaker.call(lambda: (_ for _ in ()).throw(KeyError("x")))

        assert breaker.state is CircuitBreakerState.OPEN


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

    @patch("hephaestus.resilience.circuit_breaker.time.monotonic")
    def test_open_to_half_open_after_recovery_timeout(self, mock_monotonic: MagicMock) -> None:
        """Circuit transitions from OPEN to HALF_OPEN after recovery timeout."""
        mock_monotonic.return_value = 1000.0  # baseline stamped by _record_failure
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=30.0)
        failing_func = MagicMock(side_effect=RuntimeError("fail"))

        with pytest.raises(RuntimeError):
            cb.call(failing_func)  # stamps _last_failure_time = 1000.0

        assert cb.state == CircuitBreakerState.OPEN

        # Advance past recovery timeout
        mock_monotonic.return_value = 1030.0

        assert cb.state == CircuitBreakerState.HALF_OPEN

    @patch("hephaestus.resilience.circuit_breaker.time.monotonic")
    def test_half_open_to_closed_on_success(self, mock_monotonic: MagicMock) -> None:
        """Successful call in HALF_OPEN transitions to CLOSED."""
        mock_monotonic.return_value = 1000.0
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=30.0)

        # Open the circuit
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        assert cb.state == CircuitBreakerState.OPEN

        # Advance past recovery timeout
        mock_monotonic.return_value = 1030.0

        # Successful call should close
        result = cb.call(lambda: "recovered")
        assert result == "recovered"
        assert cb.state == CircuitBreakerState.CLOSED

    @patch("hephaestus.resilience.circuit_breaker.time.monotonic")
    def test_half_open_to_open_on_failure(self, mock_monotonic: MagicMock) -> None:
        """Failed call in HALF_OPEN transitions back to OPEN."""
        mock_monotonic.return_value = 1000.0
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=30.0)

        # Open the circuit
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        # Advance past recovery timeout
        mock_monotonic.return_value = 1030.0

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
        assert exc_info.value.reason is CircuitBreakerOpenReason.RECOVERY_TIMEOUT

    @patch("hephaestus.resilience.circuit_breaker.time.monotonic")
    def test_half_open_max_calls_exceeded(self, mock_monotonic: MagicMock) -> None:
        """Exceeding half_open_max_calls raises CircuitBreakerOpenError."""
        mock_monotonic.return_value = 1000.0
        cb = CircuitBreaker(
            "test",
            failure_threshold=1,
            recovery_timeout=30.0,
            half_open_max_calls=1,
        )

        # Open the circuit
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        # Advance past recovery timeout
        mock_monotonic.return_value = 1030.0

        # First half-open call fails, re-opening circuit
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("half-open fail")))

        # Now it's OPEN again, so next call should be rejected
        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            cb.call(lambda: "should not run")
        assert exc_info.value.reason is CircuitBreakerOpenReason.RECOVERY_TIMEOUT

    def test_open_state_reports_recovery_timeout_reason(self) -> None:
        """OPEN state raises with reason=RECOVERY_TIMEOUT and positive ETA."""
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=60.0)
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))
        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            cb.call(lambda: "should not run")
        assert exc_info.value.reason is CircuitBreakerOpenReason.RECOVERY_TIMEOUT
        assert exc_info.value.time_until_recovery > 0

    @patch("hephaestus.resilience.circuit_breaker.time.monotonic")
    def test_half_open_exhausted_reports_distinct_reason(self, mock_monotonic: MagicMock) -> None:
        """HALF_OPEN with no slots raises HALF_OPEN_EXHAUSTED with time_until_recovery=0.0."""
        mock_monotonic.return_value = 1000.0
        cb = CircuitBreaker(
            "test",
            failure_threshold=1,
            recovery_timeout=30.0,
            half_open_max_calls=1,
        )
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))
        # Advance past recovery timeout so the next call drives OPEN -> HALF_OPEN.
        # The frozen value is read by both threads; safe here since the test
        # asserts on reason/state/time_until_recovery==0.0, never elapsed time.
        mock_monotonic.return_value = 1030.0

        barrier = threading.Event()
        release = threading.Event()

        def slow() -> str:
            barrier.set()
            release.wait(timeout=2.0)
            return "ok"

        t = threading.Thread(target=lambda: cb.call(slow))
        t.start()
        try:
            assert barrier.wait(timeout=1.0), "slow probe never entered call()"
            with pytest.raises(CircuitBreakerOpenError) as exc_info:
                cb.call(lambda: "rejected")
            assert exc_info.value.reason is CircuitBreakerOpenReason.HALF_OPEN_EXHAUSTED
            assert exc_info.value.time_until_recovery == 0.0
            assert "half-open slot exhausted" in str(exc_info.value)
        finally:
            release.set()
            t.join(timeout=2.0)

    def test_reason_string_value_matches_enum(self) -> None:
        """Reason is string-equality-comparable via the (str, Enum) mixin."""
        err = CircuitBreakerOpenError(
            "x",
            0.0,
            reason=CircuitBreakerOpenReason.HALF_OPEN_EXHAUSTED,
        )
        assert err.reason == "half_open_exhausted"
        assert err.reason is CircuitBreakerOpenReason.HALF_OPEN_EXHAUSTED


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

    @patch("hephaestus.resilience.circuit_breaker.time.monotonic")
    def test_success_threshold_default_one_closes_immediately(
        self, mock_monotonic: MagicMock
    ) -> None:
        """Default success_threshold=1: single success in HALF_OPEN closes the circuit."""
        mock_monotonic.return_value = 1000.0
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=30.0)

        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        mock_monotonic.return_value = 1030.0
        assert cb.state == CircuitBreakerState.HALF_OPEN

        cb.call(lambda: "ok")
        assert cb.state == CircuitBreakerState.CLOSED

    @patch("hephaestus.resilience.circuit_breaker.time.monotonic")
    def test_success_threshold_two_requires_two_successes(self, mock_monotonic: MagicMock) -> None:
        """success_threshold=2: first success keeps circuit HALF_OPEN, second closes it."""
        mock_monotonic.return_value = 1000.0
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=30.0, success_threshold=2)

        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        mock_monotonic.return_value = 1030.0
        assert cb.state == CircuitBreakerState.HALF_OPEN

        cb.call(lambda: 1)
        assert cb.state == CircuitBreakerState.HALF_OPEN  # still half-open

        cb.call(lambda: 2)
        assert cb.state == CircuitBreakerState.CLOSED

    @patch("hephaestus.resilience.circuit_breaker.time.monotonic")
    def test_failure_in_half_open_resets_success_counter(self, mock_monotonic: MagicMock) -> None:
        """A failure in HALF_OPEN re-opens the circuit and resets the success counter."""
        mock_monotonic.return_value = 1000.0
        cb = CircuitBreaker(
            "test",
            failure_threshold=1,
            recovery_timeout=30.0,
            half_open_max_calls=3,
            success_threshold=2,
        )

        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        mock_monotonic.return_value = 1030.0

        cb.call(lambda: "one success")  # half-open success counter = 1

        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail again")))

        assert cb.state == CircuitBreakerState.OPEN

    @patch("hephaestus.resilience.circuit_breaker.time.monotonic")
    def test_success_threshold_reset_clears_counter(self, mock_monotonic: MagicMock) -> None:
        """reset() clears the half-open success counter."""
        mock_monotonic.return_value = 1000.0
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=30.0, success_threshold=2)

        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        mock_monotonic.return_value = 1030.0
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
    ("failure_threshold", "success_threshold"),
    # recovery_timeout axis dropped: it was 0.1 for every case, so it carries
    # zero behavioral coverage once the monotonic clock is mocked.
    [
        (1, 1),
        (3, 1),
        (5, 2),
        (10, 3),
    ],
)
@patch("hephaestus.resilience.circuit_breaker.time.monotonic")
def test_parametrized_thresholds_full_cycle(
    mock_monotonic: MagicMock, failure_threshold: int, success_threshold: int
) -> None:
    """Various threshold configurations follow the full CLOSED→OPEN→HALF_OPEN→CLOSED cycle."""
    mock_monotonic.return_value = 500.0
    reset_all_circuit_breakers()
    cb = CircuitBreaker(
        "param_test",
        failure_threshold=failure_threshold,
        recovery_timeout=30.0,
        half_open_max_calls=success_threshold,  # allow enough probes
        success_threshold=success_threshold,
    )

    for _ in range(failure_threshold):
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

    assert cb.state == CircuitBreakerState.OPEN

    mock_monotonic.return_value = 530.0  # past recovery_timeout
    assert cb.state == CircuitBreakerState.HALF_OPEN

    for _ in range(success_threshold):
        cb.call(lambda: "ok")

    assert cb.state == CircuitBreakerState.CLOSED


class TestCircuitBreakerHalfOpenConcurrency:
    """Tests for half-open admission under concurrent access."""

    @patch("hephaestus.resilience.circuit_breaker.time.monotonic")
    def test_half_open_inflight_never_exceeds_max(self, mock_monotonic: MagicMock) -> None:
        """Peak concurrent in-flight calls never exceed half_open_max_calls.

        With success_threshold > 1, verifies that _half_open_calls is decremented
        on call completion, preventing re-admission while earlier probes are
        executing outside the lock.

        Uses a threading.Barrier to guarantee concurrent execution and
        deterministic peak measurement, eliminating timing dependencies. The
        mocked monotonic clock is frozen at a single value read by every probe
        thread; this is safe because the test asserts only on peak concurrency,
        never on elapsed time.
        """
        half_open_max_calls = 2
        success_threshold = 10
        num_probe_threads = 8

        mock_monotonic.return_value = 1000.0
        cb = CircuitBreaker(
            "concurrency_test",
            failure_threshold=1,
            recovery_timeout=30.0,
            half_open_max_calls=half_open_max_calls,
            success_threshold=success_threshold,
        )

        # Open the circuit
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        assert cb.state == CircuitBreakerState.OPEN

        # Advance past recovery timeout before spawning the probe threads
        mock_monotonic.return_value = 1030.0
        assert cb.state == CircuitBreakerState.HALF_OPEN

        # Track concurrent in-flight calls
        current_inflight = 0
        peak_inflight = 0
        inflight_lock = threading.Lock()
        probe_barrier = threading.Barrier(half_open_max_calls, timeout=2.0)

        def probe_func() -> str:
            """Probe that tracks in-flight concurrency and blocks at barrier."""
            nonlocal current_inflight, peak_inflight
            with inflight_lock:
                current_inflight += 1
                peak_inflight = max(peak_inflight, current_inflight)
            with contextlib.suppress(threading.BrokenBarrierError):
                probe_barrier.wait()
            with inflight_lock:
                current_inflight -= 1
            return "ok"

        # Spawn threads; some will be admitted (up to half_open_max_calls),
        # others will get CircuitBreakerOpenError
        threads = []
        for _ in range(num_probe_threads):

            def attempt() -> None:
                with contextlib.suppress(CircuitBreakerOpenError):
                    cb.call(probe_func)

            t = threading.Thread(target=attempt)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Peak concurrent calls should never exceed half_open_max_calls,
        # even though success_threshold > 1 and _half_open_calls was being reset
        assert peak_inflight <= half_open_max_calls, (
            f"Peak concurrent calls ({peak_inflight}) "
            f"exceeded half_open_max_calls ({half_open_max_calls})"
        )

    @patch("hephaestus.resilience.circuit_breaker.time.monotonic")
    def test_half_open_slot_released_on_success(self, mock_monotonic: MagicMock) -> None:
        """In-flight slot is released on successful call, allowing reuse within half-open phase."""
        half_open_max_calls = 1
        success_threshold = 3

        mock_monotonic.return_value = 1000.0
        cb = CircuitBreaker(
            "slot_reuse_test",
            failure_threshold=1,
            recovery_timeout=30.0,
            half_open_max_calls=half_open_max_calls,
            success_threshold=success_threshold,
        )

        # Open the circuit
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        # Advance past recovery timeout
        mock_monotonic.return_value = 1030.0
        assert cb.state == CircuitBreakerState.HALF_OPEN

        # First probe succeeds
        cb.call(lambda: "ok")
        assert cb.state == CircuitBreakerState.HALF_OPEN
        assert cb._half_open_calls == 0  # Slot released
        assert cb._half_open_successes == 1

        # Second probe reuses the same slot
        cb.call(lambda: "ok")
        assert cb.state == CircuitBreakerState.HALF_OPEN
        assert cb._half_open_calls == 0  # Slot released again
        assert cb._half_open_successes == 2

        # Third probe closes the circuit
        cb.call(lambda: "ok")
        assert cb.state == CircuitBreakerState.CLOSED
        # After transition to CLOSED, counters are reset
        assert cb._half_open_calls == 0

    @patch("hephaestus.resilience.circuit_breaker.time.monotonic")
    def test_half_open_slot_released_on_failure(self, mock_monotonic: MagicMock) -> None:
        """In-flight slot is released on failed call in half-open state."""
        half_open_max_calls = 2
        success_threshold = 2

        mock_monotonic.return_value = 1000.0
        cb = CircuitBreaker(
            "slot_failure_test",
            failure_threshold=1,
            recovery_timeout=30.0,
            half_open_max_calls=half_open_max_calls,
            success_threshold=success_threshold,
        )

        # Open the circuit
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("fail")))

        # Advance past recovery timeout
        mock_monotonic.return_value = 1030.0
        assert cb.state == CircuitBreakerState.HALF_OPEN

        # One probe fails
        with pytest.raises(RuntimeError):
            cb.call(MagicMock(side_effect=RuntimeError("probe fails")))

        # Should be back to OPEN, slot released
        assert cb.state == CircuitBreakerState.OPEN
        assert cb._half_open_calls == 0
