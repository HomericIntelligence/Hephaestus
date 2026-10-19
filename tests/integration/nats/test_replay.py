"""Real-broker idempotent replay behavior (#2395).

A poisoned message dead-letters; a restarted replay service then proves one
replay publication per request id, restart-safe dedup of completed
receipts, and exactly one handler invocation per replayed publication.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hephaestus.nats.events import NATSEvent
from hephaestus.nats.failure_store import FAILURE_STREAM, NatsJetStreamAdmin
from hephaestus.nats.replay import FailureReplayer, ReplayOutcome
from hephaestus.nats.subscriber import NATSSubscriberThread
from tests.integration.nats import harness
from tests.integration.nats.conftest import NatsServerHandle, unique_name

pytestmark = pytest.mark.integration

_DURABLE = "hephaestus-subscriber"


def test_replay_survives_restarts_with_exactly_once_publication(
    nats_server: NatsServerHandle,
) -> None:
    """Completed receipts dedup replays across restarts per request id."""
    stream = unique_name("TASKS_REPLAY")
    subjects = ["hi.tasks.replay.>"]
    subject = "hi.tasks.replay.demo"
    url = nats_server.url

    harness.run(_create_stream(url, stream, subjects))

    poison_payload = json.dumps({"poison": True}).encode()
    failures = {"n": 0}

    def failing_handler(_event: NATSEvent) -> None:
        failures["n"] += 1
        raise RuntimeError("permanent failure")

    config = harness.subscriber_config(url, stream, subjects)
    thread = NATSSubscriberThread(config=config, handler=failing_handler)
    thread.start()
    try:

        async def _poison_to_dlq() -> Any | None:
            nc = await harness.connect(url)
            try:
                await harness.publish(nc, subject, poison_payload)
                return await harness.wait_last_msg(
                    nc,
                    FAILURE_STREAM,
                    harness.event_failure_subject(stream, _DURABLE, 1),
                    timeout=30.0,
                )
            finally:
                await nc.drain()

        dlq_record = harness.run(_poison_to_dlq())
    finally:
        thread.stop(timeout=15)

    assert not thread.is_alive()
    assert dlq_record is not None, "the poison message dead-lettered"
    assert failures["n"] == 3
    failure_id = json.loads(dlq_record.data.decode())["failure_id"]

    async def _replay(request_id: str) -> Any:
        nc = await harness.connect(url)
        try:
            # A fresh connection and replayer act as the restarted service.
            replayer = FailureReplayer(NatsJetStreamAdmin(nc.jetstream()))
            return await replayer.replay(failure_id, request_id)
        finally:
            await nc.drain()

    first = harness.run(_replay("req-1"))
    assert first.outcome is ReplayOutcome.REPLAYED
    assert first.puback is not None

    second = harness.run(_replay("req-1"))
    assert second.outcome is ReplayOutcome.ALREADY_REPLAYED
    assert second.puback is not None
    assert (second.puback.stream, second.puback.sequence) == (
        first.puback.stream,
        first.puback.sequence,
    ), "the completed receipt echoes the original PubAck"

    assert harness.run(_source_message_count(url, stream)) == 2, (
        "the repeated request published nothing new"
    )

    received: list[NATSEvent] = []
    accepting = NATSSubscriberThread(config=config, handler=received.append)
    accepting.start()
    try:
        assert harness.wait_for(lambda: len(received) >= 1, timeout=30.0)

        third = harness.run(_replay("req-1"))
        assert third.outcome is ReplayOutcome.ALREADY_REPLAYED

        fourth = harness.run(_replay("req-2"))
        assert fourth.outcome is ReplayOutcome.REPLAYED
        assert harness.wait_for(lambda: len(received) >= 2, timeout=30.0)
    finally:
        accepting.stop(timeout=15)

    assert not accepting.is_alive()
    assert [event.data for event in received] == [{"poison": True}, {"poison": True}], (
        "each replayed publication invokes the handler exactly once"
    )
    assert harness.run(_source_message_count(url, stream)) == 3, (
        "one poison plus two distinct-request replays"
    )


async def _create_stream(url: str, stream: str, subjects: list[str]) -> None:
    nc = await harness.connect(url)
    try:
        await harness.create_source_stream(nc, stream, subjects)
    finally:
        await nc.drain()


async def _source_message_count(url: str, stream: str) -> int:
    nc = await harness.connect(url)
    try:
        info = await nc.jetstream().stream_info(stream)
        return int(info.state.messages)
    finally:
        await nc.drain()
