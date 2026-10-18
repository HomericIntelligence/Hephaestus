"""Hermetic fault-injection coverage for resilience primitives (#1489)."""

from __future__ import annotations

import asyncio
import errno
import json
import signal
import subprocess
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hephaestus.nats.config import NATSConfig
from hephaestus.nats.consumer import ExplicitConsumerConfig
from hephaestus.nats.delivery_ledger import StoredLedger
from hephaestus.nats.events import NATSEvent
from hephaestus.nats.failure_store import (
    EXPECTED_LAST_SUBJECT_SEQUENCE_HEADER,
    ExistingStream,
    PubAckView,
    SequenceConflictError,
    StoredSourceMessage,
    StreamTopology,
)
from hephaestus.nats.subscriber import NATSSubscriberThread, SubscriberState
from hephaestus.resilience.circuit_breaker import (
    CircuitBreakerOpenError,
    CircuitBreakerState,
    get_circuit_breaker,
    reset_all_circuit_breakers,
)
from hephaestus.resilience.subprocess_resilience import resilient_call

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clean_breakers() -> None:
    """Keep named circuit-breaker state isolated between fault-injection tests."""
    reset_all_circuit_breakers()


class _FakeBroker:
    """In-memory JetStream and consumer admin seam for the NATS fault tests.

    Every stream and consumer starts absent, so the subscriber provisions
    the four failure-resource topologies and creates the explicit pull
    consumer; the publish path applies the ledger CAS contract.
    """

    def __init__(self) -> None:
        self.ledger_rows: dict[str, StoredLedger] = {}

    async def stream_info(self, name: str) -> ExistingStream | None:
        return None

    async def add_stream(self, topology: StreamTopology) -> None:
        return None

    async def update_stream(self, name: str, *, subjects: frozenset[str]) -> None:
        raise AssertionError("update_stream must not run in the fault harness")

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: dict[str, str] | None = None,
    ) -> PubAckView:
        expected: int | None = None
        if headers is not None:
            raw = headers.get(EXPECTED_LAST_SUBJECT_SEQUENCE_HEADER)
            if raw is not None:
                expected = int(raw)
        current = self.ledger_rows.get(subject)
        sequence = 0 if current is None else current.sequence
        if expected is not None and expected != sequence:
            raise SequenceConflictError(f"stale publish for {subject}")
        new_sequence = sequence + 1
        self.ledger_rows[subject] = StoredLedger(sequence=new_sequence, payload=payload)
        return PubAckView(
            stream="HEPHAESTUS_NATS_DELIVERY_LEDGER", sequence=new_sequence, duplicate=False
        )

    async def get_message(self, stream: str, sequence: int) -> StoredSourceMessage | None:
        return None

    async def get_last_message(self, stream: str, subject: str) -> StoredLedger | None:
        return self.ledger_rows.get(subject)

    async def consumer_info(self, stream: str, durable: str) -> None:
        return None

    async def add_consumer(self, stream: str, config: ExplicitConsumerConfig) -> None:
        return None

    async def delete_consumer(self, stream: str, durable: str) -> None:
        raise AssertionError("delete_consumer must not run in the fault harness")

    async def restore_consumer(self, stream: str, snapshot: dict[str, Any]) -> None:
        raise AssertionError("restore_consumer must not run in the fault harness")


def _make_msg(payload: dict[str, Any]) -> MagicMock:
    """Build a mock JetStream message with valid durable metadata."""
    msg = MagicMock()
    msg.data = json.dumps(payload).encode()
    msg.subject = "hi.tasks.demo"
    msg.headers = None
    metadata = MagicMock()
    metadata.stream = "TASKS"
    metadata.consumer = "hephaestus-subscriber"
    metadata.sequence.stream = 1
    metadata.num_delivered = 1
    msg.metadata = metadata
    msg.ack = AsyncMock()
    msg.ack_sync = AsyncMock()
    msg.nak = AsyncMock()
    msg.term = AsyncMock()
    msg.in_progress = AsyncMock()
    return msg


def _make_connection(fetch: AsyncMock) -> MagicMock:
    """Build a fake NATS connection whose pull subscription yields from ``fetch``."""
    sub = MagicMock()
    sub.fetch = fetch
    js = MagicMock()
    js.pull_subscribe = AsyncMock(return_value=sub)
    advisory_sub = MagicMock()
    advisory_sub.unsubscribe = AsyncMock()
    nc = MagicMock()
    nc.jetstream = MagicMock(return_value=js)
    nc.subscribe = AsyncMock(return_value=advisory_sub)
    nc.drain = AsyncMock()
    return nc


def _install_fake_nats(connect: AsyncMock) -> dict[str, object]:
    """Return ``sys.modules`` entries for running subscriber code without a broker."""
    nats_module = MagicMock()
    nats_module.connect = connect
    return {"nats": nats_module}


