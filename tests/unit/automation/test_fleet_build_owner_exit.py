"""Check owner-task exit through real pool jobs in exact-owned child processes.

Apply as tests/unit/automation/test_fleet_build_owner_exit.py before execution.
The child uses the real SDK, HTTP fixture, snapshot, journal and source lease.
These failure paths stop before result collection. They create no recipe process.
The parent reaps only its exact child; it never signals an ambient process group.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import socket
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from hephaestus.automation import fleet_build_jobs
from hephaestus.automation.fleet_build_service import FleetBuildOwner
from hephaestus.automation.fleet_journal import WorkerJournal
from hephaestus.automation.pipeline.jobs import JobHandle
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from tests.unit.automation.test_fleet_build_jobs import PreparedCase, lock_is_held

pytestmark = pytest.mark.precommit
_MODULE = "tests.unit.automation.test_fleet_build_owner_exit"
_MESSAGE_LIMIT = 8192


def _send(control: socket.socket, value: dict[str, Any]) -> None:
    """Send one bounded fixture observation on the private control descriptor."""
    data = json.dumps(value, sort_keys=True).encode() + b"\n"
    if len(data) > _MESSAGE_LIMIT:
        raise ValueError("fixture control message exceeds its bound")
    control.sendall(data)


def _receive(control: socket.socket, *, timeout: float = 8) -> dict[str, Any]:
    """Receive one complete acknowledgment without an unbounded pipe read."""
    data = bytearray()
    deadline = time.monotonic() + timeout
    while not data.endswith(b"\n"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("fixture control acknowledgment did not arrive")
        control.settimeout(remaining)
        chunk = control.recv(1)
        if not chunk:
            raise EOFError("fixture control connection closed")
        data.extend(chunk)
        if len(data) > _MESSAGE_LIMIT:
            raise ValueError("fixture control message exceeds its bound")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("fixture control message is not an object")
    return value


def _writer_is_held(directory: Path) -> bool:
    """Try a distinct real journal owner and close it if exclusion failed."""
    try:
        contender = WorkerJournal(directory)
    except RuntimeError as error:
        if "writer" not in str(error):
            raise
        return True
    contender.close()
    return False


class _OwnerTaskProbe:
    """Observe creation and exit of actual owner tasks on the fixture loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, selected: str) -> None:
        self.loop = loop
        self.selected = selected
        self.installed = threading.Event()
        self.entered = threading.Event()
        self.rejected = threading.Event()
        self.exited = threading.Event()
        self.captured: list[Any] = []
        self.tasks: list[asyncio.Task[Any]] = []
        self.callback_errors: list[str] = []
        self.original_factory = loop.get_task_factory()

    def factory(self, loop: asyncio.AbstractEventLoop, coroutine: Any, **kwargs: Any) -> Any:
        """Retain each real owner coroutine and its actual task, if creation succeeds."""
        is_owner = getattr(coroutine, "cr_code", None) is FleetBuildOwner.submit.__code__
        if is_owner:
            self.captured.append(coroutine)
            self.entered.set()
            if self.selected == "creation-failure":
                # This acknowledgment runs after the rejected callback has returned.
                loop.call_soon(self.rejected.set)
                raise RuntimeError("injected owner task creation failure")
        task = (
            asyncio.Task(coroutine, loop=loop, **kwargs)
            if self.original_factory is None
            else self.original_factory(loop, coroutine, **kwargs)
        )
        if is_owner:
            if not isinstance(task, asyncio.Task):
                raise RuntimeError("fixture factory did not create an actual task")
            self.tasks.append(task)
            task.add_done_callback(lambda _task: self.exited.set())
        return task

    def install(self) -> None:
        """Install observations on the loop that owns task creation."""
        self.loop.set_task_factory(self.factory)
        self.loop.set_exception_handler(
            lambda _loop, context: self.callback_errors.append(
                type(context.get("exception")).__name__
            )
        )
        self.installed.set()


def _start_owner_job(case: PreparedCase, pool: WorkerPool, probe: _OwnerTaskProbe) -> JobHandle:
    """Establish the selected failure after the publisher has stopped."""
    # Stop this fixture's publisher before any POST or GET. No recipe child can start.
    case.stop_publication.set()
    case.publication.join(2)
    if case.publication.is_alive() or case.publisher_error is not None:
        raise RuntimeError("fixture publisher did not stop before admission")
    http = case.build.http
    if http is None:
        raise RuntimeError("fixture HTTP owner is unavailable")
    probe.loop.call_soon_threadsafe(probe.install)
    if not probe.installed.wait(2):
        raise RuntimeError("fixture task factory did not install")
    if probe.selected == "closed-loop":
        http.mode = "stall"
    handle = pool.submit(
        replace(case.job(), timeout_s=4), StageName.IMPLEMENTATION, claim_key="fixture#17"
    )
    if not probe.entered.wait(2):
        raise RuntimeError("the actual owner coroutine did not reach task creation")
    if probe.selected == "closed-loop":
        if not http.entered.wait(2) or len(probe.tasks) != 1 or probe.tasks[0].done():
            raise RuntimeError("the actual SDK request is not pending")
    elif not probe.rejected.wait(2):
        raise RuntimeError("the task creation callback did not exit")
    return handle


