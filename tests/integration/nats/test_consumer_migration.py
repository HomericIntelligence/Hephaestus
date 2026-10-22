"""Real-broker consumer migration behavior (#2395).

An incompatible ``AckPolicy.NONE`` durable fails closed on startup by
default and migrates only under explicit opt-in; the subscriber's
production call path then binds and receives the retained backlog.
"""

from __future__ import annotations

import json

import pytest

from hephaestus.nats.consumer import (
    ConsumerManager,
    ConsumerMigrationRequiredError,
    NatsConsumerAdmin,
    resolve_bindings,
)
from hephaestus.nats.events import NATSEvent
from hephaestus.nats.subscriber import NATSSubscriberThread
from tests.integration.nats import harness
from tests.integration.nats.conftest import NatsServerHandle, unique_name

pytestmark = pytest.mark.integration


def test_incompatible_consumer_fails_closed_then_migrates_on_opt_in(
    nats_server: NatsServerHandle,
) -> None:
    """Default startup refuses an incompatible durable; opt-in migrates it."""
    stream = unique_name("TASKS_MIGRATION")
    durable = unique_name("migration")
    subjects = ["hi.migration.>"]
    subject = "hi.migration.demo"
    url = nats_server.url

    async def _seed_incompatible_consumer() -> None:
        from nats.js.api import AckPolicy, ConsumerConfig

        nc = await harness.connect(url)
        try:
            await harness.create_source_stream(nc, stream, subjects)
            js = nc.jetstream()
            await js.add_consumer(
                stream,
                config=ConsumerConfig(
                    durable_name=durable,
                    filter_subject=subjects[0],
                    ack_policy=AckPolicy.NONE,
                ),
            )
            for index in range(3):
                await harness.publish(nc, subject, json.dumps({"i": index}).encode())
        finally:
            await nc.drain()

    harness.run(_seed_incompatible_consumer())
    binding = resolve_bindings(harness.subscriber_config(url, stream, subjects, durable=durable))[0]

    async def _default_startup_leaves_consumer_untouched() -> None:
        from nats.js.api import AckPolicy

        nc = await harness.connect(url)
        try:
            js = nc.jetstream()
            manager = ConsumerManager(
                NatsConsumerAdmin(js),
                harness.subscriber_config(url, stream, subjects, durable=durable),
            )
            with pytest.raises(ConsumerMigrationRequiredError):
                await manager.ensure(binding)
            info = await js.consumer_info(stream, durable)
            assert info.config.ack_policy == AckPolicy.NONE, "default startup must not mutate"
        finally:
            await nc.drain()

    harness.run(_default_startup_leaves_consumer_untouched())

    async def _opted_in_migration_recuts_the_consumer() -> None:
        from nats.js.api import AckPolicy, DeliverPolicy

        nc = await harness.connect(url)
        try:
            js = nc.jetstream()
            config = harness.subscriber_config(
                url,
                stream,
                subjects,
                durable=durable,
                migrate_incompatible_consumers=True,
            )
            manager = ConsumerManager(NatsConsumerAdmin(js), config)
            await manager.ensure(binding)
            info = await js.consumer_info(stream, durable)
            migrated = info.config
            assert migrated.ack_policy == AckPolicy.EXPLICIT
            assert migrated.deliver_policy == DeliverPolicy.BY_START_SEQUENCE
            assert migrated.opt_start_seq == 1, "an unacked NONE consumer resumes from seq 1"
            assert migrated.max_deliver == 6
        finally:
            await nc.drain()

    harness.run(_opted_in_migration_recuts_the_consumer())

    received: list[NATSEvent] = []
    thread = NATSSubscriberThread(
        config=harness.subscriber_config(
            url,
            stream,
            subjects,
            durable=durable,
            migrate_incompatible_consumers=True,
        ),
        handler=received.append,
    )
    thread.start()
    try:
        assert harness.wait_for(lambda: len(received) >= 3, timeout=30.0), (
            "the production call path binds the migrated consumer "
            f"(state={thread.state}, last_error={thread.last_error!r})"
        )
    finally:
        thread.stop(timeout=15)

    assert not thread.is_alive()
    assert [event.data for event in received] == [{"i": 0}, {"i": 1}, {"i": 2}]
