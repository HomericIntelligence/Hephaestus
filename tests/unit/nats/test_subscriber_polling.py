"""Behavioral tests for the inner polling timeout of _subscribe_loop (#753).

Issue #753: the polling loop previously wrapped ``sub.next_msg(timeout=0.5)``
in a redundant ``asyncio.wait_for(..., timeout=1.0)``. ``next_msg``'s own
``timeout`` argument already governs the full receive, so the outer wrapper
added no ceiling the inner one lacked. These tests pin the runtime behavior:
the loop must poll ``next_msg`` directly, ``continue`` on an inner timeout, and
exit when ``_stop_event`` is set — proving the removed wrapper changed nothing.

The tests drive the real ``_subscribe_loop`` coroutine (via ``asyncio.run``)
with the entire nats-py surface mocked, rather than asserting on the text of
the source via ``inspect.getsource`` (which is brittle to formatting and adds
no coverage of behavior).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hephaestus.nats.config import NATSConfig
from hephaestus.nats.events import NATSEvent
from hephaestus.nats.subscriber import NATSSubscriberThread


class _FakeNatsTimeoutError(asyncio.TimeoutError):
    """Stand-in for the inner timeout ``next_msg`` raises.

    ``next_msg``'s timeout path is built on ``asyncio.wait_for``, which raises
    ``asyncio.TimeoutError``; nats-py re-raises it as ``nats.errors.TimeoutError``
    (a subclass of the *builtin* ``TimeoutError``). On Python 3.10 those two
    bases are DISTINCT classes, so the loop must catch both. Subclassing
    ``asyncio.TimeoutError`` here exercises the ``asyncio`` arm of that dual
    catch; the parametrized ``test_both_timeout_aliases_are_caught`` covers the
    builtin arm. Together they prove removing the redundant ``asyncio.wait_for``
    wrapper (#753) did not drop timeout handling on the project's minimum
    version.
    """


def _make_msg(payload: dict[str, Any]) -> MagicMock:
    """Build a mock JetStream message carrying ``payload`` as JSON."""
    msg = MagicMock()
    msg.data = json.dumps(payload).encode()
    msg.subject = "hi.tasks.demo"
    msg.headers = None
    msg.metadata = None
    msg.ack = AsyncMock()
    return msg


def _install_fake_nats(
    next_msg: AsyncMock,
) -> tuple[MagicMock, MagicMock]:
    """Patch ``nats.connect`` so ``_subscribe_loop`` runs against a fake broker.

    Returns the (connection, subscription) mocks so callers can assert on
    ``next_msg`` call patterns and connection lifecycle.
    """
    sub = MagicMock()
    sub.next_msg = next_msg

    js = MagicMock()
    js.subscribe = AsyncMock(return_value=sub)

    nc = MagicMock()
    nc.jetstream = MagicMock(return_value=js)
    nc.drain = AsyncMock()

    nats_module = MagicMock()
    nats_module.connect = AsyncMock(return_value=nc)
    return nats_module, nc


def _run_loop(thread: NATSSubscriberThread, nats_module: MagicMock) -> None:
    """Run ``_subscribe_loop`` once with the nats-py import patched.

    ``_subscribe_loop`` lazily imports ``nats`` and ``nats.js.api`` inside the
    coroutine, so we patch ``sys.modules`` for the duration of the run.
    """
    deliver_policy_cls = MagicMock()
    deliver_policy_cls.return_value = "new"
    nats_js_api = MagicMock()
    nats_js_api.DeliverPolicy = deliver_policy_cls

    with patch.dict(
        "sys.modules",
        {"nats": nats_module, "nats.js": MagicMock(), "nats.js.api": nats_js_api},
    ):
        asyncio.run(thread._subscribe_loop())


def test_loop_continues_past_inner_timeout_then_dispatches() -> None:
    """A ``next_msg`` timeout is swallowed; the next message is dispatched.

    The loop must ``continue`` on the inner ``TimeoutError`` (not abort), then
    process the subsequent real message — the exact behavior the redundant
    ``asyncio.wait_for`` wrapper was claimed to provide.
    """
    received: list[NATSEvent] = []

    def handler(event: NATSEvent) -> None:
        received.append(event)

    thread = NATSSubscriberThread(
        config=NATSConfig(enabled=True, subjects=["hi.tasks.>"]),
        handler=handler,
    )

    msg = _make_msg({"hello": "world"})

    call_count = {"n": 0}

    async def next_msg(timeout: float = 0.0) -> MagicMock:
        # First poll times out (inner next_msg timeout), second yields a
        # message, then we request shutdown so the loop exits cleanly.
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _FakeNatsTimeoutError
        thread._stop_event.set()
        return msg

    nats_module, nc = _install_fake_nats(AsyncMock(side_effect=next_msg))
    _run_loop(thread, nats_module)

    assert call_count["n"] == 2, "loop must poll again after the inner timeout"
    assert len(received) == 1, "the message after the timeout must be dispatched"
    assert received[0].data == {"hello": "world"}
    msg.ack.assert_awaited_once()
    nc.drain.assert_awaited_once()


def test_loop_exits_when_stop_event_set() -> None:
    """The loop terminates via ``_stop_event`` even under a steady timeout stream.

    With ``next_msg`` perpetually timing out, the only exit path is the
    ``while not self._stop_event.is_set()`` guard. Setting the event from the
    side-effect proves the bounded 0.5s poll tick still services shutdown.
    """
    thread = NATSSubscriberThread(
        config=NATSConfig(enabled=True, subjects=["hi.tasks.>"]),
        handler=MagicMock(),
    )

    async def next_msg(timeout: float = 0.0) -> MagicMock:
        thread._stop_event.set()
        raise _FakeNatsTimeoutError

    nats_module, nc = _install_fake_nats(AsyncMock(side_effect=next_msg))

    # Must return rather than spin forever; a wall-clock guard catches regressions.
    _run_loop(thread, nats_module)

    nc.drain.assert_awaited_once()


def test_next_msg_called_with_bounded_timeout() -> None:
    """``next_msg`` is polled with an explicit, bounded timeout.

    The bounded ``timeout`` on ``next_msg`` is what makes the redundant outer
    ``asyncio.wait_for`` unnecessary: it both caps the receive and serves as
    the poll tick for ``_stop_event``. Assert it is passed (and is finite)
    rather than grepping the source for the literal ``timeout=0.5``.
    """
    thread = NATSSubscriberThread(
        config=NATSConfig(enabled=True, subjects=["hi.tasks.>"]),
        handler=MagicMock(),
    )

    seen_timeout: dict[str, float | None] = {"value": None}

    async def next_msg(timeout: float = 0.0) -> MagicMock:
        seen_timeout["value"] = timeout
        thread._stop_event.set()
        raise _FakeNatsTimeoutError

    nats_module, _ = _install_fake_nats(AsyncMock(side_effect=next_msg))
    _run_loop(thread, nats_module)

    assert seen_timeout["value"] is not None, "next_msg must receive a timeout"
    assert 0 < seen_timeout["value"] < 5, "the poll timeout must be bounded"


@pytest.mark.parametrize("exc_type", [asyncio.TimeoutError, TimeoutError])
def test_both_timeout_aliases_are_caught(exc_type: type[BaseException]) -> None:
    """The handler catches whichever timeout alias ``next_msg`` raises.

    On Python 3.11+ ``asyncio.TimeoutError`` *is* the builtin ``TimeoutError``,
    but on Python 3.10 (the project minimum) they are two DISTINCT classes:
    ``asyncio.wait_for`` raises ``asyncio.TimeoutError`` while nats-py's
    ``nats.errors.TimeoutError`` subclasses the builtin ``TimeoutError``. The
    loop's ``except (asyncio.TimeoutError, TimeoutError)`` must therefore catch
    both arms. Driving the real loop with each alias proves the handler swallows
    every timeout the poll can surface across 3.10-3.13, so a future single-name
    narrowing of the except clause is caught here rather than in production.
    """
    thread = NATSSubscriberThread(
        config=NATSConfig(enabled=True, subjects=["hi.tasks.>"]),
        handler=MagicMock(),
    )

    raised = {"done": False}

    async def next_msg(timeout: float = 0.0) -> MagicMock:
        if not raised["done"]:
            raised["done"] = True
            raise exc_type
        thread._stop_event.set()
        raise _FakeNatsTimeoutError

    nats_module, nc = _install_fake_nats(AsyncMock(side_effect=next_msg))

    # If the alias were not caught, _subscribe_loop would propagate it out of
    # asyncio.run; reaching the drain assertion proves it was swallowed.
    _run_loop(thread, nats_module)
    nc.drain.assert_awaited_once()


def test_ack_is_awaited_even_when_handler_raises() -> None:
    """A raising handler must NOT block the ack — at-most-once by design (#1551).

    The message is acked unconditionally so a poison message cannot wedge the
    poll loop on infinite JetStream redelivery. The failure is surfaced via
    ``last_error`` and the success counter (``_record_message`` /
    ``last_message_at``) is left untouched.
    """
    boom = RuntimeError("handler exploded")

    def handler(_event: NATSEvent) -> None:
        raise boom

    thread = NATSSubscriberThread(
        config=NATSConfig(enabled=True, subjects=["hi.tasks.>"]),
        handler=handler,
    )

    msg = _make_msg({"hello": "world"})

    async def next_msg(timeout: float = 0.0) -> MagicMock:
        # Yield exactly one message, then request shutdown so the loop exits.
        thread._stop_event.set()
        return msg

    nats_module, nc = _install_fake_nats(AsyncMock(side_effect=next_msg))
    _run_loop(thread, nats_module)

    msg.ack.assert_awaited_once()  # acked despite the raise
    assert thread.last_error is boom  # failure surfaced, not swallowed
    assert thread.last_message_at is None  # success path NOT taken
    nc.drain.assert_awaited_once()  # clean drain on loop exit
