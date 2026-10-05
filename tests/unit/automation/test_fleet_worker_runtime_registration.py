"""Run two late-registered builds through the actual CLI-selected runtime.

Apply as tests/unit/automation/test_fleet_worker_runtime_registration.py.
The real CLI main selects the runtime. Its serial serve callback drives trusted
in-process registration; no socket registration or permissive capability supplier
is added. SDK requests, source snapshots, exclusion, recipe processes, publisher,
collector, and completion receipts are real. Admission and allocation are fixtures.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from queue import Empty
from typing import TYPE_CHECKING, Any, NoReturn, TypedDict

import pytest
from agamemnon_client import AgamemnonClient, AgamemnonConfig

from hephaestus.automation import fleet_worker_cli
from hephaestus.automation.fleet_build_contract import digest
from hephaestus.automation.fleet_build_jobs import ResultHandoff, SourceLease
from hephaestus.automation.fleet_build_service import FleetBuildOwner, FleetBuildService
from hephaestus.automation.fleet_build_supervisor import BuildSnapshot
from hephaestus.automation.fleet_journal import WorkerJournal
from hephaestus.automation.fleet_provider import CodexAppServer
from hephaestus.automation.fleet_worker import FleetWorker
from hephaestus.automation.pipeline import worker_pool
from hephaestus.automation.pipeline.job_results import JobHandle, JobResult
from hephaestus.automation.pipeline.jobs import BuildTestJob
from hephaestus.io.utils import write_secure
from tests.unit.automation.test_fleet_build_jobs import PreparedCase, lock_is_held
from tests.unit.automation.test_fleet_build_service import BuildConsumerHTTP
from tests.unit.automation.test_fleet_worker_runtime_cli import WorkerCLI

if TYPE_CHECKING:
    from hephaestus.automation.fleet_worker_runtime import FleetWorkerRuntime

pytestmark = pytest.mark.precommit
_MODULE = "tests.unit.automation.test_fleet_worker_runtime_registration"

type Json = dict[str, Any]
type ClientBinding = tuple[AgamemnonClient, asyncio.AbstractEventLoop]
type OwnerBinding = tuple[str, AgamemnonClient, WorkerJournal, asyncio.AbstractEventLoop]


class BuildCapabilities(TypedDict):
    """Name the actual capabilities passed to the runtime registration API."""

    repository: str
    source: Path
    submission: Json
    parent: Json
    snapshot: BuildSnapshot
    source_lease: SourceLease
    result_handoff: ResultHandoff


@dataclass
class ReceiptObservation:
    """Retain one actual receipt write and the leases held at that boundary."""

    path: Path
    value: Json
    source_held: bool
    evidence_held: bool


@dataclass
class ResourceObservations:
    """Record only resources created after unused fixture consumers have exited."""

    clients: list[ClientBinding] = field(default_factory=list)
    client_closes: list[ClientBinding] = field(default_factory=list)
    loops: list[asyncio.AbstractEventLoop] = field(default_factory=list)
    journals: list[WorkerJournal] = field(default_factory=list)
    providers: list[CodexAppServer] = field(default_factory=list)
    provider_closes: list[CodexAppServer] = field(default_factory=list)
    owners: list[OwnerBinding] = field(default_factory=list)
    runtimes: list[FleetWorkerRuntime] = field(default_factory=list)
    receipts: list[ReceiptObservation] = field(default_factory=list)


@dataclass
class RegistrationFixture:
    """Keep the two prepared builds and their runtime observations in one scope."""

    cli: WorkerCLI
    first: PreparedCase
    second: PreparedCase
    release: threading.Event
    entered: threading.Event = field(default_factory=threading.Event)
    observed: ResourceObservations = field(default_factory=ResourceObservations)

    @property
    def http(self) -> BuildConsumerHTTP:
        """Return the first case's single retained HTTP owner."""
        http = self.first.build.http
        assert http is not None
        return http

    @property
    def runtime(self) -> FleetWorkerRuntime:
        """Return the one runtime selected by the actual CLI factory."""
        assert len(self.observed.runtimes) == 1
        return self.observed.runtimes[0]

    @contextmanager
    def first_source(self, *, deadline: float, shutdown: threading.Event) -> Iterator[None]:
        """Gate the first job only after its actual source exclusion is held."""
        with self.first.source_lease(deadline=deadline, shutdown=shutdown):
            self.entered.set()
            if not self.release.wait(max(0, deadline - time.monotonic())):
                raise TimeoutError("fixture source gate was not released")
            yield

    def drive(self, worker: FleetWorker, *, check_health: Callable[[], None] | None = None) -> None:
        """Drive the admitted registrations on the actual runtime control thread."""
        runtime = self.runtime
        assert worker is runtime.worker and check_health == runtime.check_health
        assert worker.journal is runtime.journal
        assert self.observed.owners == []
        assert runtime.completions.qsize() == 0
        assert worker.inventory()["sessions"] == []
        job = _register_first(self, runtime, worker)
        handle = _submit_first_at_source_gate(self, runtime, job)
        _take_first_completion(self, runtime, job, handle)
        _retire_first(self, runtime, worker)
        second_capabilities = _run_second(self, runtime, worker)
        _assert_live_resources(self, runtime)
        _assert_retained_intents(self, runtime)
        assert worker.provider.request("fixture/last-request", {"method": "turn/start"}) == {}
        runtime.retire_build("second-context")
        with pytest.raises(ValueError):
            runtime.register_build("second-context", **second_capabilities)


