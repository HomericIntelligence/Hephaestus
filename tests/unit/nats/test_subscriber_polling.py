"""Behavioral tests for the pull-consumer polling loop (#753, #2395).

Issue #753: the loop polls each pull subscription with a bounded
``fetch(batch, timeout=0.5)``, continues on an inner timeout, and exits when
``_stop_event`` is set. Issue #2395 rewired dispatch onto the durable
failure-handling contract: every fetched message runs through
:meth:`NATSSubscriberThread._process_message`, which claims the delivery in
the CAS ledger, acks successes via ``ack_sync``, NAKs known failures below
the processing budget, and terminates exhausted deliveries after a durable
dead-letter publication.

The tests drive the real ``_subscribe_loop`` coroutine (via ``asyncio.run``)
with the nats-py transport mocked and an in-memory admin broker injected
through ``admin_factory``, rather than asserting on the text of the source
via ``inspect.getsource`` (which is brittle to formatting and adds no
coverage of behavior).
"""

from __future__ import annotations

import asyncio
import json
import ssl
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
from hephaestus.nats.subscriber import NATSSubscriberThread


class _FakeNatsTimeoutError(asyncio.TimeoutError):
    """Stand-in for the inner timeout ``fetch`` raises.

    ``fetch``'s timeout path is built on ``asyncio.wait_for``, which raises
    ``asyncio.TimeoutError``; nats-py re-raises it as
    ``nats.errors.FetchTimeoutError`` (a subclass of the *builtin*
    ``TimeoutError``). On Python 3.10 those two bases are DISTINCT classes,
    so the loop must catch both. Subclassing ``asyncio.TimeoutError`` here
    exercises the ``asyncio`` arm of that dual catch; the parametrized
    ``test_both_timeout_aliases_are_caught`` covers the builtin arm.
    """


class _FakeBroker:
    """In-memory JetStream and consumer admin seam for the polling loop.

    Every stream and consumer starts absent, so the loop provisions all four
    failure-resource topologies and creates the explicit pull consumer. The
    publish path applies the ledger CAS contract so claim/completion writes
    behave like the real broker.
    """

    def __init__(self) -> None:
        self.ledger_rows: dict[str, StoredLedger] = {}
        self.streams_added: list[str] = []
        self.consumers_added: list[tuple[str, ExplicitConsumerConfig]] = []

    # JetStreamAdmin ------------------------------------------------------

    async def stream_info(self, name: str) -> ExistingStream | None:
        return None

    async def add_stream(self, topology: StreamTopology) -> None:
        self.streams_added.append(topology.name)

    async def update_stream(self, name: str, *, subjects: frozenset[str]) -> None:
        raise AssertionError("update_stream must not run in the polling harness")

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
        current_seq = 0 if current is None else current.sequence
        if expected is not None and expected != current_seq:
            raise SequenceConflictError(f"stale publish for {subject}")
        sequence = current_seq + 1
        self.ledger_rows[subject] = StoredLedger(sequence=sequence, payload=payload)
        return PubAckView(
            stream="HEPHAESTUS_NATS_DELIVERY_LEDGER", sequence=sequence, duplicate=False
        )

    async def get_message(self, stream: str, sequence: int) -> StoredSourceMessage | None:
        return None

    async def get_last_message(self, stream: str, subject: str) -> StoredLedger | None:
        return self.ledger_rows.get(subject)

    # ConsumerAdmin -------------------------------------------------------

    async def consumer_info(self, stream: str, durable: str) -> None:
        return None

    async def add_consumer(self, stream: str, config: ExplicitConsumerConfig) -> None:
        self.consumers_added.append((stream, config))

    async def delete_consumer(self, stream: str, durable: str) -> None:
        raise AssertionError("delete_consumer must not run in the polling harness")

    async def restore_consumer(self, stream: str, snapshot: dict[str, Any]) -> None:
        raise AssertionError("restore_consumer must not run in the polling harness")


