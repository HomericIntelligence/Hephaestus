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

# Type variable for generic function decoration
F = TypeVar("F", bound=Callable[..., Any])

# Network error keywords to detect transient failures
NETWORK_ERROR_KEYWORDS = [
    "connection",
    "network",
    "timeout",
    "timed out",
    "temporary failure",
    "could not resolve",
    "name resolution",
    "rate limit",
    "throttle",
    "503",
    "502",
    "504",
]


def is_network_error(error: BaseException) -> bool:
    """Check if error is likely a transient network issue.

    Args:
        error: Exception to check

    Returns:
        True if error message contains network error keywords

    """
    error_str = str(error).lower()
    return any(keyword in error_str for keyword in NETWORK_ERROR_KEYWORDS)


def retry_with_backoff(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: int = 2,
    jitter: bool = True,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    logger: Callable[[str], None] | None = None,
    max_delay: float | None = None,
) -> Callable[[F], F]:
    """Retry a function with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        initial_delay: Initial delay in seconds before first retry (default: 1.0)
        backoff_factor: Multiplier for delay between retries (default: 2)
        jitter: Add random jitter to delay times (default: True)
        retry_on: Tuple of exception types to retry on (default: all exceptions)
        logger: Optional logging function for retry attempts
        max_delay: Maximum delay cap in seconds (default: None, no cap)

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

                    # Don't retry on last attempt
                    if attempt == max_retries:
                        break

                    # Calculate delay with exponential backoff
                    delay = initial_delay * (backoff_factor**attempt)

                    # Cap delay if max_delay is specified
                    if max_delay is not None:
                        delay = min(delay, max_delay)

                    # Add jitter if requested (±25%)
                    if jitter:
                        jitter_amount = random.uniform(-0.25 * delay, 0.25 * delay)
                        delay += jitter_amount

                    # Ensure delay is positive
                    delay = max(0.1, delay)

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


# Backwards-compatibility shim — delegates to retry_with_backoff to avoid DRY violation.
# Deprecated: call retry_with_backoff(jitter=True, max_delay=...) directly instead.
def retry_with_jitter(
    func: Callable[..., Any], max_retries: int = 3, base_delay: float = 1.0, max_delay: float = 60.0
) -> Any:
    """Retry a function with exponential backoff and jitter.

    .. deprecated::
        Use :func:`retry_with_backoff` with ``jitter=True`` and ``max_delay`` directly.
        This shim will be removed in a future major version.

    Args:
        func: Function to retry
        max_retries: Maximum number of retry attempts
        base_delay: Base delay in seconds (``initial_delay`` in retry_with_backoff)
        max_delay: Maximum delay cap between retries

    Returns:
        Result of successful function call

    Raises:
        Exception: Last exception raised if all retries fail

    """
    import warnings

    warnings.warn(
        "retry_with_jitter() is deprecated; use "
        "retry_with_backoff(jitter=True, max_delay=...) instead.",
        DeprecationWarning,
        stacklevel=2,
    )

    decorated = retry_with_backoff(
        max_retries=max_retries,
        initial_delay=base_delay,
        backoff_factor=2,
        jitter=True,
        max_delay=max_delay,
    )
    return decorated(func)()
