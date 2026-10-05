"""Run a registered build through borrowed Fleet resources and real collection."""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from collections.abc import Coroutine, Mapping
from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from hephaestus.automation.fleet_build_collection import (
    FleetBuildEvidence as FleetBuildEvidence,
    collect_build_result,
)
from hephaestus.automation.fleet_build_contract import (
    encoded,
    equal,
    identifier,
    validate_policy,
)
from hephaestus.automation.fleet_build_service import FleetBuildOwner
from hephaestus.automation.fleet_build_supervisor import BuildSnapshot
from hephaestus.automation.fleet_snapshot import verify_snapshot
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.jobs import BuildTestJob

Json = dict[str, Any]


def _copy(value: Json) -> Json:
    return dict(json.loads(encoded(value)))


def _remaining(deadline: float, shutdown: threading.Event) -> float:
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("build deadline must be finite")
    if shutdown.is_set():
        raise InterruptedError("build observation interrupted")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("build deadline expired")
    return remaining


class SourceLease(Protocol):
    """Keep actual source exclusion until the pool releases its resources."""

    def __call__(
        self, *, deadline: float, shutdown: threading.Event
    ) -> AbstractContextManager[None]:
        """Acquire actual source exclusion within the original job budget."""
        ...


class ResultHandoff(Protocol):
    """Retain actual evidence exclusion through collection and receipt capture."""

    def __call__(
        self, record: Json, *, deadline: float, shutdown: threading.Event
    ) -> AbstractContextManager[FleetBuildEvidence]:
        """Acquire real evidence exclusion and return the trusted publisher handoff."""
        ...


@dataclass(frozen=True)
class FleetBuildJobContext:
    """Register existing resources; caller-owned exclusion is a precondition."""

    owner: FleetBuildOwner
    repository: str
    source: Path
    submission: Json
    parent: Json
    snapshot: BuildSnapshot
    source_lease: SourceLease
    result_handoff: ResultHandoff

    def __post_init__(self) -> None:
        """Copy the immutable registration before any pool invocation."""
        object.__setattr__(self, "submission", _copy(self.submission))
        object.__setattr__(self, "parent", _copy(self.parent))


class _OwnerObservation:
    """Wait for one task on the supplied loop without owning that loop's lifetime."""

    def __init__(self, loop: asyncio.AbstractEventLoop, loop_failed: threading.Event) -> None:
        self.loop = loop
        self.loop_failed = loop_failed
        self.reply: Future[Json] = Future()
        self.finished = threading.Event()
        self.cancel_requested = threading.Event()
        self.task: asyncio.Task[Json] | None = None

    def complete(self, completed: asyncio.Task[Json]) -> None:
        """Acknowledge actual task exit, including cancellation before execution."""
        try:
            if completed.cancelled():
                self.reply.set_exception(InterruptedError("build observation detached"))
            else:
                self.reply.set_result(completed.result())
        except BaseException as error:
            self.reply.set_exception(error)
        finally:
            self.finished.set()

    def begin(self, owner: FleetBuildOwner, operation: str, deadline: float) -> None:
        """Start only the requested owner operation on its existing loop."""
        if self.loop_failed.is_set():
            self.reply.set_exception(RuntimeError("build owner loop has failed"))
            self.finished.set()
            return
        if self.cancel_requested.is_set():
            self.reply.set_exception(InterruptedError("build observation detached before send"))
            self.finished.set()
            return
        call: Coroutine[Any, Any, Json] = (
            owner.submit(deadline=deadline)
            if operation == "submit"
            else owner.status(deadline=deadline)
        )
        try:
            self.task = self.loop.create_task(call)
        except BaseException as error:
            call.close()
            self.reply.set_exception(error)
            self.finished.set()
            return
        self.task.add_done_callback(self.complete)

    def cancel(self) -> None:
        """Cancel local observation without sending a remote cancellation."""
        if self.task is not None:
            self.task.cancel()

    def wait(self, deadline: float, shutdown: threading.Event) -> Json:
        """Keep the supplied lifetime until the actual task completion callback."""
        try:
            while True:
                if not self.loop.is_running() or self.loop.is_closed():
                    self.loop_failed.set()
                remaining = _remaining(deadline, shutdown)
                try:
                    return self.reply.result(timeout=min(0.05, remaining))
                except FutureTimeout:
                    if self.reply.done():
                        return self.reply.result()
        finally:
            if not self.finished.is_set():
                self.cancel_requested.set()
                try:
                    self.loop.call_soon_threadsafe(self.cancel)
                except RuntimeError:
                    self.loop_failed.set()
                # Proxy cancellation is not task exit. The supplied lifetime
                # must keep this loop running until the actual callback arrives.
                while not self.finished.wait(0.05):
                    if not self.loop.is_running() or self.loop.is_closed():
                        self.loop_failed.set()