def _make_thread(
    handler: Any,
    broker: _FakeBroker,
    **config_kwargs: Any,
) -> NATSSubscriberThread:
    """Build a subscriber thread wired to the in-memory admin broker."""
    config_kwargs.setdefault("enabled", True)
    config_kwargs.setdefault("subjects", ["hi.tasks.>"])
    return NATSSubscriberThread(
        config=NATSConfig(**config_kwargs),
        handler=handler,
        admin_factory=lambda _js: (broker, broker),
    )


def _make_msg(
    payload: dict[str, Any] | bytes,
    *,
    sequence: int = 1,
    num_delivered: int = 1,
    durable: str = "hephaestus-subscriber",
) -> MagicMock:
    """Build a mock JetStream message with valid durable metadata."""
    msg = MagicMock()
    msg.data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    msg.subject = "hi.tasks.demo"
    msg.headers = None
    metadata = MagicMock()
    metadata.stream = "TASKS"
    metadata.consumer = durable
    metadata.sequence.stream = sequence
    metadata.num_delivered = num_delivered
    msg.metadata = metadata
    msg.ack = AsyncMock()
    msg.ack_sync = AsyncMock()
    msg.nak = AsyncMock()
    msg.term = AsyncMock()
    msg.in_progress = AsyncMock()
    return msg


