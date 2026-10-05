"""Own the local Fleet worker and its borrowed build resources."""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import stat
import threading
import time
from collections.abc import Coroutine
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, TypeVar

from hephaestus.automation.fleet_build_contract import encoded, equal, fields, identifier
from hephaestus.automation.fleet_build_jobs import (
    FleetBuildJobContext,
    FleetBuildJobRunner,
    ResultHandoff,
    SourceLease,
)
from hephaestus.automation.fleet_build_service import FleetBuildOwner, FleetBuildService
from hephaestus.automation.fleet_build_storage import journal_directory, private_directory
from hephaestus.automation.fleet_build_supervisor import BuildSnapshot
from hephaestus.automation.fleet_journal import WorkerJournal
from hephaestus.automation.fleet_worker import FleetWorker
from hephaestus.automation.pipeline.job_results import JobHandle, JobResult
from hephaestus.automation.pipeline.jobs import BuildTestJob
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool

T = TypeVar("T")


class RuntimeStoppedError(RuntimeError):
    """Indicate that the SDK loop stopped and admission must end."""


class RuntimeUncertainError(RuntimeError):
    """Retain resources until the exact process owner terminates this runtime."""


class FleetWorkerRuntime:
    """Keep one worker, SDK loop, journal, and pool in one process lifetime.

    The synchronous control thread calls start, check_health, and close.
    No method discovers issues or supplies missing source or result authority.
    """

    def __init__(
        self, *, controller_port: int, controller_timeout: float = 5, **worker_options: Any
    ) -> None:
        """Capture the explicit local profile without starting any resource."""
        if type(controller_port) is not int or not 1 <= controller_port <= 65535:
            raise ValueError("invalid_controller_port")
        if (
            type(controller_timeout) not in (int, float)
            or not math.isfinite(controller_timeout)
            or not 0 < controller_timeout <= 30
        ):
            raise ValueError("invalid_controller_timeout")
        self._port = controller_port
        self._timeout = float(controller_timeout)
        self._exit_bound = max(1.0, self._timeout)
        self._api_key = os.environ.get("AGAMEMNON_API_KEY", "")
        self._worker_options = dict(worker_options)
        self._owner_thread = threading.get_ident()
        self._directory_fd: int | None = None
        self.journal: WorkerJournal | None = None
        self.worker: FleetWorker | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client: Any = None
        self.runner: FleetBuildJobRunner | None = None
        self.pool: WorkerPool | None = None
        self.completions = CompletionQueue(maxsize=48)
        self._shutdown = threading.Event()
        self._loop_failed = threading.Event()
        self._loop_finished = threading.Event()
        self._loop_stop = threading.Event()
        self._loop_ready = threading.Event()
        self._loop_thread: threading.Thread | None = None
        self._pool_finished = threading.Event()
        self._pool_thread: threading.Thread | None = None
        self._pool_error: BaseException | None = None
        self._completion_wakeup = threading.Event()
        self._completion_saturation = threading.Event()
        self._contexts: dict[str, FleetBuildJobContext] = {}
        self._context_ids: set[str] = set()
        self._inflight: dict[JobHandle, str] = {}
        self._client_closed = False
        self._pulse_at = time.monotonic()
        self._started = False
        self._closing = False
        self._closed = False

    def _control_thread(self) -> None:
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("runtime operation requires its control thread")

    def _open_directory(self, directory: Path) -> None:
        if not directory.is_absolute() or directory.resolve(strict=True) != directory:
            raise ValueError("runtime state directory must exist and be canonical")
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise ValueError("runtime state directory must be private")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(descriptor)
            raise
        self._directory_fd = descriptor

    def start(self) -> None:
        """Qualify storage before starting the one SDK loop and provider."""
        self._control_thread()
        if self._started or self._closing or self._closed or self.journal is not None:
            raise RuntimeError("runtime cannot be started twice")
        directory = Path(self._worker_options["state_dir"])
        self._open_directory(directory)
        with journal_directory(directory, time.monotonic() + self._exit_bound):
            self.journal = WorkerJournal(directory)
        self.worker = FleetWorker(**self._worker_options, journal=self.journal)
        self.loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._serve_loop, name="fleet-sdk-owner")
        self._loop_thread.start()
        if not self._loop_ready.wait(self._exit_bound):
            raise RuntimeUncertainError("SDK loop did not acknowledge startup")
        self._call(self._open_client())
        self.runner = FleetBuildJobRunner(
            loop=self.loop, contexts={}, loop_failed=self._loop_failed
        )
        receipts, locks = directory / "build-receipts", directory / "build-locks"
        for path in (receipts, locks):
            with private_directory(path, time.monotonic() + self._exit_bound):
                pass
        self.pool = WorkerPool(
            size=self.worker.capacity,
            shutdown=self._shutdown,
            completion_q=self.completions,
            lock_dir=locks,
            evidence_receipt_dir=receipts,
            fleet_build_runner=self.runner,
        )
        self.pool.set_completion_notifiers(
            wakeup=self._completion_wakeup, saturation=self._completion_saturation
        )
        self.worker.start()
        process = self.worker.provider.process
        if process is None:
            raise RuntimeError("worker provider did not start")
        # A process death cannot turn an admitted external effect into clean shutdown.
        self.journal.append("runtime", {"pid": process.pid, "uncertain": True})
        self._started = True

    async def _open_client(self) -> None:
        if self._loop_failed.is_set():
            raise RuntimeStoppedError("SDK loop stopped before client construction")
        # The optional controller profile is the only SDK import boundary.
        from agamemnon_client import AgamemnonClient, AgamemnonConfig

        self.client = AgamemnonClient(
            AgamemnonConfig(
                host="127.0.0.1", port=self._port, timeout=self._timeout, api_key=self._api_key
            ),
            trust_env=False,
        )

    async def _close_client(self) -> None:
        if self.client is not None and not self._client_closed:
            await self.client.aclose()
        self._client_closed = True

    def _pulse(self) -> None:
        self._pulse_at = time.monotonic()
        if self.loop is not None and not self._loop_stop.is_set():
            self.loop.call_later(0.05, self._pulse)

    async def _failed_loop_cleanup(self) -> None:
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.pool is not None:
            while not self._pool_finished.is_set():
                await asyncio.sleep(0.01)
            if self._pool_error is not None:
                raise RuntimeUncertainError("pool completion barrier failed") from self._pool_error
        await self._close_client()

    def _serve_loop(self) -> None:
        loop = self.loop
        if loop is None:
            self._loop_failed.set()
            self._loop_finished.set()
            return
        asyncio.set_event_loop(loop)
        loop.call_soon(self._loop_ready.set)
        loop.call_soon(self._pulse)
        try:
            loop.run_forever()
            if not self._loop_stop.is_set():
                self._loop_failed.set()
                if not loop.is_closed():
                    # Re-enter only for cleanup of this same loop and its actual tasks.
                    loop.run_until_complete(self._failed_loop_cleanup())
        except BaseException:
            self._loop_failed.set()
        finally:
            if not loop.is_closed():
                loop.close()
            self._loop_finished.set()

    def _call(self, operation: Coroutine[Any, Any, T]) -> T:
        loop = self.loop
        if self._loop_failed.is_set() or loop is None or not loop.is_running() or loop.is_closed():
            operation.close()
            raise RuntimeUncertainError("SDK loop cannot accept its owner operation")
        try:
            future = asyncio.run_coroutine_threadsafe(operation, loop)
        except BaseException:
            operation.close()
            raise
        try:
            return future.result(timeout=self._exit_bound)
        except FutureTimeout as error:
            # The operation may still run. Keep its client and journal until actual exit.
            self._loop_failed.set()
            raise RuntimeUncertainError("SDK owner operation did not acknowledge exit") from error

    def check_health(self) -> None:
        """Fence new control work when the SDK loop stops or stops making progress."""
        self._control_thread()
        if self._closed or self._closing:
            raise RuntimeStoppedError("runtime is closing")
        if self._completion_saturation.is_set():
            raise RuntimeUncertainError("runtime completion evidence could not be delivered")
        if time.monotonic() - self._pulse_at > self._exit_bound:
            self._loop_failed.set()
            raise RuntimeUncertainError("SDK loop did not acknowledge progress")
        if self._loop_failed.is_set() or self._loop_finished.is_set():
            raise RuntimeStoppedError("SDK loop stopped")
        if self.journal is not None:
            self.journal.require_writable()

    def _admission(self, submission: dict[str, Any], parent: dict[str, Any], source: Path) -> None:
        """Check a trusted attachment's build identity against the retained session."""
        self.check_health()
        if not self._started or self.worker is None or self.journal is None:
            raise RuntimeStoppedError("runtime is not ready")
        state = self.journal.snapshot()
        if state["draining"]:
            raise RuntimeStoppedError("worker is draining")
        fields(parent, "targetKind targetId sessionId executionId generation taskId agentId claim")
        claim = fields(
            parent["claim"], "schema targetKind targetId workerId agentId generation workspace"
        )
        session = state["sessions"].get(parent["sessionId"])
        if session is None:
            raise ValueError("build parent session is not admitted")
        if (
            session.get("released", False)
            or not session.get("admissionReserved", False)
            or session.get("activity") in {"unknown", "disconnected"}
            or not session.get("providerThreadId")
            or parent["targetKind"] not in {"sessions", "executions"}
            or parent["targetId"]
            != parent["sessionId" if parent["targetKind"] == "sessions" else "executionId"]
            or claim["schema"] != "hi/fleet/claim/v1"
            or claim["workerId"] != self.worker.identity["workerId"]
            or any(
                not equal(parent[name], session[name])
                for name in ("sessionId", "executionId", "generation", "taskId", "agentId")
            )
            or any(
                not equal(claim[name], parent[name])
                for name in ("targetKind", "targetId", "agentId", "generation")
            )
            or not source.is_absolute()
            or source.resolve(strict=True) != source
            or str(source) != session["workspace"]
            or claim["workspace"] != str(source)
            or not equal(
                submission["parent"],
                {
                    name: parent[name]
                    for name in ("targetKind", "targetId", "sessionId", "executionId", "generation")
                },
            )
        ):
            raise ValueError("build context differs from its admitted worker session")

    def register_build(
        self,
        context_id: str,
        *,
        repository: str,
        source: Path,
        submission: dict[str, Any],
        parent: dict[str, Any],
        snapshot: BuildSnapshot,
        source_lease: SourceLease,
        result_handoff: ResultHandoff,
    ) -> None:
        """Register server-owned admitted capabilities on the existing runtime.

        This is not a socket operation. The attachment must supply genuine
        source and evidence capabilities; a path or idle session is insufficient.
        """
        self._control_thread()
        context_id = identifier(context_id)
        self.check_health()
        saved = self._contexts.get(context_id)
        if saved is not None:
            if (
                saved.repository == repository
                and saved.source == source
                and equal(saved.submission, submission)
                and equal(saved.parent, parent)
                and saved.snapshot == snapshot
                and saved.source_lease == source_lease
                and saved.result_handoff == result_handoff
            ):
                self._admission(saved.submission, saved.parent, saved.source)
                return
            raise ValueError("build context identity cannot be rebound")
        if context_id in self._context_ids:
            raise ValueError("failed or retired build context identity cannot be reused")
        if len(self._context_ids) >= 4096:
            raise RuntimeStoppedError("runtime context identity capacity requires reconciliation")
        self._context_ids.add(context_id)
        copied_submission = dict(json.loads(encoded(submission)))
        copied_parent = dict(json.loads(encoded(parent)))
        self._admission(copied_submission, copied_parent, source)
        if (
            not isinstance(repository, str)
            or not repository
            or len(repository) > 1024
            or not callable(source_lease)
            or not callable(result_handoff)
        ):
            raise ValueError("build registration requires source and result capabilities")
        journal, runner = self.journal, self.runner
        if journal is None or runner is None:
            raise RuntimeStoppedError("runtime build resources are not ready")

        async def open_owner() -> FleetBuildOwner:
            if self._loop_failed.is_set():
                raise RuntimeStoppedError("SDK loop stopped before build registration")
            journal.require_writable()
            return FleetBuildOwner(
                FleetBuildService(self.client, copied_submission), journal, context_id
            )

        owner = self._call(open_owner())
        self.check_health()
        context = FleetBuildJobContext(
            owner=owner,
            repository=repository,
            source=source,
            submission=copied_submission,
            parent=copied_parent,
            snapshot=snapshot,
            source_lease=source_lease,
            result_handoff=result_handoff,
        )
        runner.register(context_id, context)
        self._contexts[context_id] = context

    def submit_build(self, job: BuildTestJob) -> JobHandle:
        """Submit one registered build while retaining its outstanding capacity."""
        self._control_thread()
        self.check_health()
        if not isinstance(job, BuildTestJob):
            raise ValueError("runtime accepts only registered build jobs")
        context_id = identifier(job.fleet_context_id)
        context = self._contexts.get(context_id)
        if context is None:
            raise ValueError("build context is not registered")
        self._admission(context.submission, context.parent, context.source)
        if self.worker is None or self.pool is None:
            raise RuntimeStoppedError("runtime build resources are not ready")
        if len(self._inflight) >= self.worker.capacity:
            raise RuntimeError("runtime build capacity is occupied")
        if context_id in self._inflight.values():
            raise RuntimeError("build context already has an outstanding job")
        handle = self.pool.submit(
            job,
            StageName.IMPLEMENTATION,
            claim_key=context.parent["taskId"],
            claim_stage="fleet-build",
        )
        self._inflight[handle] = context_id
        return handle

    def take_completion(self, *, timeout: float = 0) -> tuple[JobHandle, JobResult]:
        """Release admission capacity only after observing a real pool result."""
        self._control_thread()
        if self._completion_saturation.is_set():
            raise RuntimeUncertainError("runtime completion evidence could not be delivered")
        handle, result = self.completions.get(timeout=timeout)
        if handle not in self._inflight:
            raise RuntimeUncertainError("completion does not belong to an admitted runtime job")
        del self._inflight[handle]
        return handle, result

    def retire_build(self, context_id: str) -> None:
        """Retire an observed context without permitting a new identity binding."""
        self._control_thread()
        self.check_health()
        if context_id in self._inflight.values():
            raise RuntimeError("build context has an outstanding job")
        if context_id not in self._contexts or self.runner is None:
            raise ValueError("build context is not registered")
        self.runner.retire(context_id)
        del self._contexts[context_id]

    def _join_pool(self) -> None:
        try:
            if self.pool is None:
                raise RuntimeUncertainError("runtime did not retain its pool")
            self.pool.wait_for_exit()
        except BaseException as error:
            self._pool_error = error
        finally:
            self._pool_finished.set()

    def _wait_for_pool(self) -> None:
        if self.pool is None:
            return
        if self._pool_thread is None:
            self._pool_thread = threading.Thread(target=self._join_pool, name="fleet-pool-exit")
            self._pool_thread.start()
        failed_at: float | None = None
        while not self._pool_finished.wait(0.05):
            failed = self._loop_failed.is_set() or self._loop_finished.is_set()
            if failed:
                failed_at = time.monotonic() if failed_at is None else failed_at
            if time.monotonic() - self._pulse_at > self._exit_bound or (
                failed_at is not None and time.monotonic() - failed_at > self._exit_bound
            ):
                raise RuntimeUncertainError("pool borrowers did not acknowledge exit")
        self._pool_thread.join()
        if self._pool_error is not None:
            raise RuntimeUncertainError("pool completion barrier failed") from self._pool_error

    def _close_sdk_loop(self) -> None:
        """Close the SDK on its loop only after the pool exit barrier returns."""
        if self._loop_thread is not None:
            if not self._loop_finished.is_set():
                if self._loop_failed.is_set():
                    if not self._loop_finished.wait(self._exit_bound):
                        raise RuntimeUncertainError("failed SDK loop did not finish cleanup")
                else:
                    self._call(self._close_client())
                    self._loop_stop.set()
                    if self.loop is None:
                        raise RuntimeUncertainError("runtime did not retain its SDK loop")
                    self.loop.call_soon_threadsafe(self.loop.stop)
            self._loop_thread.join(self._exit_bound)
            if self._loop_thread.is_alive() or not self._client_closed:
                raise RuntimeUncertainError("SDK client closure was not confirmed")

    def close(self) -> None:
        """Wait for actual pool and SDK exit before releasing the shared journal."""
        self._control_thread()
        if self._closed:
            return
        self._closing = True
        self._wait_for_pool()
        if self._completion_saturation.is_set():
            raise RuntimeUncertainError("runtime completion evidence could not be delivered")
        self._close_sdk_loop()
        if self.worker is not None:
            try:
                self.worker.close()
            except BaseException as error:
                raise RuntimeUncertainError("worker cleanup could not be retained") from error
            if self.journal is not None and self.journal.snapshot()["runtime_uncertain"]:
                raise RuntimeUncertainError("worker provider cleanup was not confirmed")
        if self.journal is not None:
            self.journal.close()
        if self._directory_fd is not None:
            os.close(self._directory_fd)
            self._directory_fd = None
        self._closed = True

    def cleanup_owned_provider(self) -> None:
        """Attempt only provider cleanup while uncertain borrowers stay fenced."""
        self._control_thread()
        if self.worker is None:
            return
        cleanup = threading.Thread(target=self.worker.provider.close, daemon=True)
        cleanup.start()
        cleanup.join(8)