def _trigger_owner_fault(
    control: socket.socket, case: PreparedCase, probe: _OwnerTaskProbe, shutdown: threading.Event
) -> None:
    """Acknowledge readiness before the parent releases the selected loop fault."""
    http = case.build.http
    assert http is not None
    _send(
        control,
        {
            "kind": "ready",
            "entered": probe.entered.is_set(),
            "request_count": len(http.requests),
            "creation_rejected": probe.rejected.is_set(),
        },
    )
    if _receive(control) != {"operation": "trigger"}:
        raise ValueError("fixture trigger was not acknowledged")
    if probe.selected == "closed-loop":
        probe.loop.call_soon_threadsafe(probe.loop.stop)
        case.build.loop_thread.join(2)
        if case.build.loop_thread.is_alive() or probe.tasks[0].done():
            raise RuntimeError("fixture did not stop the loop with its actual task pending")
        probe.loop.close()
        # Interrupt only local observation. Do not call pool.shutdown or remote cancel.
        shutdown.set()


def _pool_observation(completions: CompletionQueue, handle: JobHandle) -> dict[str, Any]:
    """Read the actual pool result and check for a duplicate completion."""
    completion: dict[str, Any] | None = None
    try:
        completed, result = completions.get(timeout=1)
    except Empty:
        pass
    else:
        completion = {
            "same_handle": completed is handle,
            "ok": result.ok,
            "error": result.error,
            "interrupted": result.interrupted,
        }
    try:
        completions.get_nowait()
    except Empty:
        duplicate = False
    else:
        duplicate = True
    return {"completion": completion, "duplicate": duplicate}


def _child(control: socket.socket, selected: str, root: Path) -> None:
    """Retain fixture resources until the parent fences this exact process."""
    case = PreparedCase(root)
    http = case.build.http
    assert http is not None
    shutdown = threading.Event()
    completions = CompletionQueue(maxsize=2)
    pool = WorkerPool(
        size=1,
        shutdown=shutdown,
        completion_q=completions,
        lock_dir=root / "pool-locks",
        evidence_receipt_dir=root / "pool-receipts",
        fleet_build_runner=case.runner(),
    )
    probe = _OwnerTaskProbe(case.build.loop, selected)
    handle = _start_owner_job(case, pool, probe)
    _trigger_owner_fault(control, case, probe, shutdown)
    completion = _pool_observation(completions, handle)
    if case.build.publisher.scheduler.children:
        raise RuntimeError("a recipe child started in a pre-collection failure case")
    _send(
        control,
        {
            "kind": "observed",
            **completion,
            "owner_coroutines": len(probe.captured),
            "coroutine_state": inspect.getcoroutinestate(probe.captured[0]),
            "owner_tasks": len(probe.tasks),
            "task_done": probe.tasks[0].done() if probe.tasks else None,
            "task_exit_callback": probe.exited.is_set(),
            "loop_closed": probe.loop.is_closed(),
            "source_held": lock_is_held(case.source_lock),
            "journal_held": _writer_is_held(case.build.journal.directory),
            "evidence_acquisitions": len(case.evidence_deadlines),
            "request_count": len(http.requests),
            "cancel_posts": sum(request["path"].endswith("/cancel") for request in http.requests),
            "callback_errors": probe.callback_errors,
        },
    )
    if _receive(control) != {"operation": "release"}:
        raise ValueError("fixture release was not acknowledged")
    http.release.set()
    http.close()
    _send(
        control,
        {
            "kind": "released",
            "http_released": http.release.is_set(),
            "http_thread_exited": not http.thread.is_alive(),
        },
    )
    # A closed loop cannot acknowledge its pending task. Never close its journal here.
    # Keep all owners referenced until the parent terminates and reaps this exact child.
    try:
        _receive(control)
    except EOFError:
        return


