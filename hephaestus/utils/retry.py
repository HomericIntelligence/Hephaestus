#!/usr/bin/env python3
"""Enhanced retry utilities with exponential backoff for ProjectHephaestus.

Provides automatic retry logic with configurable parameters:
- Exponential backoff with jitter
- Network error detection
- Max retries limit
- Flexible exception handling
- Integration with logging framework
"""

import functools
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar, cast

from hephaestus.constants import TRANSIENT_ERROR_CORE

# Type variable for generic function decoration
F = TypeVar("F", bound=Callable[..., Any])

# Network error keywords to detect transient failures. Derived from the shared
# TRANSIENT_ERROR_CORE (issue #1205) plus retry-specific signals — including
# rate-limit/throttle, which (unlike the resilience layer) the retry layer DOES
# treat as retryable network errors.
_NETWORK_EXTRA_KEYWORDS: frozenset[str] = frozenset(
    {
        "network",
        "timeout",
        "name resolution",
        "rate limit",
        "throttle",
    }
)
NETWORK_ERROR_KEYWORDS: list[str] = sorted(TRANSIENT_ERROR_CORE | _NETWORK_EXTRA_KEYWORDS)


def is_network_error(error: BaseException) -> bool:
    """Check if error is likely a transient network issue.

    Args:
        error: Exception to check

    Returns:
        True if error message contains network error keywords

    """
    error_str = str(error).lower()
    return any(keyword in error_str for keyword in NETWORK_ERROR_KEYWORDS)


def _compute_backoff_delay(
    attempt: int,
    initial_delay: float,
    backoff_factor: int,
    max_delay: float | None,
    jitter: bool,
) -> float:
    """Compute the sleep delay for the given retry attempt.

    Args:
        attempt: Zero-based attempt index (0 for the first retry).
        initial_delay: Initial delay in seconds before first retry.
        backoff_factor: Multiplier for delay between retries.
        max_delay: Optional hard cap on the sleep value. Applied *after* jitter
            so the returned delay never exceeds max_delay (subject to the 0.1s
            minimum floor below).
        jitter: If True, perturb the delay by ±25 %.

    Returns:
        Sleep duration in seconds (always >= 0.1, and <= max_delay when a cap
        is set and max_delay >= 0.1).

    """
    delay: float = initial_delay * (backoff_factor**attempt)
    if max_delay is not None:
        delay = min(delay, max_delay)
    if jitter:
        delay = float(delay + random.uniform(-0.25 * delay, 0.25 * delay))
    # Re-apply the cap *after* jitter so max_delay is a hard ceiling, not an
    # advisory target. Without this, +25% jitter could push the sleep to
    # max_delay * 1.25 (see issue #1206).
    if max_delay is not None:
        delay = min(delay, max_delay)
    return max(0.1, delay)


def retry_with_backoff(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: int = 2,
    jitter: bool = True,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    logger: Callable[[str], None] | None = None,
    max_delay: float | None = None,
    retry_predicate: Callable[[BaseException], bool] | None = None,
) -> Callable[[F], F]:
    """Retry a function with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        initial_delay: Initial delay in seconds before first retry (default: 1.0)
        backoff_factor: Multiplier for delay between retries (default: 2)
        jitter: Add random jitter to delay times (default: True)
        retry_on: Tuple of exception types to retry on (default: all exceptions)
        logger: Optional logging function for retry attempts
        max_delay: Maximum delay cap in seconds — a hard ceiling honored even
            with jitter enabled (default: None, no cap)
        retry_predicate: Optional callable applied *after* the ``retry_on``
            isinstance check. If provided and the predicate returns ``False``
            for the raised exception, the exception is re-raised immediately
            without retrying. Lets callers filter beyond exception type — for
            example, retry only ``OSError``s whose message looks transient.

    Returns:
        Decorated function with retry logic

    Example:
        @retry_with_backoff(max_retries=3, initial_delay=2.0)
        def unstable_network_call():
            # May fail transiently
            response = requests.get("https://api.github.com")
            return response.json()

        @retry_with_backoff(retry_on=(ConnectionError, TimeoutError))
        def api_call():
            # Only retry on specific exceptions
            return requests.get("https://api.example.com")

        @retry_with_backoff(
            retry_on=(OSError,),
            retry_predicate=lambda e: "connection reset" in str(e).lower(),
        )
        def transient_only():
            # Retries OSErrors only when their message looks transient
            ...

    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retry_on as e:
                    last_exception = e

                    # Honor a caller-supplied filter that runs *after* the
                    # isinstance check. If it rejects the exception, propagate
                    # immediately so non-transient failures don't waste retries.
                    if retry_predicate is not None and not retry_predicate(e):
                        raise

                    # Don't retry on last attempt
                    if attempt == max_retries:
                        break

                    delay = _compute_backoff_delay(
                        attempt, initial_delay, backoff_factor, max_delay, jitter
                    )

                    # Log retry attempt
                    if logger:
                        error_type = type(e).__name__
                        is_network = is_network_error(e)
                        network_tag = " [NETWORK]" if is_network else ""
                        logger(
                            f"Retry {attempt + 1}/{max_retries} after"
                            f" {error_type}{network_tag}: {e} (waiting {delay:.2f}s)"
                        )

                    # Wait before retry
                    time.sleep(delay)

            # All retries exhausted, raise last exception
            if last_exception:
                raise last_exception

            # Should never reach here, but satisfy type checker
            return None

        return cast(F, wrapper)

    return decorator


def retry_on_network_error(
    max_retries: int = 3, initial_delay: float = 2.0, logger: Callable[[str], None] | None = None
) -> Callable[[F], F]:
    """Retry on network errors only (convenience wrapper).

    Args:
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay in seconds (will grow exponentially)
        logger: Optional logging function

    Returns:
        Decorated function with network error retry logic

    """
    return retry_with_backoff(
        max_retries=max_retries,
        initial_delay=initial_delay,
        backoff_factor=2,
        retry_on=(ConnectionError, TimeoutError, OSError),
        logger=logger,
    )
