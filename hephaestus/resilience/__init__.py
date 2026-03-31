"""Resilience utilities for ProjectHephaestus.

Provides circuit breaker and retry-with-resilience patterns for external calls.
"""

from __future__ import annotations

from hephaestus.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitBreakerState,
    get_circuit_breaker,
    reset_all_circuit_breakers,
)
from hephaestus.resilience.subprocess_resilience import (
    TRANSIENT_ERROR_PATTERNS,
    TRANSIENT_SUBPROCESS_ERRORS,
    is_transient_subprocess_error,
    resilient_call,
)

__all__ = [
    "TRANSIENT_ERROR_PATTERNS",
    "TRANSIENT_SUBPROCESS_ERRORS",
    "CircuitBreaker",
    "CircuitBreakerOpenError",
    "CircuitBreakerState",
    "get_circuit_breaker",
    "is_transient_subprocess_error",
    "reset_all_circuit_breakers",
    "resilient_call",
]