class FleetBuildJobRunner:
    """Borrow one owner loop without opening or closing any runtime resource.

    The lifetime owner must keep this loop and every journal writer serialized
    until pool work ends. This adapter does not supply that worker factory.
    """

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        contexts: Mapping[str, FleetBuildJobContext],
        loop_failed: threading.Event | None = None,
    ) -> None:
        """Copy registration before jobs start; never read ambient credentials."""
        self._loop = loop
        self._loop_failed = loop_failed if loop_failed is not None else threading.Event()
        self._registration_lock = threading.Lock()
        self._registered_ids = set(contexts)
        self._contexts = MappingProxyType(
            {name: replace(context) for name, context in contexts.items()}
        )

    def register(self, context_id: str, context: FleetBuildJobContext) -> None:
        """Copy a later owner-supplied context without permitting identity reuse."""
        context_id = identifier(context_id)
        with self._registration_lock:
            if context_id in self._registered_ids:
                raise ValueError("build context identity cannot be reused")
            self._registered_ids.add(context_id)
            copied = replace(context)
            if copied.owner.context_id != context_id or not equal(
                copied.owner.submission, copied.submission
            ):
                raise ValueError("build context differs from its retained owner")
            self._contexts = MappingProxyType({**self._contexts, context_id: copied})

    def retire(self, context_id: str) -> None:
        """Remove an idle registration while keeping its identity fenced."""
        with self._registration_lock:
            if context_id not in self._contexts:
                raise ValueError("build context is not registered")
            retained = dict(self._contexts)
            del retained[context_id]
            self._contexts = MappingProxyType(retained)

    def _context(self, job: BuildTestJob) -> FleetBuildJobContext:
        context_id = identifier(job.fleet_context_id)
        with self._registration_lock:
            context = self._contexts.get(context_id)
        if context is None:
            raise ValueError("build context is not registered")
        if (
            context.owner.context_id != context_id
            or not equal(context.owner.submission, context.submission)
            or job.repo != context.repository
            or job.argv != ("just", "test-unit")
            or job.immutable_source
            or job.verified_runner_source_revision is not None
            or not context.source.is_absolute()
            or context.source.resolve(strict=True) != context.source
            or job.cwd.resolve(strict=True) != context.source
            or not job.cwd.is_absolute()
            or context.parent["claim"]["workspace"] != str(context.source)
            or any(
                not equal(context.parent[name], value)
                for name, value in context.submission["parent"].items()
            )
            or (
                job.expected_head_sha
                and job.expected_head_sha != context.submission["snapshot"]["baseCommit"]
            )
        ):
            raise ValueError("build job differs from its registered source or assignment")
        return context

    def _call(
        self,
        owner: FleetBuildOwner,
        operation: str,
        deadline: float,
        shutdown: threading.Event,
    ) -> Json:
        """Wait for the actual owner task, including cancellation before it starts."""
        _remaining(deadline, shutdown)
        if self._loop_failed.is_set() or not self._loop.is_running() or self._loop.is_closed():
            self._loop_failed.set()
            raise RuntimeError("build owner loop is unavailable")
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is self._loop:
            raise RuntimeError("synchronous build runner cannot block its owner loop")
        observation = _OwnerObservation(self._loop, self._loop_failed)
        self._loop.call_soon_threadsafe(observation.begin, owner, operation, deadline)
        return observation.wait(deadline, shutdown)

    @staticmethod
    def _record(context: FleetBuildJobContext, record: Json) -> None:
        policy = validate_policy(record["build"]["policy"])
        if (
            not equal(record["parent"], context.parent)
            or not equal(record["build"]["request"], context.submission)
            or policy["workspace"]["repository"] != context.repository
            or policy["workspace"]["parentWorkspace"] != str(context.source)
        ):
            raise ValueError("build admission differs from its registered context")

    @staticmethod
    def _evidence(
        context: FleetBuildJobContext, record: Json, evidence: FleetBuildEvidence
    ) -> None:
        build = record["build"]
        expected = evidence.expected
        bound = {
            "buildId": record["id"],
            "attempt": build["attempt"],
            "commandId": record["id"] + "-start",
            "parent": record["parent"],
            "policy": build["policy"],
            "policyDigest": build["policyDigest"],
            "snapshot": context.submission["snapshot"],
        }
        if set(expected) != {*bound, "leaseId"} or any(
            not equal(expected[name], value) for name, value in bound.items()
        ):
            raise ValueError("trusted result handoff differs from the observed admission")
        receipt = build["terminal"]["receipt"]
        if not equal(evidence.reference, {"id": receipt["reference"], "digest": receipt["digest"]}):
            raise ValueError("private result reference differs from the terminal fact")

    def run(
        self,
        job: BuildTestJob,
        *,
        deadline: float,
        shutdown: threading.Event,
        resources: ExitStack,
    ) -> JobResult:
        """Use actual snapshot and result bytes; retain leases in the pool's scope."""
        try:
            _remaining(deadline, shutdown)
            context = self._context(job)
            resources.enter_context(context.source_lease(deadline=deadline, shutdown=shutdown))
            context = self._context(job)
            verify_snapshot(
                context.snapshot.artifact,
                commitment=context.submission["snapshot"],
                policy=context.snapshot.policy,
                timeout=min(30.0, _remaining(deadline, shutdown)),
            )
            _remaining(deadline, shutdown)
            record = self._call(context.owner, "submit", deadline, shutdown)
            self._record(context, record)
            terminal_states = {"completed", "failed", "timed_out", "cancelled"}
            while record["status"] not in terminal_states:
                _remaining(deadline, shutdown)
                record = self._call(context.owner, "status", deadline, shutdown)
                self._record(context, record)
                if record["status"] not in terminal_states:
                    shutdown.wait(min(0.05, _remaining(deadline, shutdown)))
            evidence = resources.enter_context(
                context.result_handoff(record, deadline=deadline, shutdown=shutdown)
            )
            self._evidence(context, record, evidence)
            _remaining(deadline, shutdown)
            collection = collect_build_result(
                evidence.evidence_root,
                reference=evidence.reference,
                expected=evidence.expected,
                source=context.source,
                snapshot_policy=context.snapshot.policy,
                deadline=deadline,
            )
            _remaining(deadline, shutdown)
            ok = (
                collection["status"] == "verified_current"
                and collection["sourceCurrent"] is True
                and collection["outcome"] == "completed"
                and type(collection["exitCode"]) is int
                and collection["exitCode"] == 0
            )
            return JobResult(
                ok=ok, value=collection, error=None if ok else "fleet_build_not_current_success"
            )
        except InterruptedError:
            return JobResult(ok=False, error="interrupted", interrupted=True)
        except TimeoutError:
            return JobResult(ok=False, error="timeout")
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            return JobResult(ok=False, error=f"fleet_build_refused: {type(error).__name__}")
