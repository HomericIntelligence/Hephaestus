"""Circuit breaker regression tests for NATSSubscriberThread."""

from __future__ import annotations

import contextlib
from collections.abc import Coroutine
from typing import Any
from unittest.mock import MagicMock, patch

from hephaestus.nats.config import NATSConfig
from hephaestus.nats.subscriber import (
    NATS_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    NATSSubscriberThread,
    SubscriberState,
)
from hephaestus.resilience import CircuitBreakerOpenError


def _config(**kwargs: object) -> NATSConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "initial_backoff_seconds": 0.01,
        "max_backoff_seconds": 0.01,
    }
    defaults.update(kwargs)
    return NATSConfig(**defaults)  # type: ignore[arg-type]


class TestNATSSubscriberCircuitBreaker:
    """Tests for subscriber reconnection circuit-breaker behavior."""

    def test_persistent_connection_failures_open_circuit_and_enter_error(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        loop = MagicMock()

        def fail_subscribe(coro: Coroutine[Any, Any, Any]) -> None:
            coro.close()
            raise RuntimeError("nats down")

        loop.run_until_complete.side_effect = fail_subscribe

        waits: list[float] = []

        def stop_after_threshold(timeout: float) -> bool:
            waits.append(timeout)
            if len(waits) >= NATS_CIRCUIT_BREAKER_FAILURE_THRESHOLD:
                thread._stop_event.set()
            return False

        with (
            patch("asyncio.new_event_loop", return_value=loop),
            patch.object(thread._stop_event, "wait", side_effect=stop_after_threshold),
        ):
            thread.run()

        assert loop.run_until_complete.call_count == NATS_CIRCUIT_BREAKER_FAILURE_THRESHOLD
        assert thread.state is SubscriberState.ERROR
        assert thread.last_error is not None
        assert "nats down" in str(thread.last_error)
        health = thread.health_dict()
        assert health["state"] == "error"
        assert health["last_error"] is not None
        assert "nats down" in health["last_error"]
        assert health["circuit_breaker_state"] == "open"

    def test_open_circuit_fails_fast_without_subscribing(self) -> None:
        thread = NATSSubscriberThread(
            config=_config(max_backoff_seconds=60.0),
            handler=MagicMock(),
        )

        def fail() -> None:
            raise RuntimeError("seed failure")

        for _ in range(NATS_CIRCUIT_BREAKER_FAILURE_THRESHOLD):
            with contextlib.suppress(RuntimeError):
                thread._circuit_breaker.call(fail)

        loop = MagicMock()
        with patch("asyncio.new_event_loop", return_value=loop):
            thread.run()

        loop.run_until_complete.assert_not_called()
        assert thread.state is SubscriberState.ERROR
        assert isinstance(thread.last_error, CircuitBreakerOpenError)
        health = thread.health_dict()
        assert health["state"] == "error"
        assert health["last_error"] is not None
        assert "Circuit breaker 'nats-subscriber' is open" in health["last_error"]
        assert health["circuit_breaker_state"] == "open"