def test_network_partition_retries_opens_breaker_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient network partition retries, opens the breaker, then recovers."""
    clock = {"now": 0.0}
    monkeypatch.setattr(
        "hephaestus.resilience.circuit_breaker.time.monotonic",
        lambda: clock["now"],
    )
    monkeypatch.setattr(
        "hephaestus.utils.retry.time",
        SimpleNamespace(sleep=lambda _delay: None),
    )

    name = "fault-injection-network"
    breaker = get_circuit_breaker(name, failure_threshold=2, recovery_timeout=10.0)
    attempts = {"n": 0}

    def partitioned() -> str:
        attempts["n"] += 1
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=["gh", "api", "/rate_limit"],
            stderr="fatal: network is unreachable",
        )

    with pytest.raises(subprocess.CalledProcessError):
        resilient_call(
            partitioned,
            circuit_breaker_name=name,
            max_retries=1,
            initial_delay=0.01,
            max_delay=0.01,
        )

    assert attempts["n"] == 2
    assert breaker.state is CircuitBreakerState.OPEN

    fail_fast_called = False

    def fail_fast_probe() -> str:
        nonlocal fail_fast_called
        fail_fast_called = True
        return "unexpected"

    with pytest.raises(CircuitBreakerOpenError):
        resilient_call(fail_fast_probe, circuit_breaker_name=name, max_retries=0)

    assert fail_fast_called is False

    clock["now"] = 11.0
    assert resilient_call(lambda: "recovered", circuit_breaker_name=name, max_retries=0) == (
        "recovered"
    )
    assert breaker.state is CircuitBreakerState.CLOSED


def test_nats_subscriber_reconnects_after_connect_partition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subscriber retries after a connect partition and dispatches after recovery."""
    received: list[NATSEvent] = []
    broker = _FakeBroker()
    thread = NATSSubscriberThread(
        config=NATSConfig(
            enabled=True,
            subjects=["hi.tasks.>"],
            initial_backoff_seconds=0.01,
            max_backoff_seconds=0.02,
        ),
        handler=received.append,
        admin_factory=lambda _js: (broker, broker),
    )

    msg = _make_msg({"kind": "recovered"})

    async def fetch(batch: int, timeout: float = 0.0) -> list[MagicMock]:
        thread._stop_event.set()
        return [msg]

    nc = _make_connection(AsyncMock(side_effect=fetch))
    attempts = {"n": 0}

    async def connect(_url: str, **_kwargs: object) -> MagicMock:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("network is unreachable")
        return nc

    waited: list[float] = []

    def record_wait(timeout: float | None = None) -> bool:
        assert timeout is not None
        waited.append(timeout)
        return False

    monkeypatch.setattr(thread._stop_event, "wait", record_wait)

    with patch.dict("sys.modules", _install_fake_nats(AsyncMock(side_effect=connect))):
        thread.run()

    assert attempts["n"] == 2
    assert waited == [0.01]
    assert [event.data for event in received] == [{"kind": "recovered"}]
    msg.ack_sync.assert_awaited_once()
    msg.nak.assert_not_awaited()
    nc.drain.assert_awaited_once()
    assert thread.state is SubscriberState.STOPPED


def test_nats_subscriber_naks_and_surfaces_disk_full_handler_failure() -> None:
    """A disk-full handler failure is ledger-recorded, NAKed, and surfaced.

    The durable contract (#2395) redelivers below the processing budget
    instead of discarding the event behind a fire-and-forget ack. The raw
    failure object stays available on the in-process ``last_error``.
    """

    def handler(_event: NATSEvent) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    broker = _FakeBroker()
    thread = NATSSubscriberThread(
        config=NATSConfig(enabled=True, subjects=["hi.tasks.>"]),
        handler=handler,
        admin_factory=lambda _js: (broker, broker),
    )

    msg = _make_msg({"write": "event"})

    async def fetch(batch: int, timeout: float = 0.0) -> list[MagicMock]:
        thread._stop_event.set()
        return [msg]

    nc = _make_connection(AsyncMock(side_effect=fetch))

    with patch.dict("sys.modules", _install_fake_nats(AsyncMock(return_value=nc))):
        asyncio.run(thread._subscribe_loop())

    msg.nak.assert_awaited_once()
    assert msg.nak.await_args.kwargs["delay"] == 1.0
    msg.ack_sync.assert_not_awaited()
    msg.ack.assert_not_awaited()
    msg.term.assert_not_awaited()
    assert isinstance(thread.last_error, OSError)
    assert thread.last_error.errno == errno.ENOSPC
    assert thread.last_message_at is None
    nc.drain.assert_awaited_once()


@pytest.mark.requires_posix
def test_process_kill_is_contained_and_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A killed child is contained in the parent and is not retried as transient."""
    monkeypatch.setattr(
        "hephaestus.utils.retry.time",
        SimpleNamespace(
            sleep=lambda _delay: pytest.fail("non-transient process kill must not sleep/retry")
        ),
    )

    name = "fault-injection-process-kill"
    breaker = get_circuit_breaker(name, failure_threshold=3)
    runs = {"n": 0}

    def killed_child() -> subprocess.CompletedProcess[str]:
        runs["n"] += 1
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        resilient_call(
            killed_child,
            circuit_breaker_name=name,
            max_retries=3,
            initial_delay=0.01,
            max_delay=0.01,
        )

    assert runs["n"] == 1
    assert exc_info.value.returncode == -signal.SIGKILL
    assert breaker.state is CircuitBreakerState.CLOSED
