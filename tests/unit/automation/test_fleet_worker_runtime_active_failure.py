"""Observe fatal CLI exit with one actual admitted build still borrowing source.

Apply as tests/unit/automation/test_fleet_worker_runtime_active_failure.py.
Admission and allocation remain controlled fixtures. The actual installed CLI,
worker session, SDK request, journal, pool job, and source exclusion are used.
The publisher is stopped before runtime activation: this case executes no recipe.
"""

from __future__ import annotations

import asyncio
import json
import os
import runpy
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation import fleet_worker_cli
from hephaestus.automation.fleet_build_service import FleetBuildOwner
from hephaestus.automation.fleet_journal import WorkerJournal
from hephaestus.automation.fleet_worker import FleetWorker
from hephaestus.automation.fleet_worker_runtime import FleetWorkerRuntime
from tests.unit.automation.test_fleet_build_jobs import PreparedCase, lock_is_held
from tests.unit.automation.test_fleet_worker_runtime_cli import WorkerCLI
from tests.unit.automation.test_fleet_worker_runtime_exit import _receive, _send, _writer_held
from tests.unit.automation.test_fleet_worker_runtime_registration import (
    ResourceObservations,
    _capabilities,
    _close_unused_consumer,
    _observe_build_owners,
    _observe_provider,
    _observe_sdk,
    _session_command,
)

pytestmark = pytest.mark.precommit
_MODULE = "tests.unit.automation.test_fleet_worker_runtime_active_failure"
type Json = dict[str, Any]


@dataclass
class ActiveFailureFixture:
    """Keep observations and finite gates for the one fixture-owned CLI lifetime."""

    root: Path
    case: PreparedCase
    control: socket.socket
    observed: ResourceObservations = field(default_factory=ResourceObservations)
    runtime: FleetWorkerRuntime | None = None
    provider: subprocess.Popen[bytes] | None = None
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    task_exited: threading.Event = field(default_factory=threading.Event)
    http_entered: threading.Event = field(default_factory=threading.Event)
    loop_release: threading.Event = field(default_factory=threading.Event)
    admission_started: bool = False

    def owner(self) -> FleetWorkerRuntime:
        """Return the actual runtime captured from the installed CLI's startup."""
        assert self.runtime is not None
        return self.runtime


def _prepare_case(root: Path) -> PreparedCase:
    """Keep source and HTTP fixtures while closing all unused consumer resources."""
    case_root = root / "workspaces" / "active"
    case_root.mkdir(mode=0o700)
    case = PreparedCase(case_root)
    try:
        case.stop_publication.set()
        case.publication.join(2)
        assert not case.publication.is_alive() and case.publisher_error is None
        _close_unused_consumer(case)
        assert case.build.http is not None
        assert case.build.publisher.scheduler.starts == []
        return case
    except BaseException:
        case.close()
        raise


def _observe_storage(monkeypatch: pytest.MonkeyPatch, fixture: ActiveFailureFixture) -> None:
    """Count successful runtime journal acquisition and actual event-loop creation."""
    real_journal = WorkerJournal.__init__
    real_loop = asyncio.new_event_loop

    def journal_init(
        journal: WorkerJournal, directory: Path, *, max_bytes: int = 64 * 1024 * 1024
    ) -> None:
        """Record only the real runtime writer, excluding rejected flock contenders."""
        real_journal(journal, directory, max_bytes=max_bytes)
        if directory == fixture.root / "state":
            fixture.observed.journals.append(journal)

    def new_loop() -> asyncio.AbstractEventLoop:
        """Return the actual loop and retain its identity for ownership assertions."""
        loop = real_loop()
        fixture.observed.loops.append(loop)
        return loop

    monkeypatch.setattr(WorkerJournal, "__init__", journal_init)
    monkeypatch.setattr(asyncio, "new_event_loop", new_loop)


def _gate_http(monkeypatch: pytest.MonkeyPatch, fixture: ActiveFailureFixture) -> None:
    """Hold the actual submit handler until fatal observation or fixture cleanup."""
    http = fixture.case.build.http
    assert http is not None
    http.mode = "stall"
    real_wait = http.release.wait

    def wait(timeout: float | None = None) -> bool:
        """Acknowledge the actual handler gate and delegate to its real event wait."""
        assert timeout == 2
        fixture.http_entered.set()
        return real_wait(30)

    monkeypatch.setattr(http.release, "wait", wait)


