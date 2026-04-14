"""Tests for hephaestus.nats.subscriber."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from hephaestus.nats.config import NATSConfig
from hephaestus.nats.subscriber import NATSSubscriberThread


def _config(**kwargs: object) -> NATSConfig:
    return NATSConfig(enabled=True, **kwargs)  # type: ignore[arg-type]


class TestNATSSubscriberThread:
    """Tests for NATSSubscriberThread."""

    def test_is_daemon_thread(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        assert thread.daemon is True

    def test_thread_name(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        assert thread.name == "NATSSubscriberThread"

    def test_stop_before_start_does_not_raise(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        # stop() calls join() — should not raise even if never started
        thread._stop_event.set()
        thread.stop()

    def test_stop_event_is_threading_event(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        assert isinstance(thread._stop_event, threading.Event)

    def test_config_stored(self) -> None:
        config = _config(url="nats://test:4222")
        thread = NATSSubscriberThread(config=config, handler=MagicMock())
        assert thread._config.url == "nats://test:4222"