def _close_unused_consumer(case: PreparedCase) -> None:
    """Finish and remove the setup consumer before any runtime resources exist."""
    build = case.build
    try:
        build.call(build._close_consumer())
    finally:
        build.loop.call_soon_threadsafe(build.loop.stop)
        build.loop_thread.join(5)
    assert not build.loop_thread.is_alive()
    assert build.client._client.is_closed
    assert build.journal.snapshot()["closed"] is True
    assert build.http is not None and build.http.requests == []
    build.loop.close()
    assert build.loop.is_closed()
    del build.owner
    del build.client
    del build.journal


def _prepare_second(case: PreparedCase) -> None:
    """Bind only fixture admission inputs to a distinct later source and session."""
    case.stop_publication.set()
    case.publication.join(2)
    assert not case.publication.is_alive() and case.publisher_error is None
    _close_unused_consumer(case)
    publisher = case.build.publisher
    publisher.close_owners()
    command = publisher.command
    payload = command["payload"]
    parent = payload["parent"]
    parent.update(
        targetId="second-session",
        sessionId="second-session",
        executionId="second-execution",
        taskId="second-task",
        agentId="second-agent",
    )
    parent["claim"].update(targetId=parent["targetId"], agentId=parent["agentId"])
    payload["policy"]["workspace"]["id"] = "second-source"
    payload["policyDigest"] = digest(payload["policy"])
    build_id = "build-" + digest(
        {"workspaceId": "second-source", "idempotencyKey": "publisher-source"}
    )
    command.update(
        targetId=build_id,
        commandId=build_id + "-start",
        idempotencyKey=build_id + "-start",
    )
    payload["snapshotWorkspace"] = f"{build_id}-attempt-{payload['attempt']}"
    publisher.open()
    case.build._bind_http()
    assert case.build.http is not None
    case.build.http.close()
    case.build.http = None


def _publish_second_at(case: PreparedCase, offset: int) -> None:
    """Observe only the next actual POST/GET pair on the existing HTTP server."""
    http = case.build.http
    assert http is not None
    deadline = time.monotonic() + 15
    try:
        while not case.stop_publication.wait(0.01):
            if time.monotonic() >= deadline:
                raise TimeoutError("the second build never reached its HTTP publication gate")
            if len(http.requests) < offset + 2:
                continue
            first, second = http.requests[offset : offset + 2]
            assert first["method"] == "POST" and first["path"].endswith("/submit")
            assert second["method"] == "GET"
            assert second["path"].endswith("/" + case.build.publisher.command["targetId"])
            case.build.publish()
            return
    except BaseException as error:
        case.publisher_error = error
    finally:
        case.published.set()