def _capture_owner_task(fixture: ActiveFailureFixture) -> None:
    """Use the existing task-factory observation pattern on the actual SDK loop."""
    loop = fixture.owner().loop
    assert loop is not None
    original = loop.get_task_factory()
    installed = threading.Event()

    def factory(
        owner_loop: asyncio.AbstractEventLoop,
        coroutine: Coroutine[Any, Any, Any],
        **kwargs: Any,
    ) -> asyncio.Task[Any]:
        """Construct the real task and observe only FleetBuildOwner.submit exit."""
        task = (
            asyncio.Task(coroutine, loop=owner_loop, **kwargs)
            if original is None
            else original(owner_loop, coroutine, **kwargs)
        )
        assert isinstance(task, asyncio.Task)
        if getattr(coroutine, "cr_code", None) is FleetBuildOwner.submit.__code__:
            fixture.tasks.append(task)
            task.add_done_callback(lambda _task: fixture.task_exited.set())
        return task

    def install() -> None:
        """Install the observation factory on its existing owner thread."""
        loop.set_task_factory(factory)
        installed.set()

    loop.call_soon_threadsafe(install)
    assert installed.wait(2), "the actual SDK loop did not install its task observer"


def _facts(fixture: ActiveFailureFixture, kind: str) -> Json:
    """Inspect real pending work and exclusion without authoring a runtime fact."""
    runtime = fixture.owner()
    worker, journal, loop = runtime.worker, runtime.journal, runtime.loop
    client = runtime.client
    http, provider = fixture.case.build.http, fixture.provider
    assert worker is not None and journal is not None and loop is not None
    assert client is not None and http is not None and provider is not None
    state = journal.snapshot()
    intents = [row["value"] for row in state["records"] if row["kind"] == "build-consumer"]
    return {
        "kind": kind,
        "source_held": lock_is_held(fixture.case.source_lock),
        "journal_held": _writer_held(journal.directory),
        "journal_shared": worker.journal is journal,
        "journal_closed": state["closed"],
        "runtime_pid": state["runtime_pid"],
        "runtime_uncertain": state["runtime_uncertain"],
        "intents": intents,
        "owner_tasks": len(fixture.tasks),
        "task_done": fixture.tasks[0].done() if fixture.tasks else None,
        "task_exit_callback": fixture.task_exited.is_set(),
        "http_gate_entered": fixture.http_entered.is_set(),
        "http_thread_alive": http.thread.is_alive(),
        "requests": [{"method": row["method"], "path": row["path"]} for row in http.requests],
        "completions": runtime.completions.qsize(),
        "source_acquisitions": len(fixture.case.source_deadlines),
        "evidence_acquisitions": len(fixture.case.evidence_deadlines),
        "recipe_starts": len(fixture.case.build.publisher.scheduler.starts),
        "recipe_children": len(fixture.case.build.publisher.scheduler.children),
        "clients": len(fixture.observed.clients),
        "client_closes": len(fixture.observed.client_closes),
        "client_closed": client._client.is_closed,
        "journals": len(fixture.observed.journals),
        "loops": len(fixture.observed.loops),
        "loop_running": loop.is_running(),
        "loop_closed": loop.is_closed(),
        "providers": len(fixture.observed.providers),
        "provider_closes": len(fixture.observed.provider_closes),
        "provider_pid": provider.pid,
        "provider_returncode": provider.poll(),
        "owner_binding_matches": fixture.observed.owners
        == [("active-context", client, journal, loop)],
        "socket_exists": (journal.directory / "worker.sock").exists(),
    }


def _block_loop(fixture: ActiveFailureFixture) -> None:
    """Acknowledge actual pending task ownership, then stop SDK-loop progress."""
    assert asyncio.get_running_loop() is fixture.owner().loop
    _send(fixture.control, _facts(fixture, "loop-entered"))
    # This exceeds the 12-second fatal observation and 5-second parent release gates.
    fixture.loop_release.wait(30)


