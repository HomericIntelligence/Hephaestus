"""Tests for hephaestus.nats.consumer (#2395).

These tests pin the explicit consumer contract: per-subject binding
resolution, the explicit pull-consumer configuration map, compatible
updates, fail-closed behavior for incompatible admission modes,
bound-consumer rejection, resume-sequence calculation, opt-in
snapshot/delete/recreate migration with restoration on failure, and the
bounded delivery heartbeat.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest

from hephaestus.nats.config import NATSConfig
from hephaestus.nats.consumer import (
    ConsumerInfoView,
    ConsumerManager,
    ConsumerMigrationRequiredError,
    DeliveryHeartbeat,
    ExplicitConsumerConfig,
    explicit_consumer_config,
    resolve_bindings,
)
from hephaestus.nats.failure_models import ConsumerBinding


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


_BINDING = ConsumerBinding(subject="hi.tasks.>", durable="durable-a")
_SNAPSHOT = {"durable_name": "durable-a", "ack_policy": "none", "raw": "config"}


class FakeConsumerAdmin:
    """In-memory consumer admin seam recording every mutation."""

    def __init__(self, info: ConsumerInfoView | None = None) -> None:
        self.info = info
        self.added: list[tuple[str, ExplicitConsumerConfig]] = []
        self.deleted: list[tuple[str, str]] = []
        self.restored: list[tuple[str, dict[str, Any]]] = []
        self.fail_next_add = False

    async def consumer_info(self, stream: str, durable: str) -> ConsumerInfoView | None:
        return self.info

    async def add_consumer(self, stream: str, config: ExplicitConsumerConfig) -> None:
        if self.fail_next_add:
            self.fail_next_add = False
            raise ConnectionError("recreate failed")
        self.added.append((stream, config))

    async def delete_consumer(self, stream: str, durable: str) -> None:
        self.deleted.append((stream, durable))

    async def restore_consumer(self, stream: str, snapshot: dict[str, Any]) -> None:
        self.restored.append((stream, snapshot))


def _info(
    *,
    ack_policy: str = "explicit",
    push_bound: bool = False,
    ack_floor_seq: int = 0,
    ack_wait: float = 30.0,
    max_deliver: int = 6,
    filter_subject: str = "hi.tasks.>",
) -> ConsumerInfoView:
    return ConsumerInfoView(
        durable_name=_BINDING.durable,
        ack_policy=ack_policy,
        push_bound=push_bound,
        ack_floor_seq=ack_floor_seq,
        ack_wait=ack_wait,
        max_deliver=max_deliver,
        filter_subject=filter_subject,
        snapshot=dict(_SNAPSHOT),
    )


def _config(**overrides: Any) -> NATSConfig:
    args: dict[str, Any] = {"enabled": True, "subjects": ["hi.tasks.>"]}
    args.update(overrides)
    return NATSConfig(**args)


def _manager(admin: FakeConsumerAdmin, **overrides: Any) -> ConsumerManager:
    return ConsumerManager(admin, _config(**overrides))


class TestResolveBindings:
    """Tests for per-subject durable binding resolution."""

    def test_a_single_subject_uses_the_base_durable_name(self) -> None:
        bindings = resolve_bindings(_config(subjects=["hi.tasks.>"]))
        assert bindings == (ConsumerBinding(subject="hi.tasks.>", durable="hephaestus-subscriber"),)

    def test_multiple_subjects_get_indexed_durable_names(self) -> None:
        bindings = resolve_bindings(_config(subjects=["hi.tasks.>", "hi.jobs.>"]))
        assert bindings == (
            ConsumerBinding(subject="hi.tasks.>", durable="hephaestus-subscriber-0"),
            ConsumerBinding(subject="hi.jobs.>", durable="hephaestus-subscriber-1"),
        )


class TestExplicitConsumerConfig:
    """Tests for the explicit consumer configuration map."""

    def test_config_maps_the_bounded_delivery_fields(self) -> None:
        config = _config(ack_wait_seconds=45.0, max_deliveries=7)
        explicit = explicit_consumer_config(_BINDING, config=config)
        assert explicit == ExplicitConsumerConfig(
            durable_name="durable-a",
            filter_subject="hi.tasks.>",
            ack_policy="explicit",
            ack_wait=45.0,
            max_deliver=7,
            replay_policy="instant",
            deliver_policy="all",
            opt_start_seq=None,
        )

    def test_by_start_sequence_requires_a_start_sequence(self) -> None:
        config = _config()
        with pytest.raises(ValueError, match="opt_start_seq"):
            explicit_consumer_config(_BINDING, config=config, deliver_policy="by_start_sequence")

    def test_by_start_sequence_carries_the_sequence(self) -> None:
        config = _config()
        explicit = explicit_consumer_config(
            _BINDING, config=config, deliver_policy="by_start_sequence", opt_start_seq=42
        )
        assert explicit.deliver_policy == "by_start_sequence"
        assert explicit.opt_start_seq == 42


class TestConsumerManagerEnsure:
    """Tests for ConsumerManager.ensure admission and migration."""

    def test_an_absent_consumer_is_created_with_the_explicit_config(self) -> None:
        admin = FakeConsumerAdmin()
        manager = _manager(admin, ack_wait_seconds=30.0, max_deliveries=6)

        _run(manager.ensure(_BINDING))

        assert len(admin.added) == 1
        stream, explicit = admin.added[0]
        assert stream == "TASKS"
        assert explicit.durable_name == "durable-a"
        assert explicit.ack_policy == "explicit"
        assert explicit.max_deliver == 6
        assert admin.deleted == []

    def test_a_compatible_consumer_needs_no_changes(self) -> None:
        admin = FakeConsumerAdmin(_info())
        manager = _manager(admin)

        _run(manager.ensure(_BINDING))

        assert admin.added == []
        assert admin.deleted == []

    def test_a_compatible_consumer_is_updated_in_place(self) -> None:
        admin = FakeConsumerAdmin(_info(ack_wait=99.0, max_deliver=99))
        manager = _manager(admin)

        _run(manager.ensure(_BINDING))

        assert len(admin.added) == 1
        _, explicit = admin.added[0]
        assert explicit.ack_wait == 30.0
        assert explicit.max_deliver == 6
        assert admin.deleted == []

    def test_an_incompatible_admission_mode_fails_closed_by_default(self) -> None:
        admin = FakeConsumerAdmin(_info(ack_policy="none"))
        manager = _manager(admin)

        with pytest.raises(ConsumerMigrationRequiredError, match="durable-a"):
            _run(manager.ensure(_BINDING))
        assert admin.deleted == []

    def test_a_bound_consumer_rejects_the_opt_in_migration(self) -> None:
        admin = FakeConsumerAdmin(_info(ack_policy="none", push_bound=True))
        manager = _manager(admin, migrate_incompatible_consumers=True)

        with pytest.raises(ConsumerMigrationRequiredError, match="still bound"):
            _run(manager.ensure(_BINDING))
        assert admin.deleted == []

    def test_opt_in_migration_resumes_at_ack_floor_plus_one(self) -> None:
        admin = FakeConsumerAdmin(_info(ack_policy="none", ack_floor_seq=41))
        manager = _manager(admin, migrate_incompatible_consumers=True)

        _run(manager.ensure(_BINDING))

        assert admin.deleted == [("TASKS", "durable-a")]
        assert len(admin.added) == 1
        _, explicit = admin.added[0]
        assert explicit.deliver_policy == "by_start_sequence"
        assert explicit.opt_start_seq == 42
        assert explicit.ack_policy == "explicit"
        assert admin.restored == []

    def test_a_recreate_failure_restores_the_original_consumer(self) -> None:
        admin = FakeConsumerAdmin(_info(ack_policy="none", ack_floor_seq=41))
        admin.fail_next_add = True
        manager = _manager(admin, migrate_incompatible_consumers=True)

        with pytest.raises(ConnectionError, match="recreate failed"):
            _run(manager.ensure(_BINDING))
        assert admin.deleted == [("TASKS", "durable-a")]
        assert admin.restored == [("TASKS", _SNAPSHOT)]


class TestDeliveryHeartbeat:
    """Tests for the bounded delivery heartbeat."""

    def test_the_heartbeat_renews_the_lease_until_ack_wait(self) -> None:
        clock = [0.0]
        in_progress_calls = 0
        renewals = 0

        class Msg:
            async def in_progress(self) -> None:
                nonlocal in_progress_calls
                in_progress_calls += 1

        class Ledger:
            async def renew_lease(self, claim: object) -> bool:
                nonlocal renewals
                renewals += 1
                return True

        async def sleep(duration: float) -> None:
            clock[0] += duration

        async def scenario() -> None:
            heartbeat = DeliveryHeartbeat(
                Msg(),
                Ledger(),
                None,  # type: ignore[arg-type]
                interval_seconds=5.0,
                max_duration_seconds=12.0,
                sleep=sleep,
                monotonic=lambda: clock[0],
            )
            async with heartbeat:
                # Real scheduling lets the heartbeat consume its fake clock.
                await asyncio.sleep(0.5)
            return None

        _run(scenario())

        assert in_progress_calls == renewals
        assert in_progress_calls == 3

    def test_exiting_the_context_cancels_the_heartbeat(self) -> None:
        in_progress_calls = 0

        class Msg:
            async def in_progress(self) -> None:
                nonlocal in_progress_calls
                in_progress_calls += 1

        class Ledger:
            async def renew_lease(self, claim: object) -> bool:
                return True

        async def scenario() -> None:
            heartbeat = DeliveryHeartbeat(
                Msg(),
                Ledger(),
                None,  # type: ignore[arg-type]
                interval_seconds=5.0,
                max_duration_seconds=1000.0,
            )
            async with heartbeat:
                await asyncio.sleep(0)

        _run(scenario())

        assert in_progress_calls <= 1

    def test_a_lease_renewal_failure_does_not_stop_the_heartbeat(self) -> None:
        clock = [0.0]
        in_progress_calls = 0
        renewals = 0

        class Msg:
            async def in_progress(self) -> None:
                nonlocal in_progress_calls
                in_progress_calls += 1

        class Ledger:
            async def renew_lease(self, claim: object) -> bool:
                nonlocal renewals
                renewals += 1
                raise ConnectionError("ledger unavailable")

        async def sleep(duration: float) -> None:
            clock[0] += duration

        async def scenario() -> None:
            heartbeat = DeliveryHeartbeat(
                Msg(),
                Ledger(),
                None,  # type: ignore[arg-type]
                interval_seconds=5.0,
                max_duration_seconds=12.0,
                sleep=sleep,
                monotonic=lambda: clock[0],
            )
            async with heartbeat:
                await asyncio.sleep(0.5)

        _run(scenario())

        assert in_progress_calls == renewals
        assert renewals == 3

    def test_in_progress_is_shielded_from_outer_cancellation(self) -> None:
        completed = False

        class Msg:
            async def in_progress(self) -> None:
                nonlocal completed
                await asyncio.sleep(0.05)
                completed = True

        class Ledger:
            async def renew_lease(self, claim: object) -> bool:
                return True

        async def scenario() -> None:
            heartbeat = DeliveryHeartbeat(
                Msg(),
                Ledger(),
                None,  # type: ignore[arg-type]
                interval_seconds=60.0,
                max_duration_seconds=3600.0,
            )
            async with heartbeat:
                await asyncio.sleep(0)
            # The shielded in_progress call completes after cancellation.
            await asyncio.sleep(0.2)
            return None

        _run(scenario())

        assert completed is True
