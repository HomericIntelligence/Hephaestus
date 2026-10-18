"""Tests for hephaestus.nats.failure_store (#2395).

These tests pin the durable failure-resource contract: the four exact stream
topologies, non-destructive provisioning with advisory-subject union,
read-only open mode, idempotent DLQ publication bounded by the exact
envelope limits, advisory parsing, and advisory-to-DLQ reconciliation with
metadata-only records for missing source messages.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from hephaestus.nats.config import NATSConfig
from hephaestus.nats.delivery_ledger import StoredLedger
from hephaestus.nats.failure_models import (
    DLQ_MAX_MSG_SIZE,
    MAX_FAILURE_PAYLOAD_BYTES,
    ConsumerBinding,
    FailureEnvelope,
    sha256_hex,
)
from hephaestus.nats.failure_store import (
    ADVISORY_STREAM,
    FAILURE_STREAM,
    LEDGER_STREAM,
    REPLAY_RECEIPTS_STREAM,
    AdvisoryReconciler,
    DlqOutcome,
    ExistingStream,
    FailureResourceMissingError,
    FailureResources,
    FailureResourceUndersizedError,
    FailureStore,
    ForeignStreamError,
    MaxDeliveryAdvisory,
    PubAckView,
    ReconcileOutcome,
    SequenceConflictError,
    StoredSourceMessage,
    advisory_stream_topology,
    ensure_topologies,
    failure_stream_topology,
    ledger_stream_topology,
    parse_max_delivery_advisory,
    replay_receipts_stream_topology,
)


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


_BINDING_A = ConsumerBinding(subject="hi.tasks.>", durable="durable-a")
_BINDING_B = ConsumerBinding(subject="hi.jobs.>", durable="durable-b")
_SECONDS_30_DAYS = 2_592_000
_GIB = 1_073_741_824
_MIB = 1_048_576


class FakeJetStreamAdmin:
    """In-memory JetStream admin seam recording provisioning and publishes."""

    def __init__(self) -> None:
        self.streams: dict[str, ExistingStream] = {}
        self.added: list[str] = []
        self.updated: list[tuple[str, frozenset[str]]] = []
        self.published: list[tuple[str, bytes, dict[str, str] | None]] = []
        self.messages: dict[tuple[str, int], StoredSourceMessage | None] = {}
        self.subjects: dict[str, StoredLedger] = {}
        self.conflict_next_publish = False
        self.fail_next_publish = False

    async def stream_info(self, name: str) -> ExistingStream | None:
        return self.streams.get(name)

    async def add_stream(self, topology: Any) -> None:
        self.added.append(topology.name)
        self.streams[topology.name] = ExistingStream(
            subjects=frozenset(topology.subjects),
            retention=topology.retention,
            discard=topology.discard,
            storage=topology.storage,
            max_age_seconds=topology.max_age_seconds,
            max_bytes=topology.max_bytes,
            max_msgs_per_subject=topology.max_msgs_per_subject,
            max_msg_size=topology.max_msg_size,
            metadata={"hephaestus.role": topology.role},
        )

    async def update_stream(self, name: str, *, subjects: frozenset[str]) -> None:
        self.updated.append((name, subjects))
        existing = self.streams[name]
        self.streams[name] = ExistingStream(
            subjects=subjects,
            retention=existing.retention,
            discard=existing.discard,
            storage=existing.storage,
            max_age_seconds=existing.max_age_seconds,
            max_bytes=existing.max_bytes,
            max_msgs_per_subject=existing.max_msgs_per_subject,
            max_msg_size=existing.max_msg_size,
            metadata=existing.metadata,
        )

    async def publish(
        self, subject: str, payload: bytes, *, headers: dict[str, str] | None = None
    ) -> PubAckView:
        self.published.append((subject, payload, headers))
        if self.conflict_next_publish:
            self.conflict_next_publish = False
            raise SequenceConflictError("conflicting publication")
        if self.fail_next_publish:
            self.fail_next_publish = False
            raise ConnectionError("broker unavailable")
        return PubAckView(stream=FAILURE_STREAM, sequence=len(self.published), duplicate=False)

    async def get_message(self, stream: str, sequence: int) -> StoredSourceMessage | None:
        return self.messages.get((stream, sequence))

    async def get_last_message(self, stream: str, subject: str) -> StoredLedger | None:
        return self.subjects.get(subject)


def _existing(
    topology: Any,
    *,
    subjects: frozenset[str] | None = None,
    max_bytes: int | None = None,
    max_msg_size: int | None = None,
    max_msgs_per_subject: int | None = None,
    role: str | None = None,
) -> ExistingStream:
    return ExistingStream(
        subjects=frozenset(topology.subjects) if subjects is None else subjects,
        retention="limits",
        discard="old",
        storage="file",
        max_age_seconds=topology.max_age_seconds,
        max_bytes=topology.max_bytes if max_bytes is None else max_bytes,
        max_msgs_per_subject=(
            topology.max_msgs_per_subject if max_msgs_per_subject is None else max_msgs_per_subject
        ),
        max_msg_size=topology.max_msg_size if max_msg_size is None else max_msg_size,
        metadata={"hephaestus.role": topology.role if role is None else role},
    )


def _envelope(payload: bytes = b'{"k": "v"}') -> FailureEnvelope:
    from hephaestus.nats.failure_models import EventIdentity, MessageSnapshot

    snapshot = MessageSnapshot(
        identity=EventIdentity(stream="TASKS", durable="durable-a", stream_sequence=42),
        subject="hi.tasks.demo",
        payload=payload,
        timestamp="",
        delivery_count=3,
    )
    return FailureEnvelope.build(
        snapshot,
        "handler",
        attempt_count=3,
        first_failure_at="2026-01-01T00:00:00Z",
        last_attempt_at="2026-01-01T00:00:05Z",
    )


class TestStreamTopologies:
    """Tests for the four exact stream topology definitions."""

    def test_failure_stream_topology_is_exact(self) -> None:
        topology = failure_stream_topology()
        assert topology.name == "HEPHAESTUS_NATS_FAILURES"
        assert topology.subjects == frozenset({"hephaestus.nats.failure.>"})
        assert topology.retention == "limits"
        assert topology.discard == "old"
        assert topology.storage == "file"
        assert topology.max_age_seconds == _SECONDS_30_DAYS
        assert topology.max_bytes == _GIB
        assert topology.max_msgs_per_subject == 1
        assert topology.max_msg_size == DLQ_MAX_MSG_SIZE
        assert topology.role

    def test_ledger_stream_topology_is_exact(self) -> None:
        topology = ledger_stream_topology()
        assert topology.name == LEDGER_STREAM
        assert topology.subjects == frozenset({"hephaestus.nats.ledger.>"})
        assert topology.discard == "old"
        assert topology.max_age_seconds == _SECONDS_30_DAYS
        assert topology.max_bytes == 256 * _MIB
        assert topology.max_msgs_per_subject == 1
        assert topology.max_msg_size == 16_384

    def test_advisory_stream_topology_derives_exact_subjects(self) -> None:
        topology = advisory_stream_topology([_BINDING_A, _BINDING_B], source_stream="TASKS")
        assert topology.name == ADVISORY_STREAM
        assert topology.subjects == frozenset(
            {
                "$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.TASKS.durable-a",
                "$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.TASKS.durable-b",
            }
        )
        assert topology.discard == "old"
        assert topology.max_age_seconds == _SECONDS_30_DAYS
        assert topology.max_bytes == 256 * _MIB
        assert topology.max_msg_size == 16_384

    def test_advisory_subjects_deduplicate_shared_durables(self) -> None:
        shared = ConsumerBinding(subject="hi.other.>", durable="durable-a")
        topology = advisory_stream_topology([_BINDING_A, shared], source_stream="TASKS")
        assert len(topology.subjects) == 1

    def test_replay_receipts_stream_topology_is_exact(self) -> None:
        topology = replay_receipts_stream_topology()
        assert topology.name == REPLAY_RECEIPTS_STREAM
        assert topology.subjects == frozenset({"hephaestus.nats.replay.receipt.>"})
        assert topology.discard == "old"
        assert topology.max_bytes == 64 * _MIB
        assert topology.max_msgs_per_subject == 1
        assert topology.max_msg_size == 16_384


class TestEnsureTopologies:
    """Tests for non-destructive provisioning and read-only opening."""

    def test_creates_all_four_streams_when_absent(self) -> None:
        admin = FakeJetStreamAdmin()

        _run(
            ensure_topologies(
                admin,
                auto_provision=True,
                bindings=[_BINDING_A],
                source_stream="TASKS",
            )
        )

        assert set(admin.added) == {
            FAILURE_STREAM,
            LEDGER_STREAM,
            ADVISORY_STREAM,
            REPLAY_RECEIPTS_STREAM,
        }
        assert admin.updated == []

    def test_read_only_mode_rejects_a_missing_stream(self) -> None:
        admin = FakeJetStreamAdmin()

        with pytest.raises(FailureResourceMissingError, match=FAILURE_STREAM):
            _run(
                ensure_topologies(
                    admin,
                    auto_provision=False,
                    bindings=[_BINDING_A],
                    source_stream="TASKS",
                )
            )
        assert admin.added == []

    def test_exact_existing_streams_need_no_changes(self) -> None:
        admin = FakeJetStreamAdmin()
        for topology in (
            failure_stream_topology(),
            ledger_stream_topology(),
            advisory_stream_topology([_BINDING_A], source_stream="TASKS"),
            replay_receipts_stream_topology(),
        ):
            admin.streams[topology.name] = _existing(topology)

        _run(
            ensure_topologies(
                admin,
                auto_provision=False,
                bindings=[_BINDING_A],
                source_stream="TASKS",
            )
        )

        assert admin.added == []
        assert admin.updated == []

    def test_a_foreign_stream_is_never_mutated(self) -> None:
        admin = FakeJetStreamAdmin()
        admin.streams[FAILURE_STREAM] = _existing(
            failure_stream_topology(), role="someone-elses-data"
        )
        admin.streams[LEDGER_STREAM] = _existing(ledger_stream_topology())
        admin.streams[ADVISORY_STREAM] = _existing(
            advisory_stream_topology([_BINDING_A], source_stream="TASKS")
        )
        admin.streams[REPLAY_RECEIPTS_STREAM] = _existing(replay_receipts_stream_topology())

        with pytest.raises(ForeignStreamError, match=FAILURE_STREAM):
            _run(
                ensure_topologies(
                    admin,
                    auto_provision=True,
                    bindings=[_BINDING_A],
                    source_stream="TASKS",
                )
            )
        assert admin.added == []
        assert admin.updated == []

    def test_missing_advisory_subjects_are_union_added_without_removal(self) -> None:
        admin = FakeJetStreamAdmin()
        topology = advisory_stream_topology([_BINDING_A], source_stream="TASKS")
        foreign = "foreign.advisory.>"
        admin.streams[FAILURE_STREAM] = _existing(failure_stream_topology())
        admin.streams[LEDGER_STREAM] = _existing(ledger_stream_topology())
        admin.streams[ADVISORY_STREAM] = _existing(topology, subjects=frozenset({foreign}))
        admin.streams[REPLAY_RECEIPTS_STREAM] = _existing(replay_receipts_stream_topology())

        _run(
            ensure_topologies(
                admin,
                auto_provision=True,
                bindings=[_BINDING_A, _BINDING_B],
                source_stream="TASKS",
            )
        )

        assert admin.updated == [
            (
                ADVISORY_STREAM,
                frozenset(topology.subjects | {_BINDING_B_advisory, foreign}),
            )
        ]

    def test_read_only_mode_rejects_a_missing_advisory_subject(self) -> None:
        admin = FakeJetStreamAdmin()
        admin.streams[FAILURE_STREAM] = _existing(failure_stream_topology())
        admin.streams[LEDGER_STREAM] = _existing(ledger_stream_topology())
        admin.streams[ADVISORY_STREAM] = _existing(
            advisory_stream_topology([_BINDING_A], source_stream="TASKS")
        )
        admin.streams[REPLAY_RECEIPTS_STREAM] = _existing(replay_receipts_stream_topology())

        with pytest.raises(FailureResourceUndersizedError, match=ADVISORY_STREAM):
            _run(
                ensure_topologies(
                    admin,
                    auto_provision=False,
                    bindings=[_BINDING_A, _BINDING_B],
                    source_stream="TASKS",
                )
            )

    def test_undersized_limits_are_rejected(self) -> None:
        admin = FakeJetStreamAdmin()
        admin.streams[FAILURE_STREAM] = _existing(failure_stream_topology(), max_msg_size=1024)
        admin.streams[LEDGER_STREAM] = _existing(ledger_stream_topology())
        admin.streams[ADVISORY_STREAM] = _existing(
            advisory_stream_topology([_BINDING_A], source_stream="TASKS")
        )
        admin.streams[REPLAY_RECEIPTS_STREAM] = _existing(replay_receipts_stream_topology())

        with pytest.raises(FailureResourceUndersizedError, match=FAILURE_STREAM):
            _run(
                ensure_topologies(
                    admin,
                    auto_provision=True,
                    bindings=[_BINDING_A],
                    source_stream="TASKS",
                )
            )

    def test_larger_existing_limits_are_never_lowered(self) -> None:
        admin = FakeJetStreamAdmin()
        bigger = failure_stream_topology().max_bytes * 4
        admin.streams[FAILURE_STREAM] = _existing(failure_stream_topology(), max_bytes=bigger)
        admin.streams[LEDGER_STREAM] = _existing(ledger_stream_topology())
        admin.streams[ADVISORY_STREAM] = _existing(
            advisory_stream_topology([_BINDING_A], source_stream="TASKS")
        )
        admin.streams[REPLAY_RECEIPTS_STREAM] = _existing(replay_receipts_stream_topology())

        _run(
            ensure_topologies(
                admin,
                auto_provision=False,
                bindings=[_BINDING_A],
                source_stream="TASKS",
            )
        )
        assert admin.streams[FAILURE_STREAM].max_bytes == bigger
        assert admin.updated == []


_BINDING_B_advisory = "$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.TASKS.durable-b"


class TestFailureStorePublish:
    """Tests for idempotent bounded DLQ publication."""

    def test_publishes_to_the_exact_failure_subject_with_dedup_headers(self) -> None:
        admin = FakeJetStreamAdmin()
        store = FailureStore(admin)

        outcome = _run(store.publish_failure(_envelope()))

        assert outcome is DlqOutcome.PUBLISHED
        assert len(admin.published) == 1
        subject, payload, headers = admin.published[0]
        envelope = _envelope()
        assert subject == f"hephaestus.nats.failure.{envelope.failure_id}"
        assert FailureEnvelope.from_dict(json.loads(payload)) == envelope
        assert headers is not None
        assert headers.get("Nats-Msg-Id") == envelope.failure_id
        assert headers.get("Nats-Expected-Last-Subject-Sequence") == "0"

    def test_a_sequence_conflict_is_an_idempotent_success(self) -> None:
        admin = FakeJetStreamAdmin()
        admin.conflict_next_publish = True
        store = FailureStore(admin)

        outcome = _run(store.publish_failure(_envelope()))

        assert outcome is DlqOutcome.ALREADY_PRESENT

    def test_store_uncertainty_returns_unknown(self) -> None:
        admin = FakeJetStreamAdmin()
        admin.fail_next_publish = True
        store = FailureStore(admin)

        outcome = _run(store.publish_failure(_envelope()))

        assert outcome is DlqOutcome.UNKNOWN

    def test_an_out_of_bounds_envelope_is_rejected_before_publication(self) -> None:
        admin = FakeJetStreamAdmin()
        store = FailureStore(admin)
        envelope = _envelope()
        oversized = FailureEnvelope(**{**envelope.to_dict(), "subject": "s" * 20_000})

        with pytest.raises(ValueError, match="metadata"):
            _run(store.publish_failure(oversized))
        assert admin.published == []


class TestAdvisoryParsing:
    """Tests for max-delivery advisory parsing."""

    def _advisory_payload(self, **overrides: Any) -> bytes:
        body = {
            "type": "io.nats.jetstream.advisory.v1.max_deliver",
            "stream": "TASKS",
            "consumer": "durable-a",
            "stream_seq": 42,
            "deliveries": 6,
            "time": "2026-01-01T00:00:00Z",
        }
        body.update(overrides)
        return json.dumps(body).encode()

    def test_parses_a_valid_advisory(self) -> None:
        advisory = parse_max_delivery_advisory(
            "$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.TASKS.durable-a",
            self._advisory_payload(),
        )
        assert advisory == MaxDeliveryAdvisory(
            stream="TASKS",
            consumer="durable-a",
            stream_seq=42,
            deliveries=6,
            time="2026-01-01T00:00:00Z",
        )

    def test_subject_carries_the_authoritative_identity(self) -> None:
        advisory = parse_max_delivery_advisory(
            "$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.TASKS.durable-a",
            self._advisory_payload(stream="OTHER", consumer="else"),
        )
        assert advisory.stream == "TASKS"
        assert advisory.consumer == "durable-a"

    def test_rejects_a_non_advisory_subject(self) -> None:
        with pytest.raises(ValueError):
            parse_max_delivery_advisory(
                "$JS.EVENT.ADVISORY.CONSUMER.OTHER.TASKS.durable-a",
                self._advisory_payload(),
            )

    def test_rejects_malformed_json(self) -> None:
        with pytest.raises(ValueError):
            parse_max_delivery_advisory(
                "$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.TASKS.durable-a", b"{corrupt"
            )

    @pytest.mark.parametrize("stream_seq", [0, -1, None])
    def test_rejects_an_invalid_stream_seq(self, stream_seq: int | None) -> None:
        with pytest.raises(ValueError):
            parse_max_delivery_advisory(
                "$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.TASKS.durable-a",
                self._advisory_payload(stream_seq=stream_seq),
            )

    def test_rejects_a_missing_stream_seq_key(self) -> None:
        body = json.loads(self._advisory_payload())
        del body["stream_seq"]
        with pytest.raises(ValueError):
            parse_max_delivery_advisory(
                "$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.TASKS.durable-a",
                json.dumps(body).encode(),
            )


class TestAdvisoryReconciler:
    """Tests for advisory-to-DLQ reconciliation."""

    def _advisory_payload(
        self,
        stream: str = "TASKS",
        consumer: str = "durable-a",
        stream_seq: int = 42,
        deliveries: int = 6,
    ) -> bytes:
        return json.dumps(
            {
                "type": "io.nats.jetstream.advisory.v1.max_deliver",
                "stream": stream,
                "consumer": consumer,
                "stream_seq": stream_seq,
                "deliveries": deliveries,
                "time": "2026-01-01T00:00:00Z",
            }
        ).encode()

    def _advisory_subject(self, stream: str = "TASKS", consumer: str = "durable-a") -> str:
        return f"$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.{stream}.{consumer}"

    def test_missing_source_message_creates_a_metadata_only_record(self) -> None:
        admin = FakeJetStreamAdmin()
        reconciler = AdvisoryReconciler(admin)

        outcome = _run(
            reconciler.reconcile_advisory(self._advisory_subject(), self._advisory_payload())
        )

        assert outcome is ReconcileOutcome.PUBLISHED
        assert len(admin.published) == 1
        _, payload, _ = admin.published[0]
        envelope = FailureEnvelope.from_dict(json.loads(payload))
        assert envelope.replayable is False
        assert envelope.payload_b64 is None
        assert envelope.failure_kind == "handler_outcome_unknown"
        assert envelope.stream == "TASKS"
        assert envelope.durable == "durable-a"
        assert envelope.stream_sequence == 42

    def test_present_source_message_creates_a_replayable_record(self) -> None:
        admin = FakeJetStreamAdmin()
        admin.messages[("TASKS", 42)] = StoredSourceMessage(
            subject="hi.tasks.demo",
            data=b'{"hello":"world"}',
            headers=None,
            sequence=42,
        )
        reconciler = AdvisoryReconciler(admin)

        outcome = _run(
            reconciler.reconcile_advisory(self._advisory_subject(), self._advisory_payload())
        )

        assert outcome is ReconcileOutcome.PUBLISHED
        _, payload, _ = admin.published[0]
        envelope = FailureEnvelope.from_dict(json.loads(payload))
        assert envelope.replayable is True
        assert envelope.payload_b64 is not None
        assert envelope.subject == "hi.tasks.demo"
        assert envelope.payload_sha256 == sha256_hex(b'{"hello":"world"}')

    def test_oversized_source_payload_falls_back_to_metadata_only(self) -> None:
        admin = FakeJetStreamAdmin()
        admin.messages[("TASKS", 42)] = StoredSourceMessage(
            subject="hi.tasks.demo",
            data=b"x" * (MAX_FAILURE_PAYLOAD_BYTES + 1),
            headers=None,
            sequence=42,
        )
        reconciler = AdvisoryReconciler(admin)

        outcome = _run(
            reconciler.reconcile_advisory(self._advisory_subject(), self._advisory_payload())
        )

        assert outcome is ReconcileOutcome.PUBLISHED
        _, payload, _ = admin.published[0]
        envelope = FailureEnvelope.from_dict(json.loads(payload))
        assert envelope.replayable is False
        assert envelope.payload_b64 is None

    def test_a_malformed_advisory_is_ignored_without_publication(self) -> None:
        admin = FakeJetStreamAdmin()
        reconciler = AdvisoryReconciler(admin)

        outcome = _run(reconciler.reconcile_advisory("wrong.subject", b"{corrupt"))

        assert outcome is ReconcileOutcome.IGNORED
        assert admin.published == []

    def test_a_duplicate_reconciliation_is_idempotent(self) -> None:
        admin = FakeJetStreamAdmin()
        admin.conflict_next_publish = True
        reconciler = AdvisoryReconciler(admin)

        outcome = _run(
            reconciler.reconcile_advisory(self._advisory_subject(), self._advisory_payload())
        )

        assert outcome is ReconcileOutcome.ALREADY_PRESENT

    def test_repeated_advisories_target_one_deterministic_subject(self) -> None:
        admin = FakeJetStreamAdmin()
        reconciler = AdvisoryReconciler(admin)

        _run(reconciler.reconcile_advisory(self._advisory_subject(), self._advisory_payload()))
        _run(reconciler.reconcile_advisory(self._advisory_subject(), self._advisory_payload()))

        subjects = {subject for subject, _, _ in admin.published}
        assert len(subjects) == 1

    def test_start_subscribes_and_reconciles_live_advisories(self) -> None:
        admin = FakeJetStreamAdmin()
        reconciler = AdvisoryReconciler(admin, source_stream="TASKS", bindings=(_BINDING_A,))
        connector = MagicMock()
        subscription = MagicMock()
        subscription.unsubscribe = AsyncMock()
        connector.subscribe = AsyncMock(return_value=subscription)

        async def drive() -> None:
            await reconciler.start(connector)
            callback = connector.subscribe.await_args.kwargs["cb"]
            advisory = MagicMock()
            advisory.subject = self._advisory_subject()
            advisory.data = self._advisory_payload()
            await callback(advisory)
            await reconciler.stop()

        _run(drive())

        connector.subscribe.assert_awaited_once()
        subject_arg = connector.subscribe.await_args.args[0]
        assert subject_arg == self._advisory_subject()
        assert len(admin.published) == 1, "the live advisory reconciles into one record"
        subscription.unsubscribe.assert_awaited_once()

    def test_a_restart_replaces_the_previous_subscriptions(self) -> None:
        admin = FakeJetStreamAdmin()
        reconciler = AdvisoryReconciler(admin, source_stream="TASKS", bindings=(_BINDING_A,))
        first = MagicMock()
        first.unsubscribe = AsyncMock()
        second = MagicMock()
        second.unsubscribe = AsyncMock()
        connector = MagicMock()
        connector.subscribe = AsyncMock(side_effect=[first, second])

        async def drive() -> None:
            await reconciler.start(connector)
            await reconciler.start(connector)
            await reconciler.stop()

        _run(drive())

        assert connector.subscribe.await_count == 2
        first.unsubscribe.assert_awaited_once()
        second.unsubscribe.assert_awaited_once()

    def test_start_without_bindings_subscribes_nothing(self) -> None:
        admin = FakeJetStreamAdmin()
        reconciler = AdvisoryReconciler(admin)
        connector = MagicMock()
        connector.subscribe = AsyncMock()

        async def drive() -> None:
            await reconciler.start(connector)
            await reconciler.stop()

        _run(drive())

        connector.subscribe.assert_not_awaited()


class TestFailureResourcesPrepare:
    """Tests for the provisioning entry point."""

    def test_prepare_provisions_and_wires_the_stores(self) -> None:
        admin = FakeJetStreamAdmin()
        config = NATSConfig(enabled=True, subjects=["hi.tasks.>"])

        resources = _run(FailureResources.prepare(admin, config, [_BINDING_A]))

        assert set(admin.added) == {
            FAILURE_STREAM,
            LEDGER_STREAM,
            ADVISORY_STREAM,
            REPLAY_RECEIPTS_STREAM,
        }
        assert resources.ledger is not None
        assert resources.store is not None
        assert resources.advisory_reconciler is not None
