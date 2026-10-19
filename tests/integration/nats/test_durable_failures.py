"""Real-broker durable failure behavior (#2395).

These tests run the production subscriber thread against a real nats-server
and prove the end-to-end contract: transient recovery, decode/handoff
dead-letter retention, full-size payload retention, poison-loop
termination, max-delivery advisory capture, and restart handoff.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import pytest

from hephaestus.nats.consumer import resolve_bindings
from hephaestus.nats.events import NATSEvent
from hephaestus.nats.failure_models import sha256_hex
from hephaestus.nats.failure_store import (
    FAILURE_STREAM,
    LEDGER_STREAM,
    FailureResources,
    NatsJetStreamAdmin,
)
from hephaestus.nats.subscriber import NATSSubscriberThread
from tests.integration.nats import harness
from tests.integration.nats.conftest import NatsServerHandle, unique_name

pytestmark = pytest.mark.integration

_DURABLE = "hephaestus-subscriber"


def _setup_source_stream(url: str, stream: str, subjects: list[str]) -> None:
    async def _setup() -> None:
        nc = await harness.connect(url)
        try:
            await harness.create_source_stream(nc, stream, subjects)
        finally:
            await nc.drain()

    harness.run(_setup())


def test_transient_handler_failure_recovers_exactly_once(nats_server: NatsServerHandle) -> None:
    """A first-attempt failure NAKs and the redelivery completes."""
    stream = unique_name("TASKS_TRANSIENT")
    subjects = ["hi.tasks.transient.>"]
    subject = "hi.tasks.transient.demo"
    url = nats_server.url
    _setup_source_stream(url, stream, subjects)

    invocations: list[str] = []

    def handler(event: NATSEvent) -> None:
        invocations.append(event.subject)
        if len(invocations) == 1:
            raise RuntimeError("transient failure")

    thread = NATSSubscriberThread(
        config=harness.subscriber_config(url, stream, subjects),
        handler=handler,
    )
    thread.start()
    try:

        async def _publish_and_wait_for_acked() -> dict[str, Any] | None:
            nc = await harness.connect(url)
            try:
                await harness.publish(nc, subject, json.dumps({"n": 1}).encode())
                js = nc.jetstream()
                ledger_subject = harness.event_ledger_subject(stream, _DURABLE, 1)
                deadline = time.monotonic() + 30.0
                from nats.js.errors import APIError

                while time.monotonic() < deadline:
                    try:
                        record_msg = await js.get_last_msg(LEDGER_STREAM, ledger_subject)
                        record: dict[str, Any] = json.loads(record_msg.data.decode())
                        if record.get("state") == "acked":
                            return record
                    except APIError:
                        pass
                    await asyncio.sleep(0.2)
                return None
            finally:
                await nc.drain()

        ledger_record = harness.run(_publish_and_wait_for_acked())
    finally:
        thread.stop(timeout=15)

    assert not thread.is_alive()
    assert invocations == [subject, subject], "exactly one transient retry"
    assert ledger_record is not None, "the ledger reached the acked state"
    assert ledger_record["state"] == "acked"


def test_decode_failure_dead_letters_with_the_raw_payload(nats_server: NatsServerHandle) -> None:
    """An undecodable payload hands off into the DLQ without handler dispatch."""
    stream = unique_name("TASKS_DECODE")
    subjects = ["hi.tasks.decode.>"]
    subject = "hi.tasks.decode.demo"
    url = nats_server.url
    _setup_source_stream(url, stream, subjects)

    invocations: list[str] = []

    def handler(event: NATSEvent) -> None:
        invocations.append(event.subject)

    thread = NATSSubscriberThread(
        config=harness.subscriber_config(url, stream, subjects),
        handler=handler,
    )
    thread.start()
    try:

        async def _publish_and_wait_for_dlq() -> Any | None:
            nc = await harness.connect(url)
            try:
                await harness.publish(nc, subject, b"\xff\xd8\xff")
                return await harness.wait_last_msg(
                    nc,
                    FAILURE_STREAM,
                    harness.event_failure_subject(stream, _DURABLE, 1),
                    timeout=30.0,
                )
            finally:
                await nc.drain()

        dlq_record = harness.run(_publish_and_wait_for_dlq())
    finally:
        thread.stop(timeout=15)

    assert not thread.is_alive()
    assert invocations == [], "a decode failure never reaches the handler"
    assert dlq_record is not None, "the dead-letter record exists"
    envelope = json.loads(dlq_record.data.decode())
    assert envelope["failure_kind"] == "decode"
    assert envelope["replayable"] is True
    assert base64.b64decode(envelope["payload_b64"]) == b"\xff\xd8\xff"


def test_full_size_payload_survives_dead_letter_retention(nats_server: NatsServerHandle) -> None:
    """A payload at the exact 1 MiB capture bound is retained replayable."""
    stream = unique_name("TASKS_FULLSIZE")
    subjects = ["hi.tasks.fullsize.>"]
    subject = "hi.tasks.fullsize.demo"
    url = nats_server.url
    _setup_source_stream(url, stream, subjects)

    blob = b'{"blob":"' + b"x" * (1_048_576 - len('{"blob":""}')) + b'"}'
    assert len(blob) == 1_048_576

    invocations = {"n": 0}

    def handler(_event: NATSEvent) -> None:
        invocations["n"] += 1
        raise RuntimeError("permanent failure")

    thread = NATSSubscriberThread(
        config=harness.subscriber_config(url, stream, subjects),
        handler=handler,
    )
    thread.start()
    try:

        async def _publish_and_wait_for_dlq() -> Any | None:
            nc = await harness.connect(url)
            try:
                await harness.publish(nc, subject, blob)
                return await harness.wait_last_msg(
                    nc,
                    FAILURE_STREAM,
                    harness.event_failure_subject(stream, _DURABLE, 1),
                    timeout=40.0,
                )
            finally:
                await nc.drain()

        dlq_record = harness.run(_publish_and_wait_for_dlq())
    finally:
        thread.stop(timeout=15)

    assert not thread.is_alive()
    assert invocations["n"] == 3, "the handler runs the full processing budget"
    assert dlq_record is not None
    envelope = json.loads(dlq_record.data.decode())
    assert envelope["failure_kind"] == "handler"
    assert envelope["replayable"] is True
    assert envelope["payload_length"] == 1_048_576
    assert envelope["payload_sha256"] == sha256_hex(blob)
    assert base64.b64decode(envelope["payload_b64"]) == blob


def test_poison_message_terminates_without_a_redelivery_loop(
    nats_server: NatsServerHandle,
) -> None:
    """A permanently failing message stops at the budget with no broker loop."""
    stream = unique_name("TASKS_POISON")
    subjects = ["hi.tasks.poison.>"]
    subject = "hi.tasks.poison.demo"
    url = nats_server.url
    _setup_source_stream(url, stream, subjects)

    invocations = {"n": 0}

    def handler(_event: NATSEvent) -> None:
        invocations["n"] += 1
        raise RuntimeError("permanent failure")

    config = harness.subscriber_config(url, stream, subjects)
    thread = NATSSubscriberThread(config=config, handler=handler)
    thread.start()
    try:

        async def _publish_and_wait_for_dlq() -> Any | None:
            nc = await harness.connect(url)
            try:
                await harness.publish(nc, subject, json.dumps({"poison": True}).encode())
                return await harness.wait_last_msg(
                    nc,
                    FAILURE_STREAM,
                    harness.event_failure_subject(stream, _DURABLE, 1),
                    timeout=30.0,
                )
            finally:
                await nc.drain()

        dlq_record = harness.run(_publish_and_wait_for_dlq())
        assert dlq_record is not None, "the poison message handed off"
        assert invocations["n"] == 3

        # Past the configured ack_wait the broker would have redelivered an
        # unacknowledged message; a terminated delivery never returns.
        time.sleep(config.ack_wait_seconds + 1.5)
        assert invocations["n"] == 3, "term() stopped the broker redelivery loop"
    finally:
        thread.stop(timeout=15)

    assert not thread.is_alive()


def test_max_delivery_advisory_recovers_undisposed_messages(
    nats_server: NatsServerHandle,
) -> None:
    """Undisposed deliveries hand off through the max-delivery advisory.

    A message that receives no client disposition (the snapshot-abstain
    class) redelivers up to the broker's own ``max_deliver``; the advisory
    reconciler turns the exact advisory ``stream_seq`` into a durable
    dead-letter record.
    """
    stream = unique_name("TASKS_ADVISORY")
    durable = unique_name("adv")
    subjects = ["hi.tasks.advisory.>"]
    subject = "hi.tasks.advisory.demo"
    url = nats_server.url

    async def _scenario() -> tuple[int, Any | None]:
        from nats.js.api import AckPolicy, ConsumerConfig

        nc = await harness.connect(url)
        try:
            await harness.create_source_stream(nc, stream, subjects)
            js = nc.jetstream()
            config = harness.subscriber_config(url, stream, subjects, durable=durable)
            bindings = resolve_bindings(config)
            resources = await FailureResources.prepare(NatsJetStreamAdmin(js), config, bindings)
            await js.add_consumer(
                stream,
                config=ConsumerConfig(
                    durable_name=durable,
                    filter_subject=subjects[0],
                    ack_policy=AckPolicy.EXPLICIT,
                    ack_wait=1.0,
                    max_deliver=2,
                ),
            )
            await harness.publish(nc, subject, json.dumps({"lost": True}).encode())
            await resources.advisory_reconciler.start(nc)
            try:
                sub = await js.pull_subscribe(subjects[0], durable=durable, stream=stream)
                deliveries = 0
                record = None
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline and record is None:
                    try:
                        fetched = await sub.fetch(1, timeout=1.0)
                        deliveries += len(fetched)  # never dispose of anything
                    except Exception:
                        pass
                    record = await harness.wait_last_msg(
                        nc,
                        FAILURE_STREAM,
                        harness.event_failure_subject(stream, durable, 1),
                        timeout=0.5,
                    )
                return deliveries, record
            finally:
                await resources.advisory_reconciler.stop()
        finally:
            await nc.drain()

    deliveries, dlq_record = harness.run(_scenario())

    assert deliveries == 2, "the broker stops at its own max_deliver"
    assert dlq_record is not None, "the advisory reconciler published a record"
    envelope = json.loads(dlq_record.data.decode())
    assert envelope["failure_kind"] == "handler_outcome_unknown"
    assert envelope["stream_sequence"] == 1
    assert envelope["replayable"] is True
    assert base64.b64decode(envelope["payload_b64"]) == json.dumps({"lost": True}).encode()


def test_restart_resumes_budget_and_hands_off_without_duplicates(
    nats_server: NatsServerHandle,
) -> None:
    """A crashed subscriber's budget resumes on restart; the handoff stays exact."""
    stream = unique_name("TASKS_RESTART")
    subjects = ["hi.tasks.restart.>"]
    subject = "hi.tasks.restart.demo"
    url = nats_server.url
    _setup_source_stream(url, stream, subjects)

    invocations = {"n": 0}

    def handler(_event: NATSEvent) -> None:
        invocations["n"] += 1
        raise RuntimeError("permanent failure")

    config = harness.subscriber_config(url, stream, subjects)

    thread_a = NATSSubscriberThread(config=config, handler=handler)
    thread_a.start()
    try:

        async def _publish() -> None:
            nc = await harness.connect(url)
            try:
                await harness.publish(nc, subject, json.dumps({"boom": True}).encode())
            finally:
                await nc.drain()

        harness.run(_publish())
        assert harness.wait_for(lambda: invocations["n"] >= 1, timeout=20.0)
    finally:
        thread_a.stop(timeout=15)
    assert not thread_a.is_alive()

    # The restarted subscriber resumes the ledger budget: the remaining
    # attempts plus the handoff complete across the process boundary.
    thread_b = NATSSubscriberThread(config=config, handler=handler)
    thread_b.start()
    try:

        async def _wait_for_dlq() -> Any | None:
            nc = await harness.connect(url)
            try:
                return await harness.wait_last_msg(
                    nc,
                    FAILURE_STREAM,
                    harness.event_failure_subject(stream, _DURABLE, 1),
                    timeout=30.0,
                )
            finally:
                await nc.drain()

        dlq_record = harness.run(_wait_for_dlq())
    finally:
        thread_b.stop(timeout=15)

    assert not thread_b.is_alive()
    assert dlq_record is not None, "the failure handed off after the restart"
    assert invocations["n"] == 3, "the ledger budget is shared across restarts"
    envelope = json.loads(dlq_record.data.decode())
    assert envelope["attempt_count"] == 3
    assert envelope["failure_kind"] == "handler"
