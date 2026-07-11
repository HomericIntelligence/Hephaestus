"""Circuit breaker pattern for external API calls.

Implements a state machine with three states:
- CLOSED: Normal operation, requests pass through
- OPEN: Requests fail fast without calling the external service
- HALF_OPEN: Limited requests allowed to test recovery

Thread-safe implementation using threading.Lock for concurrent access.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitBreakerState(enum.Enum):
    """States for the circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenReason(str, enum.Enum):
    """Why a CircuitBreakerOpenError was raised.

    Lets callers pick a retry strategy without parsing the message:

    - RECOVERY_TIMEOUT: circuit is OPEN; ``time_until_recovery`` is the
      remaining wait until HALF_OPEN probing resumes.
    - HALF_OPEN_EXHAUSTED: circuit is HALF_OPEN with no free slots; another
      in-flight probe is running. ``time_until_recovery`` is 0.0 — retry
      as soon as a slot frees rather than after a timer.
    """

    RECOVERY_TIMEOUT = "recovery_timeout"
    HALF_OPEN_EXHAUSTED = "half_open_exhausted"


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open and requests are rejected."""

    def __init__(
        self,
        name: str,
        time_until_recovery: float,
        reason: CircuitBreakerOpenReason = CircuitBreakerOpenReason.RECOVERY_TIMEOUT,
    ) -> None:
        """Initialize with circuit breaker name, recovery time, and reason.

        Args:
            name: Circuit breaker identifier.
            time_until_recovery: Seconds until recovery attempt. For
                ``reason=HALF_OPEN_EXHAUSTED`` this is 0.0 — the wait is
                for another in-flight probe to finish, not a timer.
            reason: Discriminator for the two raise paths; see
                :class:`CircuitBreakerOpenReason`. Defaults to
                ``RECOVERY_TIMEOUT`` for backward compatibility with
                positional callers.

        """
        self.name = name
        self.time_until_recovery = time_until_recovery
        self.reason = reason
        if reason is CircuitBreakerOpenReason.HALF_OPEN_EXHAUSTED:
            detail = "half-open slot exhausted; another probe in flight"
        else:
            detail = f"Recovery in {time_until_recovery:.1f}s"
        super().__init__(f"Circuit breaker '{name}' is open. {detail}")


class CircuitBreaker:
    """Circuit breaker for external service calls.

    Tracks failures and opens the circuit when a threshold is exceeded,
    preventing further calls until a recovery timeout has elapsed.

    The HALF_OPEN state admits a limited number of concurrent in-flight calls
    to test recovery. The `half_open_max_calls` parameter controls this concurrency
    limit: each admitted call increments an in-flight counter, and each completed
    call (success or failure) decrements it, allowing the next call to proceed.

    Args:
        name: Identifier for this circuit breaker instance
        failure_threshold: Number of consecutive failures before opening
        recovery_timeout: Seconds to wait before transitioning to half-open
        half_open_max_calls: Maximum number of calls admitted concurrently in-flight
            while HALF_OPEN; each call releases its slot on completion (success or failure)
        success_threshold: Consecutive successes in HALF_OPEN required to close
        ignore: Optional predicate deciding whether an exception is evidence of
            service unavailability. See :meth:`call`.

    Example:
        >>> cb = CircuitBreaker("api", failure_threshold=3, recovery_timeout=30)
        >>> result = cb.call(requests.get, "https://api.example.com")

    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 1,
        success_threshold: int = 1,
        *,
        ignore: Callable[[BaseException], bool] | None = None,
    ) -> None:
        """Initialize circuit breaker.

        Args:
            name: Identifier for this circuit breaker instance
            failure_threshold: Consecutive failures before opening
            recovery_timeout: Seconds before transitioning to half-open
            half_open_max_calls: Maximum number of calls admitted concurrently
                in-flight while HALF_OPEN; each call releases its slot on completion
            success_threshold: Consecutive successes in HALF_OPEN to close
            ignore: Predicate returning True for exceptions that do NOT indicate
                the service is unavailable (e.g. an HTTP 404 — the service
                answered, and answered correctly). Such exceptions still
                propagate to the caller but are neither counted as a failure nor
                recorded as a success, so a burst of them can never open the
                breaker and can never clear an accumulating outage signal.
                Defaults to ignoring nothing.

        """
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self.success_threshold = success_threshold
        self._ignore = ignore

        self._state = CircuitBreakerState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._half_open_calls = 0
        self._half_open_successes = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitBreakerState:
        """Current circuit breaker state, accounting for recovery timeout."""
        with self._lock:
            return self._effective_state()

    def _effective_state(self) -> CircuitBreakerState:
        """Compute effective state (must hold lock)."""
        if self._state == CircuitBreakerState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self.recovery_timeout:
                self._state = CircuitBreakerState.HALF_OPEN
                self._half_open_calls = 0
                self._half_open_successes = 0
                logger.info(
                    "Circuit breaker '%s' transitioning to HALF_OPEN after %.1fs",
                    self.name,
                    elapsed,
                )
        return self._state

    def call(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Execute a function through the circuit breaker.

        Args:
            func: Function to call
            *args: Positional arguments for func
            **kwargs: Keyword arguments for func

        An exception matched by the ``ignore`` predicate is re-raised without
        being recorded as either a failure or a success: the service demonstrably
        responded, so it is not evidence of unavailability, but the call did not
        succeed either. Recording it as a success would zero an accumulating
        failure count and mask a real outage.

        Returns:
            Result of func(*args, **kwargs)

        Raises:
            CircuitBreakerOpenError: If circuit is open
            Exception: Any exception from func (after recording failure)

        """
        with self._lock:
            state = self._effective_state()

            if state == CircuitBreakerState.OPEN:
                time_until = self.recovery_timeout - (time.monotonic() - self._last_failure_time)
                raise CircuitBreakerOpenError(
                    self.name,
                    max(0.0, time_until),
                    reason=CircuitBreakerOpenReason.RECOVERY_TIMEOUT,
                )

            if state == CircuitBreakerState.HALF_OPEN:
                if self._half_open_calls >= self.half_open_max_calls:
                    raise CircuitBreakerOpenError(
                        self.name,
                        0.0,
                        reason=CircuitBreakerOpenReason.HALF_OPEN_EXHAUSTED,
                    )
                self._half_open_calls += 1

        # Execute outside the lock to avoid blocking other threads
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            if self._ignore is not None and self._ignore(exc):
                # Leave a trace: an ignored error is invisible to the breaker, so
                # without this a masked outage would have no breaker-level record.
                logger.debug(
                    "Circuit breaker '%s' ignoring non-service exception: %r", self.name, exc
                )
                self._release_half_open_slot()
                raise
            self._record_failure()
            raise

        self._record_success()
        return result

    def _release_half_open_slot(self) -> None:
        """Give back a HALF_OPEN slot without scoring the call either way.

        An ignored exception must not hold its probe slot: leaking it would
        exhaust ``half_open_max_calls`` and wedge the breaker in HALF_OPEN,
        rejecting every later probe with ``HALF_OPEN_EXHAUSTED``.
        """
        with self._lock:
            if self._state == CircuitBreakerState.HALF_OPEN and self._half_open_calls > 0:
                self._half_open_calls -= 1

    def _record_success(self) -> None:
        """Record a successful call (releases this call's half-open slot)."""
        with self._lock:
            if self._state == CircuitBreakerState.HALF_OPEN:
                if self._half_open_calls > 0:
                    self._half_open_calls -= 1
                self._half_open_successes += 1
                if self._half_open_successes < self.success_threshold:
                    return
                logger.info(
                    "Circuit breaker '%s' closing after %d successful half-open call(s)",
                    self.name,
                    self._half_open_successes,
                )
            self._state = CircuitBreakerState.CLOSED
            self._failure_count = 0
            self._half_open_calls = 0
            self._half_open_successes = 0

    def _record_failure(self) -> None:
        """Record a failed call (releases this call's half-open slot)."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._state == CircuitBreakerState.HALF_OPEN:
                if self._half_open_calls > 0:
                    self._half_open_calls -= 1
                self._state = CircuitBreakerState.OPEN
                logger.warning(
                    "Circuit breaker '%s' re-opened after half-open failure",
                    self.name,
                )
            elif self._failure_count >= self.failure_threshold:
                self._state = CircuitBreakerState.OPEN
                logger.warning(
                    "Circuit breaker '%s' opened after %d consecutive failures",
                    self.name,
                    self._failure_count,
                )

    def reset(self) -> None:
        """Reset circuit breaker to closed state."""
        with self._lock:
            self._state = CircuitBreakerState.CLOSED
            self._failure_count = 0
            self._half_open_calls = 0
            self._half_open_successes = 0
            self._last_failure_time = 0.0
            logger.info("Circuit breaker '%s' reset to CLOSED", self.name)


# Global registry of circuit breakers
_registry: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_circuit_breaker(
    name: str,
    failure_threshold: int = 5,
    recovery_timeout: float = 60.0,
    half_open_max_calls: int = 1,
    success_threshold: int = 1,
    *,
    ignore: Callable[[BaseException], bool] | None = None,
) -> CircuitBreaker:
    """Get or create a named circuit breaker (singleton per name).

    The registry is a singleton keyed by *name*. Every configuration argument is
    applied ONLY when the breaker is first created; a later call for an existing
    name returns the cached instance and SILENTLY DISCARDS the arguments — including
    ``ignore``. Construct each named breaker exactly once (at its owning module's
    import), or you will get a breaker whose predicate you did not install.

    Args:
        name: Unique identifier for the circuit breaker
        failure_threshold: Failures before opening (only used on creation)
        recovery_timeout: Recovery timeout in seconds (only used on creation)
        half_open_max_calls: Maximum concurrent in-flight calls in HALF_OPEN state
            (only used on creation)
        success_threshold: Successes in HALF_OPEN to close (only used on creation)
        ignore: Predicate for exceptions that are not evidence of service
            unavailability (only used on creation; silently dropped if *name*
            already exists). See :meth:`CircuitBreaker.call`.

    Returns:
        CircuitBreaker instance for the given name

    """
    with _registry_lock:
        if name not in _registry:
            _registry[name] = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
                half_open_max_calls=half_open_max_calls,
                success_threshold=success_threshold,
                ignore=ignore,
            )
        return _registry[name]


def reset_all_circuit_breakers() -> None:
    """Reset all circuit breakers in the registry.

    Useful for testing to ensure a clean slate between test runs.
    """
    with _registry_lock:
        for cb in _registry.values():
            cb.reset()
        _registry.clear()