def _install_fake_nats(
    fetch: AsyncMock,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Patch ``nats.connect`` so ``_subscribe_loop`` runs against fake I/O.

    Returns the (nats module, connection, jetstream) mocks so callers can
    assert on ``fetch`` call patterns, pull-subscribe bindings, and the
    connection lifecycle.
    """
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

    nats_module = MagicMock()
    nats_module.connect = AsyncMock(return_value=nc)
    return nats_module, nc, js


def _run_loop(thread: NATSSubscriberThread, nats_module: MagicMock) -> None:
    """Run ``_subscribe_loop`` once with the nats-py import patched.

    ``_subscribe_loop`` lazily imports ``nats`` inside the coroutine, so we
    patch ``sys.modules`` for the duration of the run.
    """
    with patch.dict("sys.modules", {"nats": nats_module}):
        asyncio.run(thread._subscribe_loop())


def test_loop_continues_past_inner_timeout_then_dispatches() -> None:
    """A ``fetch`` timeout is swallowed; the next message is dispatched.

    The loop must ``continue`` on the inner ``TimeoutError`` (not abort),
    then process the subsequent real message through the durable contract:
    the handler runs and the ack is confirmed via ``ack_sync``.
    """
    broker = _FakeBroker()
    received: list[NATSEvent] = []

    thread = _make_thread(received.append, broker)
    msg = _make_msg({"hello": "world"})

    call_count = {"n": 0}

    async def fetch(batch: int, timeout: float = 0.0) -> list[MagicMock]:
        # First poll times out (inner fetch timeout), second yields a
        # message, then we request shutdown so the loop exits cleanly.
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _FakeNatsTimeoutError
        thread._stop_event.set()
        return [msg]

    nats_module, nc, _ = _install_fake_nats(AsyncMock(side_effect=fetch))
    _run_loop(thread, nats_module)

    assert call_count["n"] == 2, "loop must poll again after the inner timeout"
    assert len(received) == 1, "the message after the timeout must be dispatched"
    assert received[0].data == {"hello": "world"}
    msg.ack_sync.assert_awaited_once()
    msg.ack.assert_not_awaited()
    msg.nak.assert_not_awaited()
    msg.term.assert_not_awaited()
    nc.drain.assert_awaited_once()


def test_loop_exits_when_stop_event_set() -> None:
    """The loop terminates via ``_stop_event`` even under a steady timeout stream.

    With ``fetch`` perpetually timing out, the only exit path is the
    ``while not self._stop_event.is_set()`` guard. Setting the event from
    the side-effect proves the bounded 0.5s poll tick still services
    shutdown.
    """
    broker = _FakeBroker()
    thread = _make_thread(MagicMock(), broker)

    async def fetch(batch: int, timeout: float = 0.0) -> list[MagicMock]:
        thread._stop_event.set()
        raise _FakeNatsTimeoutError

    nats_module, nc, _ = _install_fake_nats(AsyncMock(side_effect=fetch))

    # Must return rather than spin forever; a wall-clock guard catches regressions.
    _run_loop(thread, nats_module)

    nc.drain.assert_awaited_once()


def test_fetch_called_with_bounded_timeout() -> None:
    """``fetch`` is polled with an explicit, bounded timeout.

    The bounded ``timeout`` on ``fetch`` caps the receive and serves as the
    poll tick for ``_stop_event``. Assert it is passed (and is finite)
    rather than grepping the source for the literal ``timeout=0.5``.
    """
    broker = _FakeBroker()
    thread = _make_thread(MagicMock(), broker)

    seen_timeout: dict[str, float | None] = {"value": None}

    async def fetch(batch: int, timeout: float = 0.0) -> list[MagicMock]:
        seen_timeout["value"] = timeout
        thread._stop_event.set()
        raise _FakeNatsTimeoutError

    nats_module, _, _ = _install_fake_nats(AsyncMock(side_effect=fetch))
    _run_loop(thread, nats_module)

    assert seen_timeout["value"] is not None, "fetch must receive a timeout"
    assert 0 < seen_timeout["value"] < 5, "the poll timeout must be bounded"


def test_pull_subscribe_binds_the_resolved_durable_and_provisions() -> None:
    """The loop provisions failure resources and binds the explicit consumer.

    Startup provisions the four failure-resource topologies, creates the
    explicit pull consumer for the resolved binding, and opens one pull
    subscription against the binding's subject, durable, and stream.
    """
    broker = _FakeBroker()
    thread = _make_thread(MagicMock(), broker)

    async def fetch(batch: int, timeout: float = 0.0) -> list[MagicMock]:
        thread._stop_event.set()
        raise _FakeNatsTimeoutError

    nats_module, _, js = _install_fake_nats(AsyncMock(side_effect=fetch))
    _run_loop(thread, nats_module)

    assert len(broker.streams_added) == 4, "the four failure topologies provision"
    assert broker.consumers_added, "the binding consumer must be created"
    _stream, consumer_config = broker.consumers_added[0]
    assert consumer_config.durable_name == "hephaestus-subscriber"
    assert consumer_config.filter_subject == "hi.tasks.>"
    assert consumer_config.ack_policy == "explicit"
    js.pull_subscribe.assert_awaited_once_with(
        "hi.tasks.>",
        durable="hephaestus-subscriber",
        stream="TASKS",
    )


def test_subscribe_loop_passes_tls_options_to_nats_connect() -> None:
    """TLS config is forwarded to nats-py's native connection options."""
    broker = _FakeBroker()
    configured_url = (
        "tls://alice:credential-value@broker.example.com:4222/jetstream?token=query-value"
    )
    thread = _make_thread(
        MagicMock(),
        broker,
        url=configured_url,
        tls=True,
        tls_hostname="broker.example.com",
        tls_handshake_first=True,
    )

    async def fetch(batch: int, timeout: float = 0.0) -> list[MagicMock]:
        thread._stop_event.set()
        raise _FakeNatsTimeoutError

    nats_module, nc, _ = _install_fake_nats(AsyncMock(side_effect=fetch))
    _run_loop(thread, nats_module)

    nats_module.connect.assert_awaited_once()
    args, kwargs = nats_module.connect.await_args
    assert args == (configured_url,)
    assert isinstance(kwargs["tls"], ssl.SSLContext)
    assert kwargs["tls_hostname"] == "broker.example.com"
    assert kwargs["tls_handshake_first"] is True
    nc.drain.assert_awaited_once()


@pytest.mark.parametrize("exc_type", [asyncio.TimeoutError, TimeoutError])
def test_both_timeout_aliases_are_caught(exc_type: type[BaseException]) -> None:
    """The loop catches whichever timeout alias ``fetch`` raises.

    On Python 3.11+ ``asyncio.TimeoutError`` *is* the builtin ``TimeoutError``,
    but on Python 3.10 (the project minimum) they are two DISTINCT classes:
    ``asyncio.wait_for`` raises ``asyncio.TimeoutError`` while nats-py's
    ``nats.errors.FetchTimeoutError`` subclasses the builtin ``TimeoutError``.
    The loop's ``except TimeoutError`` must therefore catch both arms.
    Driving the real loop with each alias proves the handler swallows every
    timeout the poll can surface across 3.10-3.13, so a future narrowing of
    the except clause is caught here rather than in production.
    """
    broker = _FakeBroker()
    thread = _make_thread(MagicMock(), broker)

    raised = {"done": False}

    async def fetch(batch: int, timeout: float = 0.0) -> list[MagicMock]:
        if not raised["done"]:
            raised["done"] = True
            raise exc_type
        thread._stop_event.set()
        raise _FakeNatsTimeoutError

    nats_module, nc, _ = _install_fake_nats(AsyncMock(side_effect=fetch))

    # If the alias were not caught, _subscribe_loop would propagate it out of
    # asyncio.run; reaching the drain assertion proves it was swallowed.
    _run_loop(thread, nats_module)
    nc.drain.assert_awaited_once()


@pytest.mark.parametrize("payload", [b"\xff", b"{not-json"])
def test_malformed_payload_is_recorded_naked_and_not_dispatched(
    payload: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed UTF-8/JSON is ledger-recorded and NAKed, never dispatched.

    The durable contract (#2395) replaces the old fire-and-forget ack: a
    decode failure below the processing budget returns the record to
    ``ready`` and NAKs with the configured backoff so a later delivery can
    succeed after the publisher fixes its payload.
    """
    broker = _FakeBroker()
    handler = MagicMock()
    thread = _make_thread(handler, broker)
    msg = _make_msg(payload)

    async def fetch(batch: int, timeout: float = 0.0) -> list[MagicMock]:
        thread._stop_event.set()
        return [msg]

    caplog.set_level("WARNING", logger="hephaestus.nats.subscriber")
    nats_module, nc, _ = _install_fake_nats(AsyncMock(side_effect=fetch))
    _run_loop(thread, nats_module)

    msg.nak.assert_awaited_once()
    assert msg.nak.await_args.kwargs["delay"] == 1.0
    msg.ack_sync.assert_not_awaited()
    msg.ack.assert_not_awaited()
    msg.term.assert_not_awaited()
    handler.assert_not_called()
    assert thread.last_error is None
    assert thread.last_message_at is None
    assert "Failed to decode message on hi.tasks.demo (seq=1)" in caplog.text
    nc.drain.assert_awaited_once()


def test_handler_failure_is_recorded_naked_and_not_dispatched(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising handler is ledger-recorded and NAKed below the budget.

    The failure is surfaced via ``last_error``, the success counter
    (``_record_message`` / ``last_message_at``) is left untouched, and the
    raw exception text never reaches the logs. The NAK schedules a
    redelivery so the message is not lost (#2395).
    """
    broker = _FakeBroker()
    sensitive_text = "token=known-test-value"
    boom = RuntimeError(f"handler exploded: {sensitive_text}")
    caplog.set_level("ERROR", logger="hephaestus.nats.subscriber")

    def handler(_event: NATSEvent) -> None:
        raise boom

    thread = _make_thread(handler, broker)
    msg = _make_msg({"hello": "world"})

    async def fetch(batch: int, timeout: float = 0.0) -> list[MagicMock]:
        # Yield exactly one message, then request shutdown so the loop exits.
        thread._stop_event.set()
        return [msg]

    nats_module, nc, _ = _install_fake_nats(AsyncMock(side_effect=fetch))
    _run_loop(thread, nats_module)

    msg.nak.assert_awaited_once()
    assert msg.nak.await_args.kwargs["delay"] == 1.0
    msg.ack_sync.assert_not_awaited()
    msg.term.assert_not_awaited()
    assert thread.last_error is boom  # failure surfaced, not swallowed
    assert thread.health_dict()["last_error"] == "handler_error"
    assert thread.last_message_at is None  # success path NOT taken
    assert "kind=handler_error" in caplog.text
    assert sensitive_text not in caplog.text
    nc.drain.assert_awaited_once()  # clean drain on loop exit