def _session_command(case: PreparedCase, operation: str) -> dict[str, Any]:
    """Use the fixture's actual parent claim as the admitted worker assignment."""
    parent = case.build.publisher.command["payload"]["parent"]
    command_id = parent["sessionId"] + "-" + operation
    return {
        "schema": "hi/fleet/v1",
        "commandId": command_id,
        "idempotencyKey": command_id,
        "targetKind": "sessions",
        "targetId": parent["sessionId"],
        "workerId": parent["claim"]["workerId"],
        "generation": parent["generation"],
        "operation": operation,
        "payload": {
            "workspace": str(case.build.publisher.source),
            "agentId": parent["agentId"],
            "taskId": parent["taskId"],
            "executionId": parent["executionId"],
            "stage": "implementation",
        },
    }


def _capabilities(case: PreparedCase, source_lease: SourceLease) -> BuildCapabilities:
    """Return the copied fixture identity and the actual supplied build capabilities."""
    payload = case.build.publisher.command["payload"]
    return {
        "repository": payload["policy"]["workspace"]["repository"],
        "source": case.build.publisher.source,
        "submission": copy.deepcopy(case.build.submission),
        "parent": copy.deepcopy(payload["parent"]),
        "snapshot": case.build.publisher.snapshot,
        "source_lease": source_lease,
        "result_handoff": case.result_handoff,
    }


@contextmanager
def _prepared_registration(root: Path) -> Iterator[RegistrationFixture]:
    """Close both unused consumers before exposing the prepared runtime fixture."""
    cli = WorkerCLI(root)
    first_root = cli.workspaces / "first"
    second_root = cli.workspaces / "second"
    first_root.mkdir(mode=0o700)
    second_root.mkdir(mode=0o700)
    first = PreparedCase(first_root)
    second: PreparedCase | None = None
    release = threading.Event()
    try:
        _close_unused_consumer(first)
        second = PreparedCase(second_root)
        _prepare_second(second)
        assert first.build.http is not None
        cli.parent_record.write_text(json.dumps(os.getpid()))
        yield RegistrationFixture(cli, first, second, release)
    finally:
        release.set()
        try:
            if second is not None:
                # The first case remains the sole owner of the reused HTTP server.
                second.build.http = None
                second.close()
        finally:
            first.close()


def _observe_sdk(monkeypatch: pytest.MonkeyPatch, observed: ResourceObservations) -> None:
    """Observe real SDK construction and closure on their actual event loops."""
    real_init = AgamemnonClient.__init__
    real_close = AgamemnonClient.aclose

    def client_init(
        client: AgamemnonClient,
        config: AgamemnonConfig | None = None,
        *,
        trust_env: bool = True,
    ) -> None:
        """Construct the actual SDK client before recording its owner loop."""
        real_init(client, config, trust_env=trust_env)
        observed.clients.append((client, asyncio.get_running_loop()))

    async def client_close(client: AgamemnonClient) -> None:
        """Record closure only after the real asynchronous close returns."""
        await real_close(client)
        observed.client_closes.append((client, asyncio.get_running_loop()))

    monkeypatch.setattr(AgamemnonClient, "__init__", client_init)
    monkeypatch.setattr(AgamemnonClient, "aclose", client_close)


def _observe_loop_and_journal(
    monkeypatch: pytest.MonkeyPatch, fixture: RegistrationFixture
) -> None:
    """Count real loop creation and successful runtime-journal acquisition."""
    real_loop = asyncio.new_event_loop
    real_journal_init = WorkerJournal.__init__

    def new_loop() -> asyncio.AbstractEventLoop:
        """Create and retain the real event loop for identity observations."""
        loop = real_loop()
        fixture.observed.loops.append(loop)
        return loop

    def journal_init(
        journal: WorkerJournal, directory: Path, *, max_bytes: int = 64 * 1024 * 1024
    ) -> None:
        """Count only a successfully opened journal at this runtime's state path."""
        real_journal_init(journal, directory, max_bytes=max_bytes)
        if directory == fixture.cli.state:
            fixture.observed.journals.append(journal)

    monkeypatch.setattr(asyncio, "new_event_loop", new_loop)
    monkeypatch.setattr(WorkerJournal, "__init__", journal_init)