def _exercise_child(tmp_path: Path, selected: str) -> dict[str, Any]:
    """Run one bounded fault and always reap only the process this test created."""
    root = tmp_path / "owned-child"
    root.mkdir(mode=0o700)
    project = Path(fleet_build_jobs.__file__).resolve().parents[2]
    parent, child = socket.socketpair()
    observed: dict[str, Any] = {}
    process: subprocess.Popen[bytes] | None = None
    with parent, child, (tmp_path / "child-stderr.txt").open("wb") as errors:
        parent.settimeout(8)
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-m",
                    _MODULE,
                    "--child",
                    selected,
                    str(child.fileno()),
                    str(root),
                ],
                cwd=project,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=errors,
                pass_fds=(child.fileno(),),
            )
            child.close()
            ready = _receive(parent, timeout=30)
            if ready.get("kind") != "ready" or ready.get("entered") is not True:
                pytest.fail(f"child fixture setup did not complete: {ready}")
            before_source = lock_is_held(root / "source-access.lock")
            before_journal = _writer_is_held(root / "consumer-journal")
            if selected == "closed-loop" and (
                ready.get("request_count") != 1 or not before_source or not before_journal
            ):
                pytest.fail("closed-loop fixture did not hold an actual admitted SDK request")
            if selected == "creation-failure" and ready.get("creation_rejected") is not True:
                pytest.fail("task creation fixture did not acknowledge its actual rejection")
            _send(parent, {"operation": "trigger"})
            observed = _receive(parent)
            if observed.get("kind") != "observed":
                pytest.fail(f"child fixture did not report its observations: {observed}")
            # These contenders run in the pytest process, outside the child under test.
            observed["parent_source_held"] = lock_is_held(root / "source-access.lock")
            observed["parent_journal_held"] = _writer_is_held(root / "consumer-journal")
            _send(parent, {"operation": "release"})
            released = _receive(parent)
            if released != {"kind": "released", "http_released": True, "http_thread_exited": True}:
                pytest.fail(f"child fixture did not acknowledge release: {released}")
        except (OSError, EOFError, ValueError) as error:
            pytest.fail(f"child fixture protocol failed: {type(error).__name__}: {error}")
        finally:
            # Never use killpg, process discovery, or the pool's global shutdown path.
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                observed["child_reaped"] = process.poll() is not None
    return observed


def test_closed_loop_does_not_release_source_before_actual_owner_task_exit(tmp_path: Path) -> None:
    """A stopped and closed SDK loop cannot stand in for its pending task's exit."""
    observed = _exercise_child(tmp_path, "closed-loop")
    assert observed["owner_coroutines"] == 1
    assert observed["owner_tasks"] == 1
    assert observed["loop_closed"] is True
    assert observed["task_done"] is False
    assert observed["task_exit_callback"] is False
    assert observed["request_count"] == 1
    assert observed["cancel_posts"] == 0
    assert observed["evidence_acquisitions"] == 0
    assert observed["child_reaped"] is True
    assert observed["completion"] is None, (
        "the pool completed although the closed loop never acknowledged its pending task's exit"
    )
    assert observed["source_held"] and observed["parent_source_held"], (
        "the runner released its actual source lease before the owner task exited"
    )
    assert observed["journal_held"] and observed["parent_journal_held"]
    assert observed["duplicate"] is False


def test_rejected_task_creation_closes_coroutine_and_finishes_pool_failure(tmp_path: Path) -> None:
    """A task that never started must not strand the real submitted pool job."""
    observed = _exercise_child(tmp_path, "creation-failure")
    assert observed["owner_coroutines"] == 1
    assert observed["owner_tasks"] == 0
    assert observed["request_count"] == 0
    assert observed["evidence_acquisitions"] == 0
    assert observed["child_reaped"] is True
    assert observed["completion"] is not None, (
        "task creation failed before execution but the actual pool job never completed"
    )
    assert observed["completion"]["same_handle"] is True
    assert observed["completion"]["ok"] is False
    assert observed["completion"]["interrupted"] is False
    assert observed["completion"]["error"]
    assert observed["coroutine_state"] == inspect.CORO_CLOSED
    assert observed["source_held"] is False
    assert observed["parent_source_held"] is False
    assert observed["journal_held"] and observed["parent_journal_held"]
    assert observed["duplicate"] is False
    assert observed["callback_errors"] == []


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "--child":
        raise SystemExit("this module accepts only its exact-owned child fixture invocation")
    with socket.socket(fileno=int(sys.argv[3])) as control:
        control.settimeout(8)
        try:
            _child(control, sys.argv[2], Path(sys.argv[4]))
        except BaseException as error:
            _send(
                control,
                {"kind": "fixture_error", "error": type(error).__name__, "detail": str(error)},
            )
            # The parent still owns termination if setup left any fixture thread alive.
            with suppress(OSError, EOFError):
                _receive(control)
