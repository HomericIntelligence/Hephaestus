"""Tests for hephaestus.automation.mesh.worker."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
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

    def test_non_object_json_payload_terms_without_events(self) -> None:
        """Valid non-object JSON (`[]`) is poison — term, never crash (#1764 review)."""
        handler = StubHandler(RoleResult(ok=True))
        worker, pub, _ = _worker(handler)
        msg = FakeMsg(raw=b"[1, 2, 3]")

        asyncio.run(worker.handle_message(msg))

        assert msg.termed and not msg.naked and not msg.acked
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


class TestMeshWorkerInit:
    """Tests for worker dependency initialization."""

    def test_default_agamemnon_uses_config_url_and_env_api_key(self) -> None:
        handler = StubHandler(RoleResult(ok=True))
        config = MeshConfig(
            domain="pipeline",
            role="task-agent",
            agent_id="a-1",
            agamemnon_url="http://configured:9000",
        )
        prior = os.environ.get("AGAMEMNON_API_KEY")
        os.environ["AGAMEMNON_API_KEY"] = "secret-key"
        try:
            worker = MeshWorker(config, handler, publisher=FakePublisher())  # type: ignore[arg-type]
        finally:
            if prior is None:
                os.environ.pop("AGAMEMNON_API_KEY", None)
            else:
                os.environ["AGAMEMNON_API_KEY"] = prior

        assert worker.agamemnon._base_url == "http://configured:9000"
        assert worker.agamemnon._api_key == "secret-key"


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


class TestHeartbeatOverrunCheckpoint:
    """#1764 review: the worker must observe the ADR-013 overrun deadline."""

    def test_heartbeat_reports_overrun_exactly_once(self) -> None:
        cfg = MeshConfig(
            domain="pipeline",
            role="task-agent",
            agent_id="a-1",
            exec_host="h",
            heartbeat_seconds=0,
            overrun_seconds=0,
        )
        pub = FakePublisher()
        aga = FakeAgamemnon()
        worker = MeshWorker(cfg, StubHandler(RoleResult(ok=True)), publisher=pub, agamemnon=aga)  # type: ignore[arg-type]
        msg = FakeMsg(payload={})
        ctx = TaskContext(
            config=cfg,
            payload={},
            task_id="t-1",
            team_id="mesh",
            attempt=1,
            publisher=pub,  # type: ignore[arg-type]
            agamemnon=aga,  # type: ignore[arg-type]
            deadline=0.0,  # already past: overrun() is immediately true
        )

        async def run() -> None:
            hb = asyncio.create_task(worker._heartbeat_loop(msg, ctx))
            for _ in range(50):
                await asyncio.sleep(0)
                if pub.published:
                    break
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb

        asyncio.run(run())
        updated = [(s, p) for s, p in pub.published if s.endswith(".updated")]
        assert len(updated) == 1
        assert updated[0][1]["overrun"] is True


class _FakePullSub:
    """Yields one queued message, then TimeoutError until stop is set."""

    def __init__(self, msgs: list[FakeMsg], stop: asyncio.Event) -> None:
        self._msgs = list(msgs)
        self._stop = stop

    async def fetch(self, batch: int, timeout: float = 0) -> list[FakeMsg]:
        if self._msgs:
            return [self._msgs.pop(0)]
        self._stop.set()
        raise TimeoutError


class _FakeJetStream:
    def __init__(self, psub: _FakePullSub) -> None:
        self._psub = psub
        self.pull_subscribe_calls: list[dict[str, Any]] = []

    async def pull_subscribe(self, subject: str, **kwargs: Any) -> _FakePullSub:
        self.pull_subscribe_calls.append({"subject": subject, **kwargs})
        return self._psub


class _FakeNC:
    def __init__(self, js: _FakeJetStream) -> None:
        self._js = js

    def jetstream(self) -> _FakeJetStream:
        return self._js


class TestRunForever:
    """The claim loop: subscribe, fetch, dispatch, tolerate fetch timeouts."""

    def test_consumes_one_message_then_stops_on_timeout(self, monkeypatch: Any) -> None:
        # The test env installs the base package without the [nats] extra, so
        # stub the two nats.js.api names run_forever imports at call time.
        import sys
        import types

        fake_api = types.ModuleType("nats.js.api")
        fake_api.AckPolicy = types.SimpleNamespace(EXPLICIT="explicit")  # type: ignore[attr-defined]

        class _ConsumerConfig:
            def __init__(self, **kwargs: Any) -> None:
                self.__dict__.update(kwargs)

        fake_api.ConsumerConfig = _ConsumerConfig  # type: ignore[attr-defined]
        fake_nats = types.ModuleType("nats")
        fake_js = types.ModuleType("nats.js")
        monkeypatch.setitem(sys.modules, "nats", fake_nats)
        monkeypatch.setitem(sys.modules, "nats.js", fake_js)
        monkeypatch.setitem(sys.modules, "nats.js.api", fake_api)

        handler = StubHandler(RoleResult(ok=True, summary="done"))
        pub = FakePublisher()
        aga = FakeAgamemnon()
        worker = MeshWorker(CFG, handler, publisher=pub, agamemnon=aga)  # type: ignore[arg-type]
        stop = asyncio.Event()
        msg = FakeMsg(payload={"issue": 9})
        js = _FakeJetStream(_FakePullSub([msg], stop))
        pub.connect = lambda: _async_return(_FakeNC(js))  # type: ignore[attr-defined]

        asyncio.run(worker.run_forever(stop=stop))

        assert msg.acked
        assert len(handler.contexts) == 1
        sub = js.pull_subscribe_calls[0]
        assert sub["subject"] == CFG.filter_subject
        assert sub["durable"] == CFG.durable_name
        cfg = sub["config"]
        assert cfg.max_deliver == CFG.max_deliver
        assert cfg.ack_wait == CFG.ack_wait_seconds


def _async_return(value: Any) -> Any:
    async def _coro() -> Any:
        return value

    return _coro()


class TestCliMainSuccess:
    """cli.main happy path: builds worker from env and runs the loop."""

    def test_main_runs_worker_and_returns_zero(
        self, monkeypatch: Any
    ) -> None:
        import pytest  # noqa: F401  # monkeypatch fixture provided by pytest

        from hephaestus.automation.mesh import cli

        monkeypatch.setenv("MESH_DOMAIN", "pipeline")
        monkeypatch.setenv("MESH_ROLE", "component-lead")
        ran: list[bool] = []

        class _StubWorker:
            def __init__(self, config: Any, handler: Any, **kwargs: Any) -> None:
                self.config = config

            async def run_forever(self, stop: Any = None) -> None:
                ran.append(True)

        monkeypatch.setattr(cli, "MeshWorker", _StubWorker)
        assert cli.main([]) == 0
        assert ran == [True]

    def test_main_keyboard_interrupt_is_clean_exit(self, monkeypatch: Any) -> None:
        from hephaestus.automation.mesh import cli

        monkeypatch.setenv("MESH_DOMAIN", "pipeline")
        monkeypatch.setenv("MESH_ROLE", "component-lead")

        class _StubWorker:
            def __init__(self, config: Any, handler: Any, **kwargs: Any) -> None: ...

            async def run_forever(self, stop: Any = None) -> None:
                raise KeyboardInterrupt

        monkeypatch.setattr(cli, "MeshWorker", _StubWorker)
        assert cli.main([]) == 0