def _admit_pending(fixture: ActiveFailureFixture, worker: FleetWorker) -> None:
    """Admit through real worker handling and observe durable intent before blocking."""
    runtime = fixture.owner()
    assert worker is runtime.worker
    admitted = worker.handle(_session_command(fixture.case, "start"))
    assert admitted["status"] == "completed", admitted
    runtime.register_build(
        "active-context", **_capabilities(fixture.case, fixture.case.source_lease)
    )
    runtime.submit_build(
        replace(fixture.case.job(), fleet_context_id="active-context", timeout_s=30)
    )
    assert fixture.http_entered.wait(2), "the real SDK submit did not reach the HTTP gate"
    assert len(fixture.tasks) == 1 and not fixture.tasks[0].done()
    _send(fixture.control, _facts(fixture, "admitted"))
    loop = runtime.loop
    assert loop is not None
    # Schedule immediately while the positively observed real HTTP request is pending.
    loop.call_soon_threadsafe(_block_loop, fixture)


def _instrument_cli(monkeypatch: pytest.MonkeyPatch, fixture: ActiveFailureFixture) -> None:
    """Observe the real factory, original socket service, and owned-provider cleanup."""
    real_start = FleetWorkerRuntime.start
    real_serve = fleet_worker_cli.serve
    real_cleanup = FleetWorkerRuntime.cleanup_owned_provider

    def start(runtime: FleetWorkerRuntime) -> None:
        """Start the real runtime before permitting only fixture session admission."""
        real_start(runtime)
        worker = runtime.worker
        assert worker is not None
        worker.execution_guard = lambda: None
        fixture.runtime = runtime
        fixture.provider = worker.provider.process
        assert fixture.provider is not None
        _capture_owner_task(fixture)

    def serve(worker: FleetWorker, *, check_health: Callable[[], None] | None = None) -> None:
        """Keep the actual Unix server and admit on its first real health iteration."""
        assert check_health == fixture.owner().check_health
        assert check_health is not None

        def health() -> None:
            """Admit once after the real server binds, then keep the real health checks."""
            if not fixture.admission_started:
                fixture.admission_started = True
                _admit_pending(fixture, worker)
            check_health()

        real_serve(worker, check_health=health)

    def cleanup(runtime: FleetWorkerRuntime) -> None:
        """Confirm actual provider cleanup while unresolved borrowers stay fenced."""
        assert runtime is fixture.owner()
        real_cleanup(runtime)
        http = fixture.case.build.http
        assert http is not None
        http.release.set()
        http.close()
        _send(fixture.control, _facts(fixture, "fatal-cleanup"))
        # The existing release protocol allows independent flock observations before exit.
        if _receive(fixture.control, timeout=5) != {"operation": "release"}:
            raise ValueError("the parent did not acknowledge the fatal observation")

    monkeypatch.setattr(FleetWorkerRuntime, "start", start)
    monkeypatch.setattr(fleet_worker_cli, "serve", serve)
    monkeypatch.setattr(FleetWorkerRuntime, "cleanup_owned_provider", cleanup)


def _arguments(fixture: ActiveFailureFixture) -> list[str]:
    """Select the actual CLI's one-second controller profile and private provider."""
    parent = fixture.case.build.publisher.command["payload"]["parent"]
    http = fixture.case.build.http
    assert http is not None
    return [
        "serve",
        "--state-dir",
        str(fixture.root / "state"),
        "--workspace-root",
        str(fixture.root / "workspaces"),
        "--codex-home",
        str(fixture.root / "codex"),
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
        str(fixture.root / "fixture-codex"),
        "--controller-port",
        str(http.port),
        "--controller-timeout",
        "1",
    ]


