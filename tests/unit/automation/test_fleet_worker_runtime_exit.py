"""Observe the actual installed CLI's runtime exit with owned subprocess gates.

Apply as tests/unit/automation/test_fleet_worker_runtime_exit.py. The child calls
the installed entry point after instrumenting the actual runtime. No alternate
runtime, fake SDK client, provider account, recipe, or execution claim is used.
The fatal case has no accepted build; source/evidence borrowers need a later case.
"""

from __future__ import annotations

import asyncio
import json
import os
import runpy
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.fleet_journal import WorkerJournal
from tests.unit.automation.test_fleet_build_service import BuildConsumerHTTP
from tests.unit.automation.test_fleet_worker_runtime_cli import (
    OwnedCLI,
    WorkerCLI,
    qualified_cli as qualified_cli,
)

pytestmark = pytest.mark.precommit
_MODULE = "tests.unit.automation.test_fleet_worker_runtime_exit"


def _send(control: socket.socket, value: dict[str, Any]) -> None:
    data = json.dumps(value).encode() + b"\n"
    if len(data) > 8192:
        raise ValueError("fixture observation exceeded its bound")
    control.sendall(data)


def _receive(control: socket.socket, timeout: float = 3) -> dict[str, Any]:
    data = bytearray()
    deadline = time.monotonic() + timeout
    while not data.endswith(b"\n"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("fixture acknowledgment did not arrive")
        control.settimeout(remaining)
        chunk = control.recv(1)
        if not chunk:
            raise EOFError("fixture control closed")
        data.extend(chunk)
        if len(data) > 8192:
            raise ValueError("fixture acknowledgment exceeded its bound")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("fixture acknowledgment is not an object")
    return value


def _writer_held(directory: Path) -> bool:
    try:
        contender = WorkerJournal(directory)
    except RuntimeError as error:
        if "writer" not in str(error):
            raise
        return True
    contender.close()
    return False


def _await_trigger(
    control: socket.socket,
    loop: asyncio.AbstractEventLoop,
    on_loop: Callable[[], None],
    selected: str,
    release: threading.Event,
) -> None:
    """Schedule the actual loop callback only after the parent acknowledges readiness."""
    try:
        if _receive(control, timeout=10) != {"operation": "trigger"}:
            raise ValueError("unexpected fixture trigger")
        loop.call_soon_threadsafe(on_loop)
        if selected == "unresponsive" and _receive(control, timeout=20) != {"operation": "release"}:
            raise ValueError("unexpected fixture release")
    except (EOFError, OSError):
        pass  # Exact CLI exit or parent cleanup closes this private descriptor.
    finally:
        release.set()


def _child(control: socket.socket, selected: str, entrypoint: Path, arguments: list[str]) -> None:
    """Inject only an observation wrapper and one actual SDK-loop callback gate."""
    from hephaestus.automation.fleet_worker_runtime import FleetWorkerRuntime

    release = threading.Event()
    observers: list[threading.Thread] = []
    real_start = FleetWorkerRuntime.start
    real_close = FleetWorkerRuntime.close
    real_cleanup = FleetWorkerRuntime.cleanup_owned_provider
    client_closes: list[dict[str, Any]] = []
    processes: list[subprocess.Popen[Any]] = []
    monkeypatch = pytest.MonkeyPatch()

    def start(owner: FleetWorkerRuntime) -> None:
        real_start(owner)
        worker, journal, loop, client = owner.worker, owner.journal, owner.loop, owner.client
        assert worker is not None and journal is not None
        assert loop is not None and client is not None
        process = worker.provider.process
        assert process is not None
        processes.append(process)
        real_aclose = client.aclose

        async def observe_aclose() -> None:
            before = {
                "on_owner_loop": asyncio.get_running_loop() is loop,
                "journal_held_before": _writer_held(journal.directory),
                "journal_open_before": not journal.snapshot()["closed"],
                "provider_alive_before": process.poll() is None,
            }
            await real_aclose()
            client_closes.append(
                {
                    **before,
                    "journal_held_after": _writer_held(journal.directory),
                    "client_closed": client._client.is_closed,
                }
            )

        monkeypatch.setattr(client, "aclose", observe_aclose)

        def on_loop() -> None:
            _send(
                control,
                {
                    "kind": "loop-entered",
                    "on_owner_loop": asyncio.get_running_loop() is loop,
                    "journal_shared": worker.journal is journal,
                    "journal_held": _writer_held(journal.directory),
                    "provider_pid": process.pid,
                    "provider_alive": process.poll() is None,
                    "client_closed": client._client.is_closed,
                },
            )
            if selected == "unresponsive":
                # Finite fixture fallback exceeds the parent's entire fatal-exit window.
                # No release is sent until exit is observed or cleanup begins.
                release.wait(20)

        observer = threading.Thread(
            target=_await_trigger,
            args=(control, loop, on_loop, selected, release),
            name="fixture-cli-control",
            daemon=True,
        )
        observers.append(observer)
        observer.start()

    def close(owner: FleetWorkerRuntime) -> None:
        real_close(owner)
        if processes:
            assert owner.journal is not None and owner.loop is not None
            _send(
                control,
                {
                    "kind": "close-returned",
                    "client_closes": client_closes,
                    "loop_closed": owner.loop.is_closed(),
                    "provider_returncode": processes[0].poll(),
                    "journal_closed": owner.journal.snapshot()["closed"],
                },
            )

    def cleanup(owner: FleetWorkerRuntime) -> None:
        real_cleanup(owner)
        assert owner.journal is not None and owner.loop is not None
        _send(
            control,
            {
                "kind": "fatal-cleanup",
                "client_closes": client_closes,
                "loop_running": owner.loop.is_running(),
                "loop_closed": owner.loop.is_closed(),
                "provider_returncode": processes[0].poll(),
                "journal_held": _writer_held(owner.journal.directory),
                "journal_closed": owner.journal.snapshot()["closed"],
                "runtime_uncertain": owner.journal.snapshot()["runtime_uncertain"],
            },
        )

    monkeypatch.setattr(FleetWorkerRuntime, "start", start)
    monkeypatch.setattr(FleetWorkerRuntime, "close", close)
    monkeypatch.setattr(FleetWorkerRuntime, "cleanup_owned_provider", cleanup)
    try:
        sys.argv = [str(entrypoint), *arguments]
        runpy.run_path(str(entrypoint), run_name="__main__")
    finally:
        release.set()
        with suppress(OSError):
            control.shutdown(socket.SHUT_RD)
        for observer in observers:
            observer.join(2)
        monkeypatch.undo()


def _observe_cli(case: WorkerCLI, selected: str, port: int) -> dict[str, Any]:
    """Return observations only after finally releasing and reaping this exact CLI."""
    parent, child = socket.socketpair()
    process: subprocess.Popen[str] | None = None
    result: dict[str, Any] = {"timed_out": False, "forced": False}
    case.parent_record.unlink(missing_ok=True)
    case.provider_record.unlink(missing_ok=True)
    arguments = [
        sys.executable,
        "-B",
        "-u",
        "-m",
        _MODULE,
        "--child",
        str(child.fileno()),
        selected,
        str(case.entrypoint),
        "serve",
        "--state-dir",
        str(case.state),
        "--workspace-root",
        str(case.workspaces),
        "--codex-home",
        str(case.codex),
        "--worker-id",
        "worker-cli",
        "--pool-id",
        "local",
        "--host-id",
        "fixture-host",
        "--capacity",
        "2",
        "--generation",
        "1",
        "--codex-bin",
        str(case.launcher),
        "--controller-port",
        str(port),
        "--controller-timeout",
        "1",
    ]
    environment = os.environ.copy()
    environment["AGAMEMNON_API_KEY"] = "fixture-private-key"
    try:
        process = subprocess.Popen(
            arguments,
            pass_fds=(child.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        child.close()
        pending = case.parent_record.with_suffix(".tmp")
        pending.write_text(json.dumps(process.pid))
        pending.replace(case.parent_record)
        # Reuse only readiness, not OwnedCLI.close: final behavior assertions own failures.
        result["inventory"] = case.inventory(OwnedCLI(process, case.provider_record))
        if result["inventory"] is not None:
            result["provider"] = json.loads(case.provider_record.read_text())
            result["parent_matches"] = result["provider"]["parent"] == process.pid
            result["writer_held_at_socket"] = _writer_held(case.state)
            _send(parent, {"operation": "trigger"})
            result["gate"] = _receive(parent)
            result["bytes_at_gate"] = (case.state / "receipts.jsonl").read_bytes()
            if selected == "normal":
                process.send_signal(signal.SIGTERM)
            # Includes actual provider cleanup (bounded to 8 seconds by this draft).
            result["exit_observation"] = _receive(parent, timeout=12)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                result["timed_out"] = True
    except (EOFError, OSError, ValueError) as error:
        result["protocol_error"] = f"{type(error).__name__}: {error}"
        if isinstance(error, TimeoutError):
            result["timed_out"] = True
    finally:
        with suppress(OSError):
            _send(parent, {"operation": "release"})
        if process is not None:
            if process.poll() is None:
                result["forced"] = True
                process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                result["forced"] = True
                process.kill()
                stdout, stderr = process.communicate(timeout=3)
            result.update(returncode=process.returncode, stdout=stdout, stderr=stderr)
        parent.close()
        child.close()
    if case.provider_record.exists():
        provider = json.loads(case.provider_record.read_text())
        deadline = time.monotonic() + 5
        result["provider_gone"] = False
        while time.monotonic() < deadline:
            try:
                os.kill(provider["pid"], 0)  # Observe only; never signal a recorded PID.
            except ProcessLookupError:
                result["provider_gone"] = True
                break
            time.sleep(0.01)
    result["socket_removed"] = not (case.state / "worker.sock").exists()
    result["bytes_after_exit"] = (case.state / "receipts.jsonl").read_bytes()
    return result


@pytest.mark.parametrize("selected", ["normal", "unresponsive"])
def test_actual_cli_retains_runtime_ownership_until_confirmed_or_fatal_exit(
    qualified_cli: WorkerCLI, selected: str
) -> None:
    """Require actual process exit before another writer can acquire the journal."""
    controller = BuildConsumerHTTP()
    try:
        result = _observe_cli(qualified_cli, selected, controller.port)
        assert controller.requests == [], "the lifecycle case must not submit work"
    finally:
        controller.close()
    diagnostic = {key: value for key, value in result.items() if not key.startswith("bytes_")}
    assert (result.get("inventory") or {}).get("workerId") == "worker-cli", diagnostic
    assert result.get("parent_matches") is True, diagnostic
    assert result.get("writer_held_at_socket") is True, diagnostic
    gate = result.get("gate", {})
    assert gate.get("kind") == "loop-entered", diagnostic
    assert gate["on_owner_loop"] and gate["journal_shared"] and gate["journal_held"]
    assert gate["provider_alive"] and not gate["client_closed"]
    assert gate["provider_pid"] == result["provider"]["pid"]
    assert not result["timed_out"] and not result["forced"], diagnostic
    assert result.get("provider_gone") is True and result["socket_removed"], diagnostic
    exit_observation = result.get("exit_observation", {})
    assert exit_observation.get("provider_returncode") is not None, diagnostic
    if selected == "unresponsive":
        assert result["returncode"] == 70, diagnostic
        assert exit_observation["kind"] == "fatal-cleanup", diagnostic
        assert exit_observation["loop_running"] and not exit_observation["loop_closed"]
        assert exit_observation["client_closes"] == []
        assert exit_observation["journal_held"] and not exit_observation["journal_closed"]
        assert exit_observation["runtime_uncertain"] is True
        assert result["bytes_after_exit"] == result["bytes_at_gate"]
    else:
        assert result["returncode"] == 0, diagnostic
        assert exit_observation["kind"] == "close-returned", diagnostic
        assert exit_observation["loop_closed"] and exit_observation["journal_closed"]
        assert exit_observation["client_closes"] == [
            {
                "on_owner_loop": True,
                "journal_held_before": True,
                "journal_open_before": True,
                "provider_alive_before": True,
                "journal_held_after": True,
                "client_closed": True,
            }
        ]
    # Only after the exact CLI exits may another owner take the actual journal flock.
    journal = WorkerJournal(qualified_cli.state)
    try:
        state = journal.snapshot()
        if selected == "unresponsive":
            assert state["runtime_pid"] == result["provider"]["pid"]
            assert state["runtime_uncertain"] is True
        else:
            assert state["runtime_pid"] is None and state["runtime_uncertain"] is False
    finally:
        journal.close()


if __name__ == "__main__":
    if len(sys.argv) < 6 or sys.argv[1] != "--child":
        raise SystemExit("this fixture requires its exact parent-owned control descriptor")
    with socket.socket(fileno=int(sys.argv[2])) as child_control:
        _child(child_control, sys.argv[3], Path(sys.argv[4]), sys.argv[5:])
