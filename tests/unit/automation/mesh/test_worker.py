"""Tests for hephaestus.automation.mesh.worker."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from hephaestus.automation.mesh.config import MeshConfig
from hephaestus.automation.mesh.worker import MeshWorker, RoleResult, TaskContext

CFG = MeshConfig(
    domain="pipeline",
    role="task-agent",
    agent_id="a-1",
    exec_host="hermes",
    heartbeat_seconds=1,
)


class FakePublisher:
    """Records published state events."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, subject: str, payload: dict[str, Any]) -> None:
        self.published.append((subject, payload))


class FakeAgamemnon:
    """Records split/escalate calls."""

    def __init__(self) -> None:
        self.splits: list[tuple[str, list[dict[str, Any]]]] = []

    def split_task(self, task_id: str, subtasks: list[dict[str, Any]]) -> dict[str, Any]:
        self.splits.append((task_id, subtasks))
        return {"created": len(subtasks)}


@dataclass
class FakeMsg:
    """JetStream message double."""

    subject: str = "hi.myrmidon.pipeline.task-agent.task.t-1"
    payload: dict[str, Any] | None = None
    num_delivered: int = 1
    raw: bytes | None = None
    acked: bool = False
    naked: bool = False
    termed: bool = False
    progressed: int = 0
    metadata: Any = field(init=False)

    def __post_init__(self) -> None:
        """Materialize the JetStream-style metadata attribute."""

        class Meta:
            num_delivered = self.num_delivered

        self.metadata = Meta()

    @property
    def data(self) -> bytes:
        if self.raw is not None:
            return self.raw
        return json.dumps(self.payload or {}).encode()

    async def ack(self) -> None:
        self.acked = True

    async def nak(self) -> None:
        self.naked = True

    async def term(self) -> None:
        self.termed = True

    async def in_progress(self) -> None:
        self.progressed += 1