def _observe_provider(monkeypatch: pytest.MonkeyPatch, observed: ResourceObservations) -> None:
    """Observe the actual protocol provider's constructor and close calls."""
    real_init = CodexAppServer.__init__
    real_close = CodexAppServer.close

    def provider_init(
        provider: CodexAppServer,
        command: list[str],
        codex_home: Path,
        *,
        timeout: float = 30,
    ) -> None:
        """Construct the real provider with the unchanged private fixture command."""
        real_init(provider, command, codex_home, timeout=timeout)
        observed.providers.append(provider)

    def provider_close(provider: CodexAppServer) -> bool:
        """Return the real cleanup confirmation before recording the call."""
        result = real_close(provider)
        observed.provider_closes.append(provider)
        return result

    monkeypatch.setattr(CodexAppServer, "__init__", provider_init)
    monkeypatch.setattr(CodexAppServer, "close", provider_close)


def _observe_build_owners(monkeypatch: pytest.MonkeyPatch, observed: ResourceObservations) -> None:
    """Bind each real build owner to its supplied SDK, journal, and loop."""
    real_init = FleetBuildOwner.__init__

    def owner_init(
        owner: FleetBuildOwner,
        service: FleetBuildService,
        journal: WorkerJournal,
        context_id: str,
        *,
        cancellation_ids: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        """Construct the owner before recording the resources it actually received."""
        real_init(owner, service, journal, context_id, cancellation_ids=cancellation_ids)
        observed.owners.append((context_id, service._client, journal, asyncio.get_running_loop()))

    monkeypatch.setattr(FleetBuildOwner, "__init__", owner_init)


def _observe_runtime_start(monkeypatch: pytest.MonkeyPatch, observed: ResourceObservations) -> None:
    """Retain the actual runtime selected by the CLI and acknowledge startup."""
    from hephaestus.automation.fleet_worker_runtime import FleetWorkerRuntime

    real_start = FleetWorkerRuntime.start

    def start(runtime: FleetWorkerRuntime) -> None:
        """Start the real runtime before configuring the no-model protocol fixture."""
        real_start(runtime)
        assert runtime.worker is not None
        # The existing deterministic provider runs no model or tool operation.
        runtime.worker.execution_guard = lambda: None
        observed.runtimes.append(runtime)
        print(json.dumps({"fleetRegistrationFixture": "runtime-started"}), flush=True)

    monkeypatch.setattr(FleetWorkerRuntime, "start", start)


def _observe_receipts(monkeypatch: pytest.MonkeyPatch, fixture: RegistrationFixture) -> None:
    """Preserve actual receipt writes and refuse the local heavy-tool fallback."""

    def no_local_execution(*args: object, **kwargs: object) -> NoReturn:
        """Fail if registered Fleet work reaches the local process boundary."""
        raise AssertionError("registered Fleet build used the local heavy-tool fallback")

    def observe_receipt(filepath: str | Path, content: str, permissions: int = 0o600) -> None:
        """Observe source and evidence exclusion after the actual secure write."""
        value: Json = json.loads(content)
        context_id = value.get("fleet_context_id")
        case = fixture.first if context_id == "first-context" else fixture.second
        assert case is not None
        write_secure(filepath, content, permissions)
        fixture.observed.receipts.append(
            ReceiptObservation(
                path=Path(filepath),
                value=value,
                source_held=lock_is_held(case.source_lock),
                evidence_held=lock_is_held(case.evidence_lock),
            )
        )

    monkeypatch.setattr(worker_pool, "run_subprocess", no_local_execution)
    monkeypatch.setattr(worker_pool, "write_secure", observe_receipt)


def _install_observers(monkeypatch: pytest.MonkeyPatch, fixture: RegistrationFixture) -> None:
    """Install typed observation wrappers after all unused consumers have closed."""
    monkeypatch.setenv("AGAMEMNON_API_KEY", "fixture-private-key")
    _observe_sdk(monkeypatch, fixture.observed)
    _observe_loop_and_journal(monkeypatch, fixture)
    _observe_provider(monkeypatch, fixture.observed)
    _observe_build_owners(monkeypatch, fixture.observed)
    _observe_runtime_start(monkeypatch, fixture.observed)
    _observe_receipts(monkeypatch, fixture)
    monkeypatch.setattr(fleet_worker_cli, "serve", fixture.drive)


def _reject_failed_registration(
    fixture: RegistrationFixture,
    runtime: FleetWorkerRuntime,
    capabilities: BuildCapabilities,
) -> None:
    """Reject a foreign claim and prevent reuse of that failed context identity."""
    invalid: BuildCapabilities = {
        **capabilities,
        "parent": copy.deepcopy(capabilities["parent"]),
    }
    invalid["parent"]["claim"]["agentId"] = "foreign-agent"
    with pytest.raises(ValueError):
        runtime.register_build("failed-context", **invalid)
    with pytest.raises(ValueError):
        runtime.register_build("failed-context", **capabilities)
    assert fixture.observed.owners == [] and fixture.http.requests == []


def _register_first(
    fixture: RegistrationFixture, runtime: FleetWorkerRuntime, worker: FleetWorker
) -> BuildTestJob:
    """Admit the first real session and retain one immutable registered intent."""
    first = fixture.first
    job = replace(first.job(), fleet_context_id="first-context")
    with pytest.raises(ValueError):
        runtime.submit_build(job)
    assert fixture.http.requests == [] and fixture.observed.owners == []

    admitted = worker.handle(_session_command(first, "start"))
    assert admitted["status"] == "completed", admitted
    parent = first.build.publisher.command["payload"]["parent"]
    journal = runtime.journal
    assert journal is not None
    live = journal.snapshot()["sessions"][parent["sessionId"]]
    assert live["workspace"] == parent["claim"]["workspace"]
    assert live["agentId"] == parent["agentId"]
    assert live["executionId"] == parent["executionId"]
    assert live["admissionReserved"] is True and not live["released"]
    capabilities = _capabilities(first, fixture.first_source)
    _reject_failed_registration(fixture, runtime, capabilities)

    runtime.register_build("first-context", **capabilities)
    runtime.register_build("first-context", **capabilities)
    changed: BuildCapabilities = {**capabilities, "repository": "other/repo"}
    with pytest.raises(ValueError):
        runtime.register_build("first-context", **changed)
    assert len(fixture.observed.owners) == 1
    # Mutating caller-owned JSON must not change the registered immutable intent.
    capabilities["submission"]["parameters"]["afterRegistration"] = True
    return job


def _submit_first_at_source_gate(
    fixture: RegistrationFixture, runtime: FleetWorkerRuntime, job: BuildTestJob
) -> JobHandle:
    """Keep the real source lease active while checking retirement and capacity."""
    handle = runtime.submit_build(job)
    try:
        assert fixture.entered.wait(2), "the actual source lease was not acquired"
        assert lock_is_held(fixture.first.source_lock)
        with pytest.raises(RuntimeError):
            runtime.retire_build("first-context")
        with pytest.raises(RuntimeError, match="capacity"):
            runtime.submit_build(job)
        assert runtime.completions.qsize() == 0 and fixture.http.requests == []
    finally:
        fixture.release.set()
    return handle


def _assert_build_result(
    case: PreparedCase, completed: JobHandle, expected: JobHandle, result: JobResult
) -> None:
    """Compare the real completion with an independent collection under both locks."""
    assert completed is expected and result.ok and not result.interrupted
    assert result.fleet_receipt is not None
    assert result.value == case.collect(deadline=time.monotonic() + 5)
    assert isinstance(result.value, dict)
    assert result.value["status"] == "verified_current"


def _take_first_completion(
    fixture: RegistrationFixture,
    runtime: FleetWorkerRuntime,
    job: BuildTestJob,
    handle: JobHandle,
) -> None:
    """Retain capacity through an unconsumed completion, then observe its release."""
    deadline = time.monotonic() + 10
    while runtime.completions.qsize() == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime.completions.qsize() == 1, "the real pool produced no completion"
    request_count = len(fixture.http.requests)
    with pytest.raises(RuntimeError, match="capacity"):
        runtime.submit_build(job)
    assert len(fixture.http.requests) == request_count
    assert runtime.completions.qsize() == 1
    completed, result = runtime.take_completion(timeout=1)
    _assert_build_result(fixture.first, completed, handle, result)
    with pytest.raises(Empty):
        runtime.take_completion(timeout=0)


def _retire_first(
    fixture: RegistrationFixture, runtime: FleetWorkerRuntime, worker: FleetWorker
) -> None:
    """Retire the completed context and release its real idle worker session."""
    runtime.retire_build("first-context")
    with pytest.raises(ValueError):
        runtime.register_build(
            "first-context", **_capabilities(fixture.first, fixture.first_source)
        )
    cancelled = worker.handle(_session_command(fixture.first, "cancel"))
    assert cancelled["status"] == "completed", cancelled


def _attach_second_publication(fixture: RegistrationFixture) -> tuple[int, list[Json]]:
    """Preserve full HTTP history while selecting the next controlled admission."""
    http = fixture.http
    second = fixture.second
    offset = len(http.requests)
    first_history = copy.deepcopy(http.requests)
    second.build.http = http
    second.build._bind_http()
    second.stop_publication.clear()
    second.publication = threading.Thread(
        target=_publish_second_at,
        args=(second, offset),
        name="fixture-second-publication",
    )
    second.publication.start()
    return offset, first_history


def _run_second(
    fixture: RegistrationFixture, runtime: FleetWorkerRuntime, worker: FleetWorker
) -> BuildCapabilities:
    """Admit a later independent session and run its real build on the same client."""
    first, second = fixture.first, fixture.second
    assert second is not None
    offset, first_history = _attach_second_publication(fixture)
    admitted = worker.handle(_session_command(second, "start"))
    assert admitted["status"] == "completed", admitted
    parent1 = first.build.publisher.command["payload"]["parent"]
    parent2 = second.build.publisher.command["payload"]["parent"]
    assert parent2["sessionId"] != parent1["sessionId"]
    assert parent2["executionId"] != parent1["executionId"]
    assert second.build.publisher.source != first.build.publisher.source
    capabilities = _capabilities(second, second.source_lease)
    runtime.register_build("second-context", **capabilities)
    job = replace(second.job(), fleet_context_id="second-context")
    handle = runtime.submit_build(job)
    completed, result = runtime.take_completion(timeout=10)
    _assert_build_result(second, completed, handle, result)
    assert fixture.http.requests[:offset] == first_history
    assert len([row for row in fixture.http.requests if row["method"] == "POST"]) == 2
    assert not any(row["path"].endswith("/cancel") for row in fixture.http.requests)
    assert runtime.completions.qsize() == 0
    return capabilities


def _assert_live_resources(fixture: RegistrationFixture, runtime: FleetWorkerRuntime) -> None:
    """Require one still-open runtime owner for both actual build contexts."""
    observed = fixture.observed
    assert len(observed.clients) == len(observed.loops) == 1
    assert len(observed.journals) == len(observed.providers) == 1
    assert observed.client_closes == observed.provider_closes == []
    assert observed.owners == [
        ("first-context", runtime.client, runtime.journal, runtime.loop),
        ("second-context", runtime.client, runtime.journal, runtime.loop),
    ]


def _assert_retained_intents(fixture: RegistrationFixture, runtime: FleetWorkerRuntime) -> None:
    """Require both unchanged submissions in the runtime's actual shared journal."""
    journal = runtime.journal
    assert journal is not None
    intents = [
        record["value"]
        for record in journal.snapshot()["records"]
        if record["kind"] == "build-consumer"
    ]
    assert {value["contextId"] for value in intents} == {"first-context", "second-context"}
    for value in intents:
        case = fixture.first if value["contextId"] == "first-context" else fixture.second
        assert value["submission"] == case.build.submission


def _cli_arguments(fixture: RegistrationFixture) -> list[str]:
    """Select the real CLI controller profile with the original private fixture paths."""
    cli = fixture.cli
    parent = fixture.first.build.publisher.command["payload"]["parent"]
    return [
        "serve",
        "--state-dir",
        str(cli.state),
        "--workspace-root",
        str(cli.workspaces),
        "--codex-home",
        str(cli.codex),
        "--worker-id",
        parent["claim"]["workerId"],
        "--pool-id",
        "local",
        "--host-id",
        "fixture-host",
        "--capacity",
        "1",
        "--generation",
        "1",
        "--codex-bin",
        str(cli.launcher),
        "--controller-port",
        str(fixture.http.port),
        "--controller-timeout",
        "1",
    ]


def _assert_closed_resources(fixture: RegistrationFixture) -> None:
    """Require actual SDK, loop, provider, and journal closure after the CLI returns."""
    runtime = fixture.runtime
    worker, journal, loop = runtime.worker, runtime.journal, runtime.loop
    assert worker is not None and journal is not None and loop is not None
    observed = fixture.observed
    assert observed.clients == observed.client_closes == [(runtime.client, loop)]
    assert observed.providers == observed.provider_closes == [worker.provider]
    assert loop.is_closed() and journal.snapshot()["closed"]
    assert worker.provider.process is None


def _assert_receipts(fixture: RegistrationFixture) -> None:
    """Require two actual final receipts and preserve their lease-boundary observations."""
    finals = [
        row
        for row in fixture.observed.receipts
        if row.value.get("fleet_receipt_state") == "finalized"
    ]
    pending = [
        row
        for row in fixture.observed.receipts
        if row.value.get("fleet_receipt_state") == "pending"
    ]
    assert len(finals) == len(pending) == 2
    assert len({row.path for row in finals}) == 2
    assert all(row.source_held and row.evidence_held for row in pending)
    assert all(not row.source_held and not row.evidence_held for row in finals)
    assert all(row.value["ok"] and row.value["succeeded"] for row in finals)
    assert all(json.loads(row.path.read_text()) == row.value for row in finals)


def _assert_publishers(fixture: RegistrationFixture) -> None:
    """Require one exited real recipe process for each independent build."""
    for case in (fixture.first, fixture.second):
        assert case is not None
        assert len(case.build.publisher.scheduler.starts) == 1
        assert all(child.poll() == 0 for child in case.build.publisher.scheduler.children.values())


def _registration_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the CLI-selected lifetime with prepared fixtures and typed observations."""
    build_root = Path(__file__).resolve().parents[3] / "build"
    build_root.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fleet-registration-", dir=build_root) as temporary:
        root = Path(temporary).resolve(strict=True)
        with _prepared_registration(root) as fixture:
            _install_observers(monkeypatch, fixture)
            exit_code = fleet_worker_cli.main(_cli_arguments(fixture))
            assert exit_code == 0
            _assert_closed_resources(fixture)
            _assert_receipts(fixture)
            _assert_publishers(fixture)


def test_cli_runtime_runs_two_later_contexts_with_one_resource_owner() -> None:
    """Fence only the actual fixture-owning CLI child if its lifetime fails."""
    child = subprocess.Popen(
        [sys.executable, "-B", "-u", "-m", _MODULE, "--registration-case"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timed_out = forced = False
    stdout = stderr = ""
    try:
        try:
            stdout, stderr = child.communicate(timeout=75)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        if child.poll() is None:
            forced = True
            child.terminate()
            try:
                stdout, stderr = child.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                stdout, stderr = child.communicate(timeout=3)
    events = [
        json.loads(line)["fleetRegistrationFixture"]
        for line in stdout.splitlines()
        if line.startswith('{"fleetRegistrationFixture":')
    ]
    diagnostic = {"exit": child.returncode, "events": events, "stderr": stderr}
    assert not timed_out and not forced, diagnostic
    assert child.returncode == 0, diagnostic
    assert events == ["runtime-started", "completed"], diagnostic


if __name__ == "__main__":
    if sys.argv[1:] != ["--registration-case"]:
        raise SystemExit("this module is an exact-owned integration fixture")
    with pytest.MonkeyPatch.context() as fixture_patches:
        _registration_case(fixture_patches)
    print(json.dumps({"fleetRegistrationFixture": "completed"}), flush=True)
