"""Journal one exact scheduler step without supplying an execution fallback."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, cast

from hephaestus.automation.fleet_build_contract import (
    Json,
    digest,
    encoded,
    equal,
    fields,
    identifier,
    integer,
    sha,
    text,
    validate_policy,
)
from hephaestus.automation.fleet_build_storage import journal_directory
from hephaestus.automation.fleet_journal import WorkerJournal
from hephaestus.automation.fleet_snapshot_files import directory, private_parent, publish
from hephaestus.automation.fleet_snapshot_policy import valid_path


class StepTransport(Protocol):
    """Observe and control one operator-bound allocation through its trusted owner."""

    def start(self, lease: Json, nonce: str, *, deadline: float) -> Json:
        """Start a gated step, returning its independently obtained identity."""
        ...

    def find(self, nonce: str, *, deadline: float) -> list[Json]:
        """Observe existing steps for the exact retained launch nonce."""
        ...

    def release(
        self,
        identity: Json,
        *,
        argv: tuple[str, ...],
        deadline: float,
        shutdown: threading.Event,
        max_output_bytes: int,
    ) -> Json:
        """Release the fixed recipe and own its bounded output and process lifetime."""
        ...

    def cancel(self, identity: Json, *, deadline: float) -> None:
        """Cancel only this exact step, never its enclosing allocation."""
        ...

    def observe(self, identity: Json, *, deadline: float) -> Json:
        """Read matching scheduler terminal and execution-node kernel observations."""
        ...


def budget(deadline: float) -> float:
    """Keep every operation within one finite caller-owned monotonic deadline."""
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("build deadline must be finite")
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("build deadline expired")
    return value


def validate_allocation(value: Json) -> Json:
    """Validate an immutable observation binding without claiming live qualification."""
    fields(
        value,
        "schema allocationId generation qualificationReceiptDigest cluster "
        "jobId jobStart node uid bootId",
    )
    if value["schema"] != "hi/hephaestus/slurm-allocation/v1":
        raise ValueError("invalid scheduler allocation schema")
    identifier(value["allocationId"])
    integer(value["generation"])
    integer(value["uid"], minimum=0)
    sha(value["qualificationReceiptDigest"])
    if (
        not isinstance(value["jobId"], str)
        or re.fullmatch(r"[1-9][0-9]{0,18}", value["jobId"]) is None
    ):
        raise ValueError("invalid scheduler job identity")
    for name in ("cluster", "node", "jobStart", "bootId"):
        text(value[name])
    return cast(Json, json.loads(encoded(value)))


def validate_step(value: Json, binding: Json, lease_id: str, nonce: str) -> Json:
    """Require the exact allocation incarnation, lease and gated-step nonce."""
    fields(value, "schema allocation leaseId launchNonce stepId stepStart cgroup")
    if (
        value["schema"] != "hi/hephaestus/slurm-step/v1"
        or not equal(value["allocation"], binding)
        or value["leaseId"] != lease_id
        or value["launchNonce"] != nonce
        or not isinstance(value["stepId"], str)
        or re.fullmatch(r"0|[1-9][0-9]{0,18}", value["stepId"]) is None
    ):
        raise ValueError("scheduler step identity changed")
    validate_allocation(binding)
    sha(lease_id, 32)
    identifier(nonce)
    text(value["stepStart"])
    text(value["cgroup"])
    return cast(Json, json.loads(encoded(value)))


def _lease(value: Json, binding: Json, state_dir: Path) -> Json:
    fields(
        value,
        "schema leaseId buildId attempt commandId workerId allocationId generation "
        "policyDigest snapshotDigest workspace policy",
    )
    policy = validate_policy(value["policy"])
    allocation = policy["allocation"]
    sha(value["leaseId"], 32)
    identifier(value["buildId"])
    integer(value["attempt"])
    integer(value["generation"])
    sha(value["snapshotDigest"])
    workspace = Path(text(value["workspace"]))
    if (
        value["schema"] != "hi/hephaestus/build-lease/v1"
        or value["commandId"] != value["buildId"] + "-start"
        or value["policyDigest"] != digest(policy)
        or value["workerId"] != allocation["workerId"]
        or value["allocationId"] != allocation["id"]
        or not equal(value["generation"], allocation["generation"])
        or binding["allocationId"] != allocation["id"]
        or not equal(binding["generation"], allocation["generation"])
        or binding["qualificationReceiptDigest"] != allocation["qualificationReceiptDigest"]
        or not workspace.is_absolute()
        or workspace.resolve() != workspace
        or workspace.is_relative_to(state_dir)
        or state_dir.is_relative_to(workspace)
    ):
        raise ValueError("build lease differs from its immutable allocation policy")
    return cast(Json, json.loads(encoded(value)))


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_uid,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def read_private_file(
    root: Path,
    name: str,
    *,
    max_bytes: int,
    deadline: float,
    retain: bool = False,
) -> tuple[bytes, str, int]:
    """Hash stable regular bytes through private no-follow directory descriptors."""
    budget(deadline)
    if not valid_path(name) or type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("invalid bounded private file selection")
    with directory(root, deadline) as root_fd:
        private_parent(os.fstat(root_fd))
        descriptors: list[int] = []
        bindings: list[tuple[int, str, tuple[int, ...]]] = []
        parent = root_fd
        try:
            parts = name.split("/")
            for part in parts[:-1]:
                budget(deadline)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                descriptors.append(child)
                metadata = os.fstat(child)
                private_parent(metadata)
                bindings.append((parent, part, _file_identity(metadata)))
                parent = child
            descriptor = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=parent,
            )
            descriptors.append(descriptor)
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_nlink != 1
                or before.st_size > max_bytes
            ):
                raise RuntimeError("private file ownership, type or size is invalid")
            content = bytearray()
            hashed = hashlib.sha256()
            count = 0
            while True:
                budget(deadline)
                block = os.read(descriptor, min(65536, max_bytes - count + 1))
                if not block:
                    break
                count += len(block)
                if count > max_bytes:
                    raise RuntimeError("private file exceeds its bound")
                hashed.update(block)
                if retain:
                    content.extend(block)
            if (
                count != before.st_size
                or _file_identity(before) != _file_identity(os.fstat(descriptor))
                or _file_identity(before)
                != _file_identity(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False))
            ):
                raise RuntimeError("private file changed during collection")
            for ancestor, part, identity in bindings:
                if (
                    _file_identity(os.stat(part, dir_fd=ancestor, follow_symlinks=False))
                    != identity
                ):
                    raise RuntimeError("private directory changed during collection")
            private_parent(os.fstat(root_fd))
            return bytes(content), hashed.hexdigest(), count
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


def _result(value: Json, maximum: int) -> Json:
    fields(value, "outcome exitCode stdout stderr")
    outcome, code = value["outcome"], value["exitCode"]
    if outcome == "timed_out":
        if code is not None:
            raise ValueError("timeout cannot invent an exit code")
    elif (
        outcome not in ("completed", "failed")
        or type(code) is not int
        or not -255 <= code <= 255
        or (outcome == "completed") != (code == 0)
    ):
        raise ValueError("invalid owned step exit observation")
    if any(not isinstance(value[name], str) for name in ("stdout", "stderr")):
        raise ValueError("invalid owned step output")
    if sum(len(value[name].encode()) for name in ("stdout", "stderr")) > maximum:
        raise ValueError("owned step output exceeds its bound")
    return cast(Json, json.loads(encoded(value)))


class SlurmStepOwner:
    """Retain one irreversible step attempt through an explicit trusted transport."""

    def __init__(self, *, state_dir: Path, binding: Json, transport: StepTransport) -> None:
        """Bind private journal ownership without contacting a scheduler."""
        self._binding = validate_allocation(binding)
        if transport is None:
            raise ValueError("an explicit scheduler transport is required")
        if not state_dir.is_absolute() or state_dir.resolve() != state_dir:
            raise ValueError("step state directory must be canonical")
        self._root, self._transport = state_dir, transport
        self._lock = threading.Lock()
        self._closed = False
        self._state: Json | None = None
        deadline = time.monotonic() + 5
        journal: WorkerJournal | None = None
        try:
            with directory(state_dir.parent, deadline) as parent:
                private_parent(os.fstat(parent))
            with journal_directory(state_dir, deadline) as descriptor:
                metadata = os.fstat(descriptor)
                self._root_identity = (metadata.st_dev, metadata.st_ino)
                for name in ("writer.lock", "receipts.jsonl"):
                    try:
                        item = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if (
                        not stat.S_ISREG(item.st_mode)
                        or item.st_uid != os.geteuid()
                        or item.st_nlink != 1
                        or (
                            name == "receipts.jsonl"
                            and (
                                stat.S_IMODE(item.st_mode) != 0o600
                                or item.st_size > 64 * 1024 * 1024
                            )
                        )
                    ):
                        raise RuntimeError("invalid step journal ownership")
                journal = WorkerJournal(state_dir)
                self._journal = journal
            raw, _, _ = read_private_file(
                state_dir,
                "receipts.jsonl",
                max_bytes=64 * 1024 * 1024,
                deadline=deadline,
                retain=True,
            )
            expected = b"".join(
                (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
                for record in self._journal.records
            )
            if raw != expected:
                raise ValueError("step journal is not canonical")
            for record in self._journal.records:
                fields(record, "kind value")
                if record["kind"] != "build_step":
                    raise ValueError("foreign step journal record")
                state = self._validate(record["value"])
                self._transition(self._state, state)
                self._state = state
        except BaseException:
            if journal is not None:
                journal.close()
            raise

    def _validate(self, value: Json) -> Json:
        names = "schema binding lease launchNonce phase step result fenced cleanup"
        if isinstance(value, dict) and value.get("schema") == "hi/hephaestus/build-step-owner/v2":
            names += " cleanupObservation"
        fields(value, names)
        if (
            value["schema"]
            not in ("hi/hephaestus/build-step-owner/v1", "hi/hephaestus/build-step-owner/v2")
            or not equal(value["binding"], self._binding)
            or value["phase"]
            not in ("reserved", "start_intent", "step_owned", "release_intent", "terminal")
            or type(value["fenced"]) is not bool
            or value["cleanup"] not in (None, "confirmed_empty")
        ):
            raise ValueError("invalid retained scheduler ownership")
        _lease(value["lease"], self._binding, self._root)
        sha(value["launchNonce"], 32)
        if value["step"] is not None:
            validate_step(
                value["step"], self._binding, value["lease"]["leaseId"], value["launchNonce"]
            )
        if (
            (value["phase"] in ("reserved", "start_intent")) != (value["step"] is None)
            or (value["phase"] == "terminal") != (value["result"] is not None)
            or (value["cleanup"] is not None and not value["fenced"])
            or (value["cleanup"] is not None and value["phase"] == "start_intent")
        ):
            raise ValueError("retained scheduler phase is inconsistent")
        if value["result"] is not None:
            fields(value["result"], "digest bytes")
            sha(value["result"]["digest"])
            integer(value["result"]["bytes"])
        observation = value.get("cleanupObservation")
        if observation is not None:
            fields(observation, "step schedulerTerminal kernelEmpty")
            if (
                value["cleanup"] != "confirmed_empty"
                or value["step"] is None
                or not equal(observation["step"], value["step"])
                or observation["schedulerTerminal"] is not True
                or observation["kernelEmpty"] is not True
            ):
                raise ValueError("retained cleanup observation changed identity")
        return cast(Json, json.loads(encoded(value)))

    @staticmethod
    def _transition(previous: Json | None, current: Json) -> None:
        if previous is None:
            if (
                current["phase"] != "reserved"
                or current["fenced"]
                or current["cleanup"] is not None
            ):
                raise ValueError("step journal starts after an unrecorded effect")
            return
        phases = ("reserved", "start_intent", "step_owned", "release_intent", "terminal")
        if (
            any(
                not equal(previous[name], current[name])
                for name in ("lease", "binding", "launchNonce")
            )
            or phases.index(current["phase"])
            not in (phases.index(previous["phase"]), phases.index(previous["phase"]) + 1)
            or (previous["fenced"] and not current["fenced"])
            or (
                previous["fenced"]
                and current["phase"] != previous["phase"]
                and (previous["phase"], current["phase"]) != ("start_intent", "step_owned")
            )
            or (previous["cleanup"] is not None and current["phase"] != previous["phase"])
            or (previous["cleanup"] is not None and current["cleanup"] != previous["cleanup"])
            or any(
                previous[name] is not None and not equal(previous[name], current[name])
                for name in ("step", "result")
            )
            or (
                previous["schema"] == "hi/hephaestus/build-step-owner/v2"
                and current["schema"] != previous["schema"]
            )
            or (
                previous.get("cleanupObservation") is not None
                and not equal(previous["cleanupObservation"], current.get("cleanupObservation"))
            )
        ):
            raise ValueError("retained scheduler identity or effect order changed")

    def _save(self, **changes: object) -> None:
        if self._state is None:
            raise RuntimeError("step journal has no retained admission")
        state = self._validate(
            {
                **self._state,
                "schema": "hi/hephaestus/build-step-owner/v2",
                "cleanupObservation": self._state.get("cleanupObservation"),
                **changes,
            }
        )
        self._transition(self._state, state)
        self._journal.append("build_step", state)
        self._state = state

    @contextmanager
    def _operation(self, deadline: float) -> Iterator[None]:
        if not self._lock.acquire(timeout=budget(deadline)):
            raise TimeoutError("step owner is busy")
        try:
            if self._closed:
                raise RuntimeError("step owner is closed")
            with directory(self._root, deadline) as descriptor:
                metadata = os.fstat(descriptor)
                private_parent(metadata)
                if (metadata.st_dev, metadata.st_ino) != self._root_identity:
                    raise RuntimeError("step journal directory changed")
                yield
        finally:
            self._lock.release()

    def _adopt(self, lease: Json) -> None:
        checked = _lease(lease, self._binding, self._root)
        if self._state is not None:
            if not equal(checked, self._state["lease"]):
                raise ValueError("step lease identity conflicts with its retained admission")
            return
        state = self._validate(
            {
                "schema": "hi/hephaestus/build-step-owner/v2",
                "binding": self._binding,
                "lease": checked,
                "launchNonce": uuid.uuid4().hex,
                "phase": "reserved",
                "step": None,
                "result": None,
                "fenced": False,
                "cleanup": None,
                "cleanupObservation": None,
            }
        )
        self._journal.append("build_step", state)
        self._state = state

    def _output(self, deadline: float) -> Json:
        if self._state is None or self._state["result"] is None:
            raise RuntimeError("step journal has no retained output")
        maximum = self._state["lease"]["policy"]["recipe"]["resources"]["outputBytes"]
        data, hashed, size = read_private_file(
            self._root / "output",
            "result.json",
            max_bytes=maximum * 6 + 4096,
            deadline=deadline,
            retain=True,
        )
        if not equal(self._state["result"], {"digest": hashed, "bytes": size}):
            raise RuntimeError("retained step output changed")
        result = _result(json.loads(data), maximum)
        if encoded(result) != data:
            raise ValueError("step output is not canonical")
        return result

    def run(
        self,
        lease: Json,
        *,
        argv: tuple[str, ...],
        deadline: float,
        shutdown: threading.Event,
        max_output_bytes: int,
    ) -> Json:
        """Start and release once; any uncertain boundary permits reconciliation only."""
        with self._operation(deadline):
            self._adopt(lease)
            if self._state is None:
                raise RuntimeError("step admission was not retained")
            lease = json.loads(encoded(self._state["lease"]))
            maximum = lease["policy"]["recipe"]["resources"]["outputBytes"]
            if (
                type(argv) is not tuple
                or argv != ("just", "test-unit")
                or integer(max_output_bytes) > maximum
            ):
                raise ValueError("fixed build command or output budget changed")
            if shutdown.is_set():
                self._save(fenced=True)
                raise InterruptedError("step start is cancelled")
            if self._state["phase"] == "terminal":
                return _result(self._output(deadline), max_output_bytes)
            if self._state["fenced"] or self._state["phase"] != "reserved":
                raise RuntimeError("owned step requires reconciliation")
            self._save(phase="start_intent")
            try:
                step = self._transport.start(
                    json.loads(encoded(lease)), self._state["launchNonce"], deadline=deadline
                )
                step = validate_step(
                    step, self._binding, lease["leaseId"], self._state["launchNonce"]
                )
                self._save(step=step, phase="step_owned")
                budget(deadline)
                if shutdown.is_set():
                    self._save(fenced=True)
                    raise InterruptedError("step release is cancelled")
                self._save(phase="release_intent")
                result = self._transport.release(
                    json.loads(encoded(step)),
                    argv=argv,
                    deadline=deadline,
                    shutdown=shutdown,
                    max_output_bytes=max_output_bytes,
                )
                budget(deadline)
                result = _result(result, max_output_bytes)
                data = encoded(result)
                publish(self._root / "output", {"result.json": (data, 0o600)}, deadline)
                with directory(self._root / "output", deadline) as output_fd:
                    descriptor = os.open(
                        "result.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=output_fd
                    )
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    os.fsync(output_fd)
                with directory(self._root, deadline) as root_fd:
                    os.fsync(root_fd)
                self._save(
                    phase="terminal",
                    result={"digest": hashlib.sha256(data).hexdigest(), "bytes": len(data)},
                )
                return self._output(deadline)
            except (InterruptedError, TimeoutError):
                raise
            except Exception as error:
                raise RuntimeError("owned step requires reconciliation") from error

    def _observed(self, step: Json, deadline: float) -> Json | None:
        value = self._transport.observe(json.loads(encoded(step)), deadline=deadline)
        budget(deadline)
        fields(value, "step schedulerTerminal kernelEmpty")
        if (
            not equal(value["step"], step)
            or type(value["schedulerTerminal"]) is not bool
            or (value["kernelEmpty"] is not None and type(value["kernelEmpty"]) is not bool)
        ):
            raise RuntimeError("owned cleanup observation changed identity")
        if value["schedulerTerminal"] is True and value["kernelEmpty"] is True:
            return cast(Json, json.loads(encoded(value)))
        return None

    def dispose(self, lease: Json, *, deadline: float) -> Json:
        """Fence release and verify exact scheduler plus kernel disposal observations."""
        with self._operation(deadline):
            if self._state is None or not equal(
                _lease(lease, self._binding, self._root), self._state["lease"]
            ):
                raise RuntimeError("cleanup has no retained lease")
            lease = json.loads(encoded(self._state["lease"]))
            self._save(fenced=True)
            if self._state["phase"] == "reserved":
                self._save(cleanup="confirmed_empty")
                return {"leaseId": lease["leaseId"], "cleanup": "confirmed_empty"}
            if self._state["step"] is None:
                found = self._transport.find(self._state["launchNonce"], deadline=deadline)
                budget(deadline)
                if not isinstance(found, list) or len(found) != 1:
                    raise RuntimeError("owned cleanup requires reconciliation")
                step = validate_step(
                    found[0], self._binding, lease["leaseId"], self._state["launchNonce"]
                )
                self._save(step=step, phase="step_owned")
            step = self._state["step"]
            observation = self._observed(step, deadline)
            if observation is None:
                self._transport.cancel(json.loads(encoded(step)), deadline=deadline)
                observation = self._observed(step, deadline)
                if observation is None:
                    raise RuntimeError("owned cleanup is not confirmed")
            self._save(cleanup="confirmed_empty", cleanupObservation=observation)
            return {"leaseId": lease["leaseId"], "cleanup": "confirmed_empty"}

    def evidence(self, lease: Json, *, deadline: float) -> Json:
        """Read actual retained result and cleanup without another execution."""
        with self._operation(deadline):
            if (
                self._state is None
                or not equal(_lease(lease, self._binding, self._root), self._state["lease"])
                or self._state.get("cleanupObservation") is None
            ):
                raise RuntimeError("owned result has no retained cleanup observation")
            return {
                "schema": "hi/hephaestus/build-step-result/v1",
                "lease": json.loads(encoded(self._state["lease"])),
                "cleanup": json.loads(encoded(self._state["cleanupObservation"])),
                "result": self._output(deadline),
            }

    def close(self) -> None:
        """Release the local journal only; retained remote uncertainty stays durable."""
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("step owner still has an active operation")
        try:
            if not self._closed:
                self._journal.close()
                self._closed = True
        finally:
            self._lock.release()
