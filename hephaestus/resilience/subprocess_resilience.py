"""Resilience utilities composing retry and circuit breaker patterns.

Provides high-level helpers for subprocess calls with:
- Automatic retry on transient errors (network, subprocess crashes)
- Circuit breaker integration for fail-fast on repeated failures
- Rate limit error passthrough (not retried, handled by callers)
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from typing import Any, TypeVar

from hephaestus.constants import TRANSIENT_ERROR_CORE
from hephaestus.resilience.circuit_breaker import CircuitBreaker, get_circuit_breaker
from hephaestus.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)

R = TypeVar("R")

# Transient subprocess error types that should be retried
TRANSIENT_SUBPROCESS_ERRORS: tuple[type[Exception], ...] = (
    subprocess.SubprocessError,
    ConnectionError,
    TimeoutError,
    OSError,
)

# Patterns in stderr that indicate transient (retryable) failures.
# Derived from the shared TRANSIENT_ERROR_CORE (issue #1205) plus
# subprocess-specific signals. The explicit ``connection *`` / ``could not
# resolve host`` phrases are retained as extras (rather than relying solely on
# the broader core substrings) so callers and tests asserting exact membership
# keep working. Kept as a sorted list because call sites iterate it.
_SUBPROCESS_TRANSIENT_EXTRAS: frozenset[str] = frozenset(
    {
        "connection reset",
        "connection refused",
        "connection timed out",
        "network is unreachable",
        "could not resolve host",
        "curl 56",
        "early eof",
        "recv failure",
        "broken pipe",
        "ssl handshake",
    }
)
TRANSIENT_ERROR_PATTERNS: list[str] = sorted(TRANSIENT_ERROR_CORE | _SUBPROCESS_TRANSIENT_EXTRAS)


def is_transient_subprocess_error(error: BaseException) -> bool:
    """Check if a subprocess error is transient and should be retried.

    Checks both the exception type and any stderr content in the error
    message for known transient patterns.

    Accepts ``BaseException`` so the function is directly usable as a
    ``retry_predicate`` for :func:`hephaestus.utils.retry.retry_with_backoff`
    (which calls predicates with the broadest exception type). Non-Exception
    base exceptions (e.g. ``KeyboardInterrupt``) fall through the type checks
    and return ``False``.

    Args:
        error: Exception to check

    Returns:
        True if the error is transient and retryable

    """
    # Intentional timeouts should not be retried
    if isinstance(error, subprocess.TimeoutExpired):
        return False

    if isinstance(error, TRANSIENT_SUBPROCESS_ERRORS):
        # For generic OSError/SubprocessError, check for transient patterns
        if isinstance(error, (OSError, subprocess.SubprocessError)):
            # Build searchable text from all available error info
            parts = [str(error).lower()]
            if isinstance(error, subprocess.CalledProcessError):
                if error.stderr:
                    parts.append(str(error.stderr).lower())
                if error.stdout:
                    parts.append(str(error.stdout).lower())
            error_str = " ".join(parts)
            return any(pattern in error_str for pattern in TRANSIENT_ERROR_PATTERNS)
        return True

    return False


def resilient_call(
    func: Callable[..., R],
    *args: Any,
    circuit_breaker_name: str | None = None,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    retry_predicate: Callable[[BaseException], bool] | None = None,
    **kwargs: Any,
) -> R:
    """Execute a function with retry and optional circuit breaker.

    Combines retry-with-backoff and circuit breaker for external calls.
    Rate limit errors are NOT retried — they propagate immediately for
    the caller to handle.

    Only *transient* errors are retried: the broad ``TRANSIENT_SUBPROCESS_ERRORS``
    tuple is paired with :func:`is_transient_subprocess_error` as a predicate
    so that, e.g., a ``PermissionError`` (``OSError``) does not waste three
    retries on a clearly non-transient failure, and ``subprocess.TimeoutExpired``
    propagates immediately because timeouts are intentional, not flakes.

    Args:
        func: Function to call
        *args: Positional arguments for func
        circuit_breaker_name: Optional circuit breaker name for fail-fast
        max_retries: Maximum retry attempts
        initial_delay: Initial backoff delay in seconds
        max_delay: Maximum backoff delay cap in seconds
        retry_predicate: Optional extra predicate applied after the standard
            transient-error classifier. Returning False suppresses retry.
        **kwargs: Keyword arguments for func

    Returns:
        Result of func(*args, **kwargs)

    Raises:
        CircuitBreakerOpenError: If circuit breaker is open
        Exception: Final exception after retries exhausted

    """
    cb: CircuitBreaker | None = None
    if circuit_breaker_name:
        cb = get_circuit_breaker(circuit_breaker_name)

    def _retry_predicate(error: BaseException) -> bool:
        if not is_transient_subprocess_error(error):
            return False
        return retry_predicate is None or retry_predicate(error)

    @retry_with_backoff(
        max_retries=max_retries,
        initial_delay=initial_delay,
        backoff_factor=2,
        max_delay=max_delay,
        retry_on=TRANSIENT_SUBPROCESS_ERRORS,
        retry_predicate=_retry_predicate,
        logger=logger.warning,
        jitter=True,
    )
    def _inner() -> R:
        if cb:
            return cb.call(func, *args, **kwargs)
        return func(*args, **kwargs)

    return _inner()
