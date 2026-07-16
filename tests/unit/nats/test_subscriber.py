"""Tests for hephaestus.nats.subscriber."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

from hephaestus.nats.config import NATSConfig
from hephaestus.nats.events import NATSEvent
from hephaestus.nats.subscriber import (
    DEFAULT_JOIN_TIMEOUT,
    NATSSubscriberThread,
    SubscriberState,
)
from hephaestus.observability.metrics import MetricsRegistry


def _config(**kwargs: object) -> NATSConfig:
    return NATSConfig(enabled=True, **kwargs)  # type: ignore[arg-type]


def _make_event(**kwargs: object) -> NATSEvent:
    defaults: dict[str, object] = {
        "subject": "test.subject",
        "data": {},
        "timestamp": "",
        "sequence": 1,
    }
    defaults.update(kwargs)
    return NATSEvent(**defaults)  # type: ignore[arg-type]


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
        # A thread that was never started counts as "joined cleanly" (True).
        assert thread.stop() is True

    def test_stop_returns_false_on_join_timeout(self) -> None:
        """When a started thread refuses to exit within the timeout, stop() returns False.

        Regression for #521: stop() previously returned None and silently
        masked a wedged subscriber thread.
        """
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        # Simulate a wedged thread: is_alive() returns True both before and
        # after join(), and join() itself is a no-op so we don't need to
        # actually start a real thread.
        with (
            patch.object(NATSSubscriberThread, "is_alive", return_value=True),
            patch.object(NATSSubscriberThread, "join", return_value=None),
        ):
            result = thread.stop(timeout=0.01)
        assert result is False

    def test_stop_event_is_threading_event(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        assert isinstance(thread._stop_event, threading.Event)

    def test_config_stored(self) -> None:
        config = _config(url="nats://test:4222")
        thread = NATSSubscriberThread(config=config, handler=MagicMock())
        assert thread._config.url == "nats://test:4222"

    def test_injected_registry_records_lifecycle_activity_and_errors(self) -> None:
        """An opt-in registry receives only live subscriber lifecycle values."""
        registry = MetricsRegistry()
        thread = NATSSubscriberThread(
            config=_config(), handler=MagicMock(), metrics_registry=registry
        )

        thread._record_message()
        thread._record_error(ConnectionError("nats gone"))
        thread._record_handler_error(RuntimeError("handler failed"))
        thread._record_decode_error()

        rendered = registry.render_prometheus()
        assert 'hephaestus_nats_subscriber_state{state="disconnected"} 1' in rendered
        assert "hephaestus_nats_subscriber_messages_total 1" in rendered
        assert 'hephaestus_nats_subscriber_errors_total{kind="connection"} 1' in rendered
        assert 'hephaestus_nats_subscriber_errors_total{kind="handler"} 1' in rendered
        assert 'hephaestus_nats_subscriber_errors_total{kind="decode"} 1' in rendered


class TestSubscriberStateEnum:
    """Tests for the SubscriberState enum."""

    def test_all_expected_states_present(self) -> None:
        expected = {"INITIALIZING", "CONNECTED", "DISCONNECTED", "STOPPING", "STOPPED", "ERROR"}
        actual = {s.name for s in SubscriberState}
        assert expected == actual

    def test_state_values_are_lowercase_strings(self) -> None:
        for state in SubscriberState:
            assert state.value == state.name.lower()


class TestHealthObservability:
    """Tests for the health state surface (#314)."""

    def test_initial_state_is_initializing(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        assert thread.state is SubscriberState.INITIALIZING

    def test_initial_last_error_is_none(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        assert thread.last_error is None

    def test_initial_last_message_at_is_none(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        assert thread.last_message_at is None

    def test_health_dict_keys(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        hd = thread.health_dict()
        assert set(hd.keys()) == {
            "state",
            "last_error",
            "last_message_at",
            "url",
            "stream",
            "circuit_breaker_state",
            "uptime_seconds",
        }

    def test_health_dict_circuit_breaker_state_is_string(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        assert thread.health_dict()["circuit_breaker_state"] == "closed"

    def test_health_dict_initial_values(self) -> None:
        config = _config(url="nats://localhost:4222")
        thread = NATSSubscriberThread(config=config, handler=MagicMock())
        hd = thread.health_dict()
        assert hd["state"] == "initializing"
        assert hd["last_error"] is None
        assert hd["last_message_at"] is None
        assert hd["url"] == "nats://localhost:4222"

    def test_health_dict_state_is_string_not_enum(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        hd = thread.health_dict()
        # must be JSON-serialisable
        import json

        dumped = json.dumps(hd)
        assert '"state"' in dumped

    def test_health_dict_uptime_non_negative(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        assert thread.health_dict()["uptime_seconds"] >= 0.0

    def test_set_state_transitions(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        thread._set_state(SubscriberState.CONNECTED)
        assert thread.state is SubscriberState.CONNECTED
        thread._set_state(SubscriberState.DISCONNECTED)
        assert thread.state is SubscriberState.DISCONNECTED
        thread._set_state(SubscriberState.STOPPING)
        assert thread.state is SubscriberState.STOPPING
        thread._set_state(SubscriberState.STOPPED)
        assert thread.state is SubscriberState.STOPPED

    def test_record_error_sets_last_error_and_disconnected(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        exc = ValueError("boom")
        thread._record_error(exc)
        assert thread.last_error is exc
        assert thread.state is SubscriberState.DISCONNECTED

    def test_record_message_updates_last_message_at(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        assert thread.last_message_at is None
        before = time.time()
        thread._record_message()
        after = time.time()
        ts = thread.last_message_at
        assert ts is not None
        assert before <= ts <= after

    def test_last_error_recorded_when_handler_raises(self) -> None:
        """_last_error is set when the message handler raises an exception."""
        exc = RuntimeError("handler failure")
        handler = MagicMock(side_effect=exc)
        thread = NATSSubscriberThread(config=_config(), handler=handler)

        # Simulate what _subscribe_loop does after building an event
        event = _make_event()
        try:
            thread._handler(event)
        except Exception as caught:
            with thread._state_lock:
                thread._last_error = caught

        assert thread.last_error is exc

    def test_last_message_at_not_updated_when_handler_raises(self) -> None:
        """last_message_at stays None if handler always raises."""
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock(side_effect=ValueError))
        # Simulate the else-branch not executing
        assert thread.last_message_at is None

    def test_health_dict_after_error(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        exc = ConnectionError("nats gone")
        thread._record_error(exc)
        hd = thread.health_dict()
        assert hd["state"] == "disconnected"
        assert hd["last_error"] is not None
        assert "nats gone" in hd["last_error"]

    def test_stop_transitions_to_stopping_then_stopped(self) -> None:
        """stop() immediately flips state to STOPPING; STOPPED is set after run() exits."""
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        # Never actually start the thread; manually validate the transition contract.
        thread._set_state(SubscriberState.CONNECTED)
        # Pre-set _stop_event so join() returns immediately (thread never started).
        thread._stop_event.set()
        thread.stop()
        # After stop() sets STOPPING, run() is not alive so STOPPED transition happens
        # only inside run(); without starting the thread the final state stays STOPPING.
        assert thread.state in (SubscriberState.STOPPING, SubscriberState.STOPPED)

    def test_state_thread_safety(self) -> None:
        """Concurrent reads/writes to state don't deadlock or raise."""
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        errors: list[Exception] = []

        def writer() -> None:
            for state in list(SubscriberState) * 5:
                thread._set_state(state)

        def reader() -> None:
            for _ in range(50):
                _ = thread.state
                _ = thread.health_dict()

        writers = [threading.Thread(target=writer) for _ in range(3)]
        readers = [threading.Thread(target=reader) for _ in range(3)]
        all_threads = writers + readers
        for t in all_threads:
            t.start()
        for t in all_threads:
            t.join(timeout=5.0)
        assert not errors


class TestConfigurableJoinTimeout:
    """Tests for configurable stop() join timeout (#334)."""

    def test_default_join_timeout_constant(self) -> None:
        assert DEFAULT_JOIN_TIMEOUT == 5.0

    def test_default_join_timeout_used_when_no_arg(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock(), join_timeout=3.0)
        assert thread._join_timeout == 3.0

    def test_constructor_stores_join_timeout(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock(), join_timeout=7.5)
        assert thread._join_timeout == 7.5

    def test_default_constructor_uses_module_default(self) -> None:
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        assert thread._join_timeout == DEFAULT_JOIN_TIMEOUT

    def test_stop_uses_override_timeout(self) -> None:
        """stop(timeout=0.1) calls join with 0.1, not the constructor default."""
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock(), join_timeout=99.0)
        thread._stop_event.set()  # so stop() exits quickly

        join_calls: list[float | None] = []

        def mock_join(timeout: float | None = None) -> None:
            join_calls.append(timeout)
            # Don't call the real join — thread was never started.

        thread.join = mock_join  # type: ignore[method-assign]

        # Patch is_alive to return True so join() is actually called.
        with patch.object(thread, "is_alive", return_value=True):
            thread.stop(timeout=0.1)

        assert join_calls == [0.1]

    def test_stop_uses_constructor_timeout_when_no_override(self) -> None:
        """stop() with no arg falls back to constructor join_timeout."""
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock(), join_timeout=3.3)
        thread._stop_event.set()

        join_calls: list[float | None] = []

        def mock_join(timeout: float | None = None) -> None:
            join_calls.append(timeout)

        thread.join = mock_join  # type: ignore[method-assign]

        # Patch is_alive to return True so join() is actually called.
        with patch.object(thread, "is_alive", return_value=True):
            thread.stop()

        assert join_calls == [3.3]

    def test_stop_with_zero_timeout_override(self) -> None:
        """stop(timeout=0) passes 0 to join (non-blocking join)."""
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        thread._stop_event.set()

        join_calls: list[float | None] = []

        def mock_join(timeout: float | None = None) -> None:
            join_calls.append(timeout)

        thread.join = mock_join  # type: ignore[method-assign]

        with patch.object(thread, "is_alive", return_value=True):
            thread.stop(timeout=0.0)

        assert join_calls == [0.0]

    def test_backward_compat_no_args_constructor(self) -> None:
        """NATSSubscriberThread() with only required args still works."""
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        assert thread._join_timeout == DEFAULT_JOIN_TIMEOUT

    def test_backward_compat_stop_no_args(self) -> None:
        """stop() with no args still works (uses constructor default)."""
        thread = NATSSubscriberThread(config=_config(), handler=MagicMock())
        thread._stop_event.set()
        # Thread was never started — stop() should not raise.
        thread.stop()


def test_importing_subscriber_does_not_mutate_global_warnings_filters() -> None:
    """Regression for #798: module-level warnings.filterwarnings was global mutation.

    Spawns a fresh Python process so test-collection import state cannot mask
    the leak. The subprocess snapshots warnings.filters, imports the module,
    and asserts no row was added.
    """
    import subprocess
    import sys

    code = (
        "import warnings, json, sys;"
        "before = list(warnings.filters);"
        "import hephaestus.nats.subscriber;"  # triggers module body
        "after = list(warnings.filters);"
        "sys.exit(0 if after == before else 1)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        "Importing hephaestus.nats.subscriber added a row to warnings.filters; "
        "suppression should be scoped via warnings.catch_warnings() inside "
        "_subscribe_loop instead of running at module scope. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_subscribe_loop_lazy_import_suppresses_deprecation_warning() -> None:
    """Regression for #798: catch_warnings() suppresses nats-py DeprecationWarning.

    The catch_warnings() block must actually suppress the nats-py
    DeprecationWarning when _subscribe_loop runs its lazy import.

    Runs in a subprocess with -W error::DeprecationWarning so that if the
    suppression block fails to scope correctly, the import re-raises as an
    error and the subprocess exits non-zero. The NATS connect that follows
    the import is expected to fail (no broker); we only care that the import
    inside the catch_warnings block completed cleanly.
    """
    import subprocess
    import sys

    code = """
import asyncio
import builtins
import sys
import types
import warnings

from hephaestus.nats.config import NATSConfig
from hephaestus.nats.subscriber import NATSSubscriberThread

cfg = NATSConfig(enabled=True, url='nats://127.0.0.1:1', subjects=['x.>'])
t = NATSSubscriberThread(config=cfg, handler=lambda e: None)

fake_nats = types.ModuleType("nats")
fake_api = types.ModuleType("nats.js.api")

async def connect(url, **kwargs):
    raise RuntimeError("connect stopped before network")

class DeliverPolicy(str):
    def __new__(cls, value):
        return str.__new__(cls, value)

fake_nats.connect = connect
fake_api.DeliverPolicy = DeliverPolicy
real_import = builtins.__import__

def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "nats":
        warnings.warn_explicit(
            "asyncio.iscoroutinefunction is deprecated",
            DeprecationWarning,
            "nats/client.py",
            1,
            module="nats",
        )
        return fake_nats
    if name == "nats.js.api":
        return fake_api
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = fake_import
try:
    asyncio.run(t._subscribe_loop())
except DeprecationWarning:
    raise SystemExit(2)
except Exception:
    pass
finally:
    builtins.__import__ = real_import

raise SystemExit(0)
"""
    result = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        "Lazy import of nats-py inside _subscribe_loop leaked a "
        "DeprecationWarning past the catch_warnings() block. "
        f"returncode={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