class StubHandler:
    """Handler double returning a canned result (or raising)."""

    def __init__(self, result: RoleResult | None = None, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.contexts: list[TaskContext] = []

    def handle(self, ctx: TaskContext) -> RoleResult:
        self.contexts.append(ctx)
        if self.exc is not None:
            raise self.exc
        assert self.result is not None
        return self.result


def _worker(handler: StubHandler) -> tuple[MeshWorker, FakePublisher, FakeAgamemnon]:
    pub = FakePublisher()
    aga = FakeAgamemnon()
    worker = MeshWorker(CFG, handler, publisher=pub, agamemnon=aga)  # type: ignore[arg-type]
    return worker, pub, aga


def _verbs(pub: FakePublisher) -> list[str]:
    return [s.rsplit(".", 1)[1] for s, _ in pub.published]


class TestHandleMessage:
    """Tests for the claim-loop message handling."""

    def test_success_publishes_started_completed_and_acks(self) -> None:
        handler = StubHandler(RoleResult(ok=True, summary="done", pr={"number": 5}))
        worker, pub, _ = _worker(handler)
        msg = FakeMsg(payload={"issue": 9, "team_id": "team-1"})

        asyncio.run(worker.handle_message(msg))

        assert _verbs(pub) == ["started", "completed"]
        started_subject, started = pub.published[0]
        assert started_subject == "hi.tasks.team-1.t-1.started"
        assert started["agent_id"] == "a-1"
        assert started["exec_host"] == "hermes"
        assert started["attempt"] == 1
        completed = pub.published[1][1]
        assert completed["pr"] == {"number": 5}
        assert msg.acked and not msg.naked and not msg.termed

    def test_retryable_failure_naks(self) -> None:
        handler = StubHandler(
            RoleResult(ok=False, error_kind="Boom", error_message="x", retryable=True)
        )
        worker, pub, _ = _worker(handler)
        msg = FakeMsg(payload={})

        asyncio.run(worker.handle_message(msg))

        assert _verbs(pub) == ["started", "failed"]
        error = pub.published[1][1]["error"]
        assert error == {"kind": "Boom", "message": "x", "retryable": True}
        assert msg.naked and not msg.acked

    def test_non_retryable_failure_terms(self) -> None:
        handler = StubHandler(
            RoleResult(ok=False, error_kind="Bad", error_message="x", retryable=False)
        )
        worker, _, _ = _worker(handler)
        msg = FakeMsg(payload={})

        asyncio.run(worker.handle_message(msg))

        assert msg.termed and not msg.naked

    def test_handler_crash_becomes_retryable_failure(self) -> None:
        handler = StubHandler(exc=RuntimeError("kaboom"))
        worker, pub, _ = _worker(handler)
        msg = FakeMsg(payload={})

        asyncio.run(worker.handle_message(msg))

        error = pub.published[1][1]["error"]
        assert error["kind"] == "RuntimeError"
        assert error["retryable"] is True
        assert msg.naked

    def test_malformed_payload_terms_without_events(self) -> None:
        handler = StubHandler(RoleResult(ok=True))
        worker, pub, _ = _worker(handler)
        msg = FakeMsg(raw=b"not json")

        asyncio.run(worker.handle_message(msg))

        assert msg.termed
        assert pub.published == []
        assert handler.contexts == []

    def test_task_id_from_subject_and_attempt_from_metadata(self) -> None:
        handler = StubHandler(RoleResult(ok=True))
        worker, _pub, _ = _worker(handler)
        msg = FakeMsg(payload={}, num_delivered=2)

        asyncio.run(worker.handle_message(msg))

        ctx = handler.contexts[0]
        assert ctx.task_id == "t-1"  # parsed from the dispatch subject
        assert ctx.team_id == "mesh"  # config default
        assert ctx.attempt == 2
        assert ctx.is_redelivery

    def test_heartbeat_extends_lease_while_handler_runs(self) -> None:
        class SlowHandler:
            def handle(self, ctx: TaskContext) -> RoleResult:
                import time

                time.sleep(2.5)  # > 2 heartbeat intervals (1 s in CFG)
                return RoleResult(ok=True)

        pub = FakePublisher()
        worker = MeshWorker(CFG, SlowHandler(), publisher=pub, agamemnon=FakeAgamemnon())  # type: ignore[arg-type]
        msg = FakeMsg(payload={})

        asyncio.run(worker.handle_message(msg))

        assert msg.progressed >= 1
        assert msg.acked


class TestTaskContext:
    """Tests for context helpers."""

    def _ctx(self, **payload: Any) -> tuple[TaskContext, FakeAgamemnon]:
        aga = FakeAgamemnon()
        ctx = TaskContext(
            config=CFG,
            payload=payload,
            task_id="t-9",
            team_id="mesh",
            attempt=1,
            publisher=FakePublisher(),  # type: ignore[arg-type]
            agamemnon=aga,  # type: ignore[arg-type]
            deadline=0.0,
        )
        return ctx, aga

    def test_overrun_uses_deadline(self) -> None:
        ctx, _ = self._ctx()
        assert ctx.overrun() is True  # deadline in the past
        object.__setattr__(ctx, "deadline", float("inf"))
        assert ctx.overrun() is False

    def test_split_registers_subtasks_and_checkpoints(self) -> None:
        ctx, aga = self._ctx()
        progressed: list[str] = []
        ctx.progress = progressed.append  # type: ignore[method-assign, assignment]

        response = ctx.split([{"title": "remainder", "description": "d"}])

        assert response == {"created": 1}
        assert aga.splits[0][0] == "t-9"
        assert "<!-- hi:checkpoint t-9 -->" in progressed[0]

    def test_progress_without_issue_logs_only(self) -> None:
        ctx, _ = self._ctx()
        # No issue in payload → logging path; must not raise.
        ctx.progress("step done")

    def test_ask_requires_loop(self) -> None:
        ctx, _ = self._ctx()
        import pytest

        with pytest.raises(RuntimeError):
            ctx.ask("q?")


class TestDeliveryAttempt:
    """Tests for metadata fallback."""

    def test_missing_metadata_defaults_to_one(self) -> None:
        class BareMsg:
            metadata = None

        assert MeshWorker._delivery_attempt(BareMsg()) == 1
