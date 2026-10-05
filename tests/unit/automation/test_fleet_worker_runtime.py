"""Observe the concrete local factory with the real SDK and protocol provider."""

from __future__ import annotations

import asyncio
import fcntl
import os
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import agamemnon_client
import pytest

from hephaestus.automation.fleet_journal import WorkerJournal
from hephaestus.automation.fleet_worker_runtime import FleetWorkerRuntime
from tests.unit.automation.test_fleet_build_service import BuildConsumerHTTP
from tests.unit.automation.test_fleet_worker import FIXTURE

pytestmark = pytest.mark.precommit


@pytest.fixture
def runtime_case(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[FleetWorkerRuntime, BuildConsumerHTTP]]:
    """Use the production storage checks and a finite loopback HTTP fixture."""
    build = Path(__file__).resolve().parents[3] / "build"
    build.mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setenv("AGAMEMNON_API_KEY", "fixture-private-key")
    with tempfile.TemporaryDirectory(prefix="fleet-owner-", dir=build) as temporary:
        root = Path(temporary)
        for name in ("state", "codex", "workspaces"):
            (root / name).mkdir(mode=0o700)
        http = BuildConsumerHTTP()
        runtime = FleetWorkerRuntime(
            controller_port=http.port,
            controller_timeout=5,
            state_dir=root / "state",
            workspace_root=root / "workspaces",
            codex_home=root / "codex",
            worker_id="worker-a",
            pool_id="local",
            host_id="fixture-host",
            capacity=2,
            generation=1,
            provider_command=[sys.executable, "-u", str(FIXTURE)],
        )
        try:
            yield runtime, http
        finally:
            try:
                runtime.close()
            finally:
                http.close()


def writer_held(journal: WorkerJournal) -> bool:
    """Try a distinct real journal owner and close an unexpected acquisition."""
    try:
        contender = WorkerJournal(journal.directory)
    except RuntimeError as error:
        assert "writer" in str(error)
        return True
    contender.close()
    return False


def test_runtime_closes_its_one_sdk_on_the_live_loop_before_releasing_worker_storage(
    runtime_case: tuple[FleetWorkerRuntime, BuildConsumerHTTP], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real client borrows the same journal until its actual close returns."""
    runtime, http = runtime_case
    clients: list[Any] = []
    opened_on: list[asyncio.AbstractEventLoop] = []
    closed_on: list[asyncio.AbstractEventLoop] = []
    real_client = agamemnon_client.AgamemnonClient

    def create(*args: Any, **kwargs: Any) -> Any:
        client = real_client(*args, **kwargs)
        clients.append(client)
        opened_on.append(asyncio.get_running_loop())
        actual_close = client.aclose

        async def close() -> None:
            assert runtime.journal is not None and runtime.worker is not None
            runtime.journal.require_writable()
            assert writer_held(runtime.journal)
            assert runtime.worker.journal is runtime.journal
            assert runtime.worker.provider.process is not None
            assert runtime.worker.provider.process.poll() is None
            closed_on.append(asyncio.get_running_loop())
            await actual_close()

        monkeypatch.setattr(client, "aclose", close)
        return client

    monkeypatch.setattr(agamemnon_client, "AgamemnonClient", create)
    runtime.start()
    assert runtime.worker is not None and runtime.journal is not None
    assert runtime.loop is not None and runtime.loop.is_running()
    runtime.check_health()
    assert len(clients) == 1 and runtime.client is clients[0]
    assert opened_on == [runtime.loop]
    assert runtime.worker.journal is runtime.journal
    assert runtime.journal.snapshot()["runtime_uncertain"] is True
    process = runtime.worker.provider.process
    assert process is not None
    directory = os.open(runtime.journal.directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runtime.close()
        assert closed_on == [runtime.loop]
        assert runtime.loop.is_closed() and not runtime.loop.is_running()
        assert process.poll() is not None
        assert runtime.journal.snapshot()["closed"] is True
        assert runtime.journal.snapshot()["runtime_uncertain"] is False
        assert runtime.journal.snapshot()["runtime_pid"] is None
        assert not writer_held(runtime.journal)
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runtime.close()
        assert closed_on == [runtime.loop]
        assert http.requests == []
    finally:
        os.close(directory)


def test_provider_wait_does_not_stop_real_sdk_or_shared_journal_progress(
    runtime_case: tuple[FleetWorkerRuntime, BuildConsumerHTTP], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The synchronous provider wait must leave the existing SDK loop available."""
    runtime, http = runtime_case
    runtime.start()
    assert runtime.worker is not None and runtime.journal is not None
    assert runtime.loop is not None
    provider = runtime.worker.provider
    provider.request("fixture/freeze", {})
    entered, returned = threading.Event(), threading.Event()
    actual_request = provider.request
    observations: dict[str, Any] = {}

    def request(method: str, params: dict[str, Any], **kwargs: Any) -> Any:
        entered.set()
        try:
            return actual_request(method, params, **kwargs)
        finally:
            returned.set()

    def observe_sdk() -> None:
        try:
            assert entered.wait(1), "the control thread did not enter its provider request"
            assert runtime.journal is not None and runtime.loop is not None
            runtime.journal.append("runtime-owner-observation", {"sdkProgress": True})
            future = asyncio.run_coroutine_threadsafe(
                runtime.client.fleet_build_status(http.record["id"]), runtime.loop
            )
            observations["record"] = future.result(timeout=1)
            observations["before_provider_reply"] = not returned.is_set()
        except BaseException as error:
            observations["error"] = error

    monkeypatch.setattr(provider, "request", request)
    monkeypatch.setenv("AGAMEMNON_API_KEY", "changed-after-runtime-start")
    observer = threading.Thread(target=observe_sdk)
    observer.start()
    try:
        provider.request("fixture/environment", {"names": []})
    finally:
        entered.set()
        observer.join(3)
    assert not observer.is_alive(), "the SDK observer did not exit"
    assert "error" not in observations, observations.get("error")
    assert observations["before_provider_reply"] is True
    assert observations["record"] == http.record
    assert len(http.requests) == 1 and http.requests[0]["method"] == "GET"
    headers = {key.lower(): value for key, value in http.requests[0]["headers"].items()}
    assert headers["authorization"] == "Bearer fixture-private-key"
    runtime.check_health()