def _child(control: socket.socket, root: Path, entrypoint: Path) -> None:
    """Keep the one actual CLI and its borrowed source until exact process exit."""
    case = _prepare_case(root)
    fixture = ActiveFailureFixture(root, case, control)
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("AGAMEMNON_API_KEY", "fixture-private-key")
        _observe_sdk(monkeypatch, fixture.observed)
        _observe_provider(monkeypatch, fixture.observed)
        _observe_build_owners(monkeypatch, fixture.observed)
        _observe_storage(monkeypatch, fixture)
        _gate_http(monkeypatch, fixture)
        _instrument_cli(monkeypatch, fixture)
        sys.argv = [str(entrypoint), *_arguments(fixture)]
        runpy.run_path(str(entrypoint), run_name="__main__")
    finally:
        fixture.loop_release.set()
        try:
            case.close()
        finally:
            monkeypatch.undo()


def _observe_child(cli: WorkerCLI) -> Json:
    """Return observations after releasing and reaping only the exact CLI child."""
    parent, child = socket.socketpair()
    process: subprocess.Popen[str] | None = None
    observed: Json = {"forced": False, "timed_out": False}
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-u",
                "-m",
                _MODULE,
                "--child",
                str(child.fileno()),
                str(cli.root),
                str(cli.entrypoint),
            ],
            pass_fds=(child.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        child.close()
        pending = cli.parent_record.with_suffix(".tmp")
        pending.write_text(json.dumps(process.pid))
        pending.replace(cli.parent_record)
        observed["admitted"] = _receive(parent, timeout=30)
        observed["loop"] = _receive(parent, timeout=3)
        observed["fatal"] = _receive(parent, timeout=12)
        observed["alive_at_fatal"] = process.poll() is None
        observed["source_held_at_fatal"] = lock_is_held(
            cli.workspaces / "active" / "source-access.lock"
        )
        observed["writer_held_at_fatal"] = _writer_held(cli.state)
        observed["bytes_at_fatal"] = (cli.state / "receipts.jsonl").read_bytes()
        observed["provider"] = json.loads(cli.provider_record.read_text())
        observed["parent_matches"] = observed["provider"]["parent"] == process.pid
        _send(parent, {"operation": "release"})
        process.wait(timeout=3)
    except (EOFError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        observed["error"] = f"{type(error).__name__}: {error}"
        observed["timed_out"] = isinstance(error, (TimeoutError, subprocess.TimeoutExpired))
    finally:
        with suppress(OSError):
            _send(parent, {"operation": "release"})
        if process is not None:
            try:
                _reap_cli(process, observed)
            finally:
                observed["provider_cleanup"] = _observe_provider_exit(cli, process.pid)
        parent.close()
        child.close()
    return observed


def _reap_cli(process: subprocess.Popen[str], observed: Json) -> None:
    """Reap the exact child with the existing three-second cleanup bounds."""
    try:
        if process.poll() is None:
            observed["forced"] = True
            process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            observed["forced"] = True
            process.kill()
            stdout, stderr = process.communicate(timeout=3)
        observed.update(returncode=process.returncode, stdout=stdout, stderr=stderr)
    except (OSError, subprocess.TimeoutExpired) as error:
        observed["forced"] = True
        observed["cleanup_error"] = f"{type(error).__name__}: {error}"
        observed["returncode"] = process.poll()


def _observe_provider_exit(cli: WorkerCLI, cli_pid: int) -> Json:
    """Observe provider absence after exact child cleanup on every outcome."""
    try:
        record = json.loads(cli.provider_record.read_text())
    except (OSError, ValueError) as error:
        return {"gone": False, "error": f"provider record: {type(error).__name__}: {error}"}
    if not isinstance(record, dict) or record.get("parent") != cli_pid:
        return {"gone": False, "error": "provider record does not match the exact CLI parent"}
    provider_pid = record.get("pid")
    if type(provider_pid) is not int or provider_pid <= 0:
        return {"gone": False, "error": "provider record has no valid process identity"}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(provider_pid, 0)  # Existence observation only; never signal a recorded PID.
        except ProcessLookupError:
            return {"gone": True, "pid": provider_pid, "parent": cli_pid}
        except OSError as error:
            return {"gone": False, "error": f"provider observation: {error}"}
        time.sleep(0.01)
    return {"gone": False, "pid": provider_pid, "error": "provider remained after cleanup"}


def _assert_pending(facts: Json, expected_kind: str) -> None:
    """Require actual unresolved intent, one HTTP submit, and retained exclusion."""
    assert facts["kind"] == expected_kind
    assert facts["source_held"] and facts["journal_held"] and facts["journal_shared"]
    assert not facts["journal_closed"] and facts["runtime_uncertain"]
    assert facts["owner_tasks"] == 1 and facts["task_done"] is False
    assert not facts["task_exit_callback"] and facts["http_gate_entered"]
    assert facts["completions"] == facts["recipe_starts"] == facts["recipe_children"] == 0
    assert facts["source_acquisitions"] == 1 and facts["evidence_acquisitions"] == 0
    assert facts["clients"] == facts["journals"] == facts["loops"] == facts["providers"] == 1
    assert facts["client_closes"] == 0 and not facts["client_closed"]
    assert facts["loop_running"] and not facts["loop_closed"] and facts["owner_binding_matches"]
    assert len(facts["requests"]) == 1 and facts["requests"][0]["method"] == "POST"
    assert facts["requests"][0]["path"].endswith("/submit")
    assert len(facts["intents"]) == 1
    intent = facts["intents"][0]
    assert intent["contextId"] == "active-context"
    assert intent["admission"] is None and intent["cancellation"] is None


def _assert_reaped(cli: WorkerCLI, observed: Json) -> None:
    """Observe provider absence and replay unresolved records after exact CLI exit."""
    provider_pid = observed["provider"]["pid"]
    deadline = time.monotonic() + 5
    gone = False
    while time.monotonic() < deadline:
        try:
            os.kill(provider_pid, 0)  # Existence observation only; never signal a recorded PID.
        except ProcessLookupError:
            gone = True
            break
        time.sleep(0.01)
    assert gone, "the actual fixture provider did not exit"
    assert not (cli.state / "worker.sock").exists()
    assert not lock_is_held(cli.workspaces / "active" / "source-access.lock")
    assert (cli.state / "receipts.jsonl").read_bytes() == observed["bytes_at_fatal"]
    journal = WorkerJournal(cli.state)
    try:
        state = journal.snapshot()
        assert state["runtime_pid"] == provider_pid and state["runtime_uncertain"] is True
        intents = [row["value"] for row in state["records"] if row["kind"] == "build-consumer"]
        assert intents == observed["admitted"]["intents"]
    finally:
        journal.close()


def test_actual_cli_fences_an_admitted_build_when_its_sdk_loop_stops_progress() -> None:
    """Require fatal process fencing without inventing task exit or clean completion."""
    build_root = Path(__file__).resolve().parents[3] / "build"
    build_root.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fleet-active-failure-", dir=build_root) as temporary:
        cli = WorkerCLI(Path(temporary).resolve(strict=True))
        observed = _observe_child(cli)
        diagnostic = {key: value for key, value in observed.items() if key != "bytes_at_fatal"}
        assert not observed["timed_out"] and not observed["forced"], diagnostic
        assert observed.get("provider_cleanup", {}).get("gone") is True, diagnostic
        assert observed.get("returncode") == 70, diagnostic
        assert observed.get("alive_at_fatal") and observed.get("parent_matches"), diagnostic
        _assert_pending(observed["admitted"], "admitted")
        _assert_pending(observed["loop"], "loop-entered")
        _assert_pending(observed["fatal"], "fatal-cleanup")
        assert observed["admitted"]["socket_exists"] is True
        assert observed["admitted"]["provider_returncode"] is None
        assert observed["loop"]["provider_returncode"] is None
        assert observed["fatal"]["provider_returncode"] is not None
        assert observed["fatal"]["provider_closes"] == 1
        assert observed["fatal"]["http_thread_alive"] is False
        assert observed["source_held_at_fatal"] and observed["writer_held_at_fatal"]
        assert observed["fatal"]["intents"] == observed["admitted"]["intents"]
        assert observed["fatal"]["provider_pid"] == observed["provider"]["pid"]
        _assert_reaped(cli, observed)


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "--child":
        raise SystemExit("this fixture requires its exact parent-owned control descriptor")
    with socket.socket(fileno=int(sys.argv[2])) as child_control:
        _child(child_control, Path(sys.argv[3]), Path(sys.argv[4]))
