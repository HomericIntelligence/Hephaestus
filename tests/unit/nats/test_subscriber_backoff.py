"""Tests that NATSSubscriberThread reads backoff from NATSConfig, not module constants."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any
from unittest.mock import MagicMock, patch

from hephaestus.nats.config import NATSConfig
from hephaestus.nats.subscriber import NATSSubscriberThread


class TestSubscriberReadsBackoffFromConfig:
    """Tests that subscriber reads backoff configuration from NATSConfig."""

    def test_initial_backoff_taken_from_config(self) -> None:
        config = NATSConfig(
            enabled=True,
            initial_backoff_seconds=0.25,
            max_backoff_seconds=4.0,
            backoff_multiplier=3.0,
        )
        thread = NATSSubscriberThread(config=config, handler=MagicMock())
        # Force one iteration of the reconnect loop, then bail.
        call_count = {"n": 0}

        def _fake_run_until_complete(coro: Coroutine[Any, Any, Any]) -> None:
            coro.close()
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("boom")
            thread._stop_event.set()

        waited: list[float] = []

        def _fake_wait(timeout: float) -> bool:
            waited.append(timeout)
            return False

        with (
            patch("asyncio.new_event_loop") as new_loop,
            patch.object(thread._stop_event, "wait", side_effect=_fake_wait),
        ):
            new_loop.return_value.run_until_complete.side_effect = _fake_run_until_complete
            thread.run()

        # First backoff must equal the config's initial value, not the old 1.0 constant.
        assert waited and waited[0] == 0.25

    def test_max_backoff_caps_growth(self) -> None:
        config = NATSConfig(
            enabled=True,
            initial_backoff_seconds=1.0,
            max_backoff_seconds=2.0,
            backoff_multiplier=10.0,
        )
        thread = NATSSubscriberThread(config=config, handler=MagicMock())
        iters = {"n": 0}

        def _fake_run_until_complete(coro: Coroutine[Any, Any, Any]) -> None:
            coro.close()
            iters["n"] += 1
            if iters["n"] >= 3:
                thread._stop_event.set()
            raise RuntimeError("boom")

        waited: list[float] = []

        def _fake_wait(timeout: float) -> bool:
            waited.append(timeout)
            return False

        with (
            patch("asyncio.new_event_loop") as new_loop,
            patch.object(thread._stop_event, "wait", side_effect=_fake_wait),
        ):
            new_loop.return_value.run_until_complete.side_effect = _fake_run_until_complete
            thread.run()

        # After multiplier=10, second wait would be 10.0; capped to max=2.0.
        assert max(waited) == 2.0
