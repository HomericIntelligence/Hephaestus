"""Own one granted build attempt without a default host execution path."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from hephaestus.automation.fleet_build_collection import FleetBuildEvidence
from hephaestus.automation.fleet_build_contract import (
    Json,
    digest,
    encoded,
    equal,
    fields,
    identifier,
    start_command,
    validate_command,
    validate_grant,
    validate_history_transition,
    validate_policy,
    validate_state,
)
from hephaestus.automation.fleet_build_executor import budget
from hephaestus.automation.fleet_build_storage import journal_directory, private_directory
from hephaestus.automation.fleet_journal import WorkerJournal
from hephaestus.automation.fleet_snapshot import SnapshotPolicy, restore_snapshot
from hephaestus.automation.fleet_snapshot_files import directory, private_parent


def remaining(deadline: float) -> float:
    """Keep one monotonic operation budget through all blocking capabilities."""
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("build deadline expired")
    return value


class BuildClient(Protocol):
    """Operator-owned authenticated control path, with no caller URL or secret."""

    def claim_run(self, build_id: str, claim: Json, *, deadline: float) -> Json:
        """Request the persisted claim's grant within the operation deadline."""
        ...

    def publish_fact(self, build_id: str, fact: Json, *, deadline: float) -> Json:
        """Send one retained terminal fact and return its controller response."""
        ...


class BuildExecutor(Protocol):
    """Qualified isolation owner; metadata alone cannot implement this contract.

    Prepare must verify actual allocation/isolation/limit state, excluding the
    supervisor journal and receipts from child write authority. Every method
    owns the same persisted lease. There is deliberately no default executor.
    """

    def prepare(self, lease: Json, *, deadline: float) -> Json:
        """Verify isolation and return the exact prepared lease before starting."""
        ...

    def run(
        self,
        lease: Json,
        *,
        argv: tuple[str, ...],
        deadline: float,
        shutdown: threading.Event,
        max_output_bytes: int,
    ) -> Json:
        """Run the fixed recipe once and return its bounded exit observation."""
        ...

    def dispose(self, lease: Json, *, deadline: float) -> Json:
        """Confirm that the exact lease has no remaining owned resources."""
        ...


class BuildResultPublisher(Protocol):
    """Retain actual owner evidence before a terminal fact leaves the supervisor."""

    def publish(
        self, command: Json, lease: Json, terminal: Json, *, deadline: float
    ) -> Json | None:
        """Create or verify this exact attempt's private result references."""
        ...


@runtime_checkable
class BuildResultReader(Protocol):
    """Keep actual published evidence locked through collection and exit checks."""

    def read_result(
        self,
        command: Json,
        lease: Json,
        terminal: Json,
        *,
        deadline: float,
        shutdown: threading.Event,
    ) -> AbstractContextManager[FleetBuildEvidence]:
        """Verify existing bytes without publication, repair or a new execution."""
        ...


def _handoff_budget(deadline: float, shutdown: threading.Event) -> float:
    if shutdown.is_set():
        raise InterruptedError("build result handoff interrupted")
    return budget(deadline)


def _same_fields(actual: Any, expected: Json) -> Json:
    if type(actual) is not dict or any(
        name not in actual or not equal(actual[name], value) for name, value in expected.items()
    ):
        raise ValueError("build result observation differs from retained authority")
    return cast(Json, actual)


def _result_observation(record: Json, state: Json) -> None:
    """Match a controller observation to the original acknowledged attempt."""
    command, terminal = state["command"], state["terminal"]
    payload, policy = command["payload"], command["payload"]["policy"]
    _same_fields(
        record,
        {
            "schema": "hi/fleet/v1",
            "kind": "build-jobs",
            "id": command["targetId"],
            "commandId": command["commandId"],
            "generation": command["generation"],
            "parent": payload["parent"],
            "status": terminal["outcome"],
            "collectionVerified": False,
        },
    )
    build = _same_fields(
        record.get("build"),
        {
            "schema": "hi/fleet/build/v1",
            "attempt": payload["attempt"],
            "policy": policy,
            "policyDigest": payload["policyDigest"],
            "parametersDigest": payload["parametersDigest"],
            "allocation": policy["allocation"],
            "snapshotWorkspace": payload["snapshotWorkspace"],
            "terminal": terminal,
        },
    )
    _same_fields(
        build.get("request"),
        {
            "schema": "hi/fleet/build-submit/v1",
            "workspaceId": policy["workspace"]["id"],
            "recipeId": policy["recipe"]["id"],
            "parameters": policy["recipe"]["parameters"],
            "snapshot": payload["snapshot"],
            "parent": {
                name: payload["parent"][name]
                for name in ("targetKind", "targetId", "sessionId", "executionId", "generation")
            },
        },
    )


def _retained_evidence(state: Json, root: Path) -> FleetBuildEvidence:
    command, lease, fact = state["command"], state["lease"], state["terminal"]
    payload = command["payload"]
    return FleetBuildEvidence(
        evidence_root=root,
        reference={"id": fact["receipt"]["reference"], "digest": fact["receipt"]["digest"]},
        expected={
            "buildId": command["targetId"],
            "attempt": payload["attempt"],
            "commandId": command["commandId"],
            "leaseId": lease["leaseId"],
            "parent": payload["parent"],
            "policy": payload["policy"],
            "policyDigest": payload["policyDigest"],
            "snapshot": payload["snapshot"],
        },
    )


@dataclass(frozen=True)
class BuildSnapshot:
    """Bind one operator-selected artifact to the real byte-verifying restore API."""

    artifact: Path
    policy: SnapshotPolicy

    def restore(self, commitment: Json, destination: Path, *, deadline: float) -> Path:
        """Restore into a new directory under the supervisor's exclusive lease."""
        return restore_snapshot(
            self.artifact,
            destination,
            commitment=commitment,
            policy=self.policy,
            timeout=min(30, remaining(deadline)),
        )


def _workspace_owner(workspace_root: Path, state_dir: Path) -> int:
    """Keep one private parent bound to its journal, including after restart."""
    descriptor = os.open(workspace_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("build workspace owner is not private")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        binding = os.open(
            ".build-owner", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=descriptor
        )
        with os.fdopen(binding, "r+b") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise ValueError("build workspace owner binding is invalid")
            identity = encoded(
                {"schema": "hi/hephaestus/build-owner/v1", "stateDir": str(state_dir)}
            )
            retained = stream.read(4097)
            if retained and retained != identity:
                raise ValueError("build workspace belongs to another journal owner")
            if not retained:
                stream.write(identity)
                stream.flush()
            os.fsync(stream.fileno())
            os.fsync(descriptor)
        return descriptor
    except BaseException as error:
        os.close(descriptor)
        if isinstance(error, BlockingIOError):
            raise RuntimeError("another build workspace owner is active") from error
        raise


def _prepare_storage(state_dir: Path, workspace_root: Path) -> None:
    """Require separate canonical storage and a private workspace parent."""
    for path in (state_dir, workspace_root):
        if not path.is_absolute() or path.resolve() != path:
            raise ValueError("build storage must be canonical")
    if state_dir.is_relative_to(workspace_root) or workspace_root.is_relative_to(state_dir):
        raise ValueError("build authority and child workspace overlap")
    with private_directory(workspace_root, time.monotonic() + 5):
        pass


class BuildSupervisor:
    """Journal one immutable attempt and refuse another start after uncertainty."""

    def __init__(
        self,
        *,
        state_dir: Path,
        workspace_root: Path,
        policy: Json,
        client: BuildClient,
        snapshot: BuildSnapshot,
        executor: BuildExecutor | None,
        publisher: BuildResultPublisher | None = None,
        claim_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        """Require explicit operator capabilities and separate private ownership."""
        if executor is None:
            raise ValueError("build isolation executor is unavailable")
        if snapshot is None or client is None:
            raise ValueError("build snapshot or controller capability is unavailable")
        self.policy = validate_policy(policy)
        if snapshot.policy.digest != self.policy["workspace"]["snapshotPolicyDigest"]:
            raise ValueError("snapshot capability has a different policy")
        _prepare_storage(state_dir, workspace_root)
        self.workspace_root = workspace_root
        self.client, self.snapshot, self.executor = client, snapshot, executor
        self.publisher = publisher
        self._claim_id_factory = claim_id_factory
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._active = False
        self._closed = False
        self._result_pin: object | None = None
        self._workspace_descriptor = _workspace_owner(workspace_root, state_dir)
        self._state: Json = {}
        try:
            with journal_directory(state_dir, time.monotonic() + 5):
                self.journal = WorkerJournal(state_dir)
            for record in self.journal.records:
                if record["kind"] == "build":
                    validate_history_transition(
                        self._state, record["value"], self.policy, self.workspace_root
                    )
                    self._state = record["value"]
            if self._state:
                self._state = validate_state(self._state, self.policy, workspace_root)
                if self._state["cancel"] is not None:
                    self._shutdown.set()
        except BaseException:
            if hasattr(self, "journal"):
                self.journal.close()
            os.close(self._workspace_descriptor)
            raise

    def __enter__(self) -> BuildSupervisor:
        """Retain journal ownership through the consumer's operation."""
        return self

    def __exit__(self, *_: Any) -> None:
        """Release ownership only after the synchronous operation returns."""
        self.close()

    def _save(self, **changes: Any) -> None:
        state = {**self._state, **changes}
        if not equal(state, self._state):
            validate_history_transition(self._state, state, self.policy, self.workspace_root)
            self.journal.append("build", state)
            self._state = json.loads(encoded(state))

    def status(self) -> Json:
        """Return bounded copied state without logs or controller credentials."""
        with self._lock:
            if not self._state:
                return {"status": "idle", "collectionVerified": False}
            phase = self._state["phase"]
            if self._state.get("reason"):
                phase = "reconciliation_required"
            elif self._state.get("terminal"):
                phase = self._state["terminal"]["outcome"]
            elif self._state.get("cancel"):
                phase = "cancelling"
            return {
                "status": phase,
                "buildId": self._state["command"]["targetId"],
                "attempt": self._state["command"]["payload"]["attempt"],
                "reason": self._state.get("reason", ""),
                "cleanup": self._state.get("cleanup"),
                "collectionVerified": False,
            }

    @contextmanager
    def _result_lock(self, deadline: float, shutdown: threading.Event) -> Iterator[None]:
        """Acquire only the short state lock within the caller's remaining budget."""
        _handoff_budget(deadline, shutdown)
        while not self._lock.acquire(blocking=False):
            shutdown.wait(min(0.05, _handoff_budget(deadline, shutdown)))
        try:
            _handoff_budget(deadline, shutdown)
            yield
        finally:
            self._lock.release()

    def _result_state(self) -> Json:
        """Read one healthy acknowledged journal state without giving it to a caller."""
        saved = self.journal.snapshot()
        if self._closed or saved["closed"] or saved["write_uncertain"] or self._active:
            raise RuntimeError("build result owner is unavailable or uncertain")
        retained = next(
            (row["value"] for row in reversed(saved["records"]) if row["kind"] == "build"),
            None,
        )
        if not retained or not equal(retained, self._state):
            raise RuntimeError("build result state differs from retained journal evidence")
        state = validate_state(retained, self.policy, self.workspace_root)
        terminal = state["terminal"]
        if (
            not state["published"]
            or state["cancel"] is not None
            or state["reason"]
            or state["cleanup"] != "confirmed_empty"
            or terminal is None
            or any(terminal[name] is None for name in ("receipt", "logs", "artifacts"))
        ):
            raise RuntimeError("build result requires acknowledged complete execution evidence")
        return state

    def _pin_result(
        self, record: Json, deadline: float, shutdown: threading.Event
    ) -> tuple[object, Json]:
        with self._result_lock(deadline, shutdown):
            self._require_no_result_reader()
            state = self._result_state()
            _result_observation(record, state)
            _handoff_budget(deadline, shutdown)
            self._result_pin = pin = object()
            return pin, state

    def _check_result_pin(
        self, pin: object, state: Json, deadline: float, shutdown: threading.Event
    ) -> None:
        with self._result_lock(deadline, shutdown):
            if self._result_pin is not pin or not equal(self._result_state(), state):
                raise RuntimeError("build result ownership changed during collection")

    def _release_result_pin(self, pin: object) -> None:
        # This cleanup allowance cannot extend successful result eligibility.
        if not self._lock.acquire(timeout=0.25):
            raise RuntimeError("build result cleanup is uncertain; retain the owner")
        try:
            if self._result_pin is not pin:
                raise RuntimeError("build result pin changed; retain the owner")
            self._result_pin = None
        finally:
            self._lock.release()

    def _require_no_result_reader(self) -> None:
        if self._result_pin is not None:
            raise RuntimeError("build result reader must exit before owner mutation or close")

    @contextmanager
    def result_handoff(
        self, record: Json, *, deadline: float, shutdown: threading.Event
    ) -> Iterator[FleetBuildEvidence]:
        """Pin acknowledged authority and actual private bytes through collection."""
        reader = self.publisher
        if not isinstance(reader, BuildResultReader):
            raise RuntimeError("build publisher has no explicit result-read capability")
        pin, state = self._pin_result(record, deadline, shutdown)
        try:
            with reader.read_result(
                json.loads(encoded(state["command"])),
                json.loads(encoded(state["lease"])),
                json.loads(encoded(state["terminal"])),
                deadline=deadline,
                shutdown=shutdown,
            ) as evidence:
                if not isinstance(evidence, FleetBuildEvidence):
                    raise ValueError("build reader returned no trusted evidence value")
                expected = _retained_evidence(state, evidence.evidence_root)
                if not equal(evidence.reference, expected.reference) or not equal(
                    evidence.expected, expected.expected
                ):
                    raise ValueError("build reader changed the acknowledged result identity")
                try:
                    yield expected
                finally:
                    self._check_result_pin(pin, state, deadline, shutdown)
        finally:
            self._release_result_pin(pin)

    def _adopt(self, command: Json) -> None:
        if self._state:
            if not equal(self._state["command"], command):
                raise ValueError("build command conflicts with the retained attempt")
            return
        allocation = self.policy["allocation"]
        claim = {
            "schema": "hi/fleet/build-claim/v1",
            "workerId": allocation["workerId"],
            "allocationId": allocation["id"],
            "generation": allocation["generation"],
            "attempt": command["payload"]["attempt"],
            "commandId": command["commandId"],
            "claimId": identifier(self._claim_id_factory()),
        }
        self._save(
            schema="hi/hephaestus/build-attempt/v1",
            command=command,
            claim=claim,
            grant=None,
            lease=None,
            terminal=None,
            cancel=None,
            cleanup=None,
            published=False,
            phase="grant_pending",
            reason="",
        )

    def handle(self, command: Json) -> Json:
        """Retain a start or cancel, then execute at most one owned attempt."""
        checked = validate_command(command, self.policy)
        with self._lock:
            if self._closed:
                raise RuntimeError("build supervisor is closed")
            self._require_no_result_reader()
            self._adopt(start_command(checked))
            if checked["operation"] == "cancel" and not self._accept_cancel(checked):
                return self.status()
            if self._active or self._state["published"]:
                return self.status()
            if not self._state.get("cancel") and self._state["phase"] not in {
                "grant_pending",
                "granted",
                "terminal",
            }:
                self._save(reason="owned_execution_requires_reconciliation")
                return self.status()
            self._active = True
        try:
            if self._state["terminal"] is None and not self._finish_cancel():
                self._execute()
            self._publish()
        except Exception:
            with self._lock:
                self._save(reason="build_effect_requires_reconciliation")
        finally:
            with self._lock:
                self._active = False
        return self.status()

    def _accept_cancel(self, command: Json) -> bool:
        if self._state["cancel"] is not None:
            if not equal(command, self._state["cancel"]):
                raise ValueError("build cancellation conflicts with retained identity")
        elif self._state["terminal"] is not None:
            if self._state["published"]:
                self._save(reason="terminal_cancellation_requires_controller_reconciliation")
                return False
            self._save(
                schema="hi/hephaestus/build-attempt/v2",
                supersededTerminal=self._state["terminal"],
                cancel=command,
                terminal=self._fact(command, outcome="cancelled", exit_code=None, logs=None),
                reason="",
            )
        else:
            self._save(cancel=command, reason="")
        self._shutdown.set()
        return True

    def _execute(self) -> None:
        command = self._state["command"]
        payload = command["payload"]
        resources = self.policy["recipe"]["resources"]
        deadline = time.monotonic() + resources["wallSeconds"]
        if self._state["grant"] is None:
            response = self.client.claim_run(
                command["targetId"], json.loads(encoded(self._state["claim"])), deadline=deadline
            )
            remaining(deadline)
            grant = validate_grant(response, command, self._state["claim"])
            with self._lock:
                self._save(grant=grant, phase="granted", reason="")
        if self._finish_cancel():
            return
        destination = self.workspace_root / payload["snapshotWorkspace"]
        workspace = self.snapshot.restore(payload["snapshot"], destination, deadline=deadline)
        for name, field in (("justfile", "recipeDigest"), ("uv.lock", "lockDigest")):
            path = workspace / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("restored recipe is not a regular file")
            if hashlib.sha256(path.read_bytes()).hexdigest() != self.policy["recipe"][field]:
                raise ValueError("restored recipe or lock bytes differ from policy")
        allocation = self.policy["allocation"]
        lease = {
            "schema": "hi/hephaestus/build-lease/v1",
            "leaseId": uuid.uuid4().hex,
            "buildId": command["targetId"],
            "attempt": payload["attempt"],
            "commandId": command["commandId"],
            "workerId": allocation["workerId"],
            "allocationId": allocation["id"],
            "generation": allocation["generation"],
            "policyDigest": payload["policyDigest"],
            "snapshotDigest": payload["snapshot"]["manifestDigest"],
            "workspace": str(workspace),
            "policy": self.policy,
        }
        with self._lock:
            cancelled = self._state.get("cancel") is not None
            if not cancelled:
                self._save(lease=lease, phase="preparing", reason="")
        if cancelled:
            self._finish_cancel()
            return
        prepared = self.executor.prepare(json.loads(encoded(lease)), deadline=deadline)
        if not equal(prepared, lease):
            raise ValueError("prepared build lease identity changed")
        remaining(deadline)
        with self._lock:
            cancelled = self._state.get("cancel") is not None
            if not cancelled:
                self._save(phase="starting")
        if cancelled:
            self._finish_cancel()
            return
        try:
            result = self.executor.run(
                json.loads(encoded(lease)),
                argv=("just", "test-unit"),
                deadline=deadline,
                shutdown=self._shutdown,
                max_output_bytes=resources["outputBytes"],
            )
        finally:
            # Cleanup has its own bounded budget; it cannot extend result eligibility.
            self._dispose()
        if self._finish_cancel():
            return
        self._retain_result(command, lease, result)

    def _retain_result(self, command: Json, lease: Json, result: Json) -> None:
        resources = self.policy["recipe"]["resources"]
        if result.get("outcome") == "timed_out":
            if result.get("exitCode") is not None:
                raise ValueError("build timeout cannot invent an exit code")
        elif (
            result.get("outcome") not in {"completed", "failed"}
            or type(result.get("exitCode")) is not int
            or not -255 <= result["exitCode"] <= 255
            or (result["outcome"] == "completed") != (result["exitCode"] == 0)
        ):
            raise ValueError("build exit observation is invalid")
        output = {name: result[name] for name in ("stdout", "stderr")}
        if (
            any(not isinstance(value, str) for value in output.values())
            or sum(len(value.encode()) for value in output.values()) > resources["outputBytes"]
        ):
            raise ValueError("build output exceeds its private bound")
        self._write_private("output.json", encoded(output))
        fact = self._fact(
            command,
            outcome=result["outcome"],
            exit_code=result["exitCode"],
            logs={"reference": "output-" + lease["leaseId"], "digest": digest(output)},
        )
        with self._lock:
            cancelled = self._state.get("cancel") is not None
            if not cancelled:
                self._save(phase="terminal", terminal=fact, reason="")
        if cancelled:
            self._finish_cancel()

    def _fact(
        self, command: Json, *, outcome: str, exit_code: int | None, logs: Json | None
    ) -> Json:
        payload = command["payload"]
        allocation = self.policy["allocation"]
        return {
            "schema": "hi/fleet/build-fact/v1",
            "eventId": "build-"
            + digest(
                {
                    "claim": self._state["claim"],
                    "commandId": command["commandId"],
                    "outcome": outcome,
                }
            ),
            "workerId": allocation["workerId"],
            "allocationId": allocation["id"],
            "generation": allocation["generation"],
            "attempt": payload["attempt"],
            "commandId": command["commandId"],
            "policyDigest": payload["policyDigest"],
            "parametersDigest": payload["parametersDigest"],
            "snapshotDigest": payload["snapshot"]["manifestDigest"],
            "platform": allocation["platform"],
            "imageDigest": allocation["imageDigest"],
            "toolchainDigest": allocation["toolchainDigest"],
            "outcome": outcome,
            "exitCode": exit_code,
            "cleanup": "confirmed_empty",
            "startFenced": True,
            "receipt": None,
            "logs": logs,
            "artifacts": None,
        }

    def _dispose(self) -> None:
        if self._state.get("cleanup") == "confirmed_empty":
            return
        lease = self._state["lease"]
        if lease is not None:
            cleanup = self.executor.dispose(
                json.loads(encoded(lease)), deadline=time.monotonic() + 5
            )
            if not equal(cleanup, {"leaseId": lease["leaseId"], "cleanup": "confirmed_empty"}):
                raise ValueError("owned build cleanup is unconfirmed")
        with self._lock:
            self._save(cleanup="confirmed_empty")

    def _finish_cancel(self) -> bool:
        if self._state.get("cancel") is None:
            return False
        self._dispose()
        with self._lock:
            fact = self._fact(self._state["cancel"], outcome="cancelled", exit_code=None, logs=None)
            self._save(phase="terminal", terminal=fact, reason="")
        return True

    def _write_private(self, name: str, data: bytes) -> None:
        with directory(self.journal.directory, time.monotonic() + 5) as owner:
            private_parent(os.fstat(owner))
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=owner,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(owner)

    def _publish(self) -> None:
        fact = self._state["terminal"]
        if fact["logs"] is not None and self._output_digest() != fact["logs"]["digest"]:
            raise ValueError("private build output differs from its retained digest")
        self._require_current_terminal(fact)
        if self.publisher is not None and fact["logs"] is not None:
            references = fields(
                self.publisher.publish(
                    json.loads(encoded(self._state["command"])),
                    json.loads(encoded(self._state["lease"])),
                    json.loads(encoded(fact)),
                    deadline=time.monotonic() + 5,
                ),
                "receipt logs artifacts",
            )
            if not equal(references["logs"], fact["logs"]) or (
                fact["receipt"] is not None
                and any(not equal(fact[name], references[name]) for name in references)
            ):
                raise ValueError("private result references changed during replay")
            with self._lock:
                self._require_current_terminal(fact)
                state = validate_state(
                    {**self._state, "terminal": {**fact, **references}},
                    self.policy,
                    self.workspace_root,
                )
                self._save(terminal=state["terminal"])
                fact = self._state["terminal"]
        self._require_current_terminal(fact)
        response = self.client.publish_fact(
            self._state["command"]["targetId"],
            json.loads(encoded(fact)),
            deadline=time.monotonic() + 5,
        )
        with self._lock:
            self._require_current_terminal(fact)
            if response.get("eventId") != fact["eventId"]:
                raise ValueError("terminal publication is not confirmed")
            self._save(published=True, reason="")

    def _require_current_terminal(self, fact: Json) -> None:
        with self._lock:
            if not equal(fact, self._state["terminal"]):
                raise RuntimeError("terminal callback was superseded by an accepted stop")

    def _output_digest(self) -> str:
        """Read bounded private bytes before their terminal reference is sent."""
        # JSON can use six bytes for one control character in captured output.
        limit = 6 * self.policy["recipe"]["resources"]["outputBytes"] + len(
            encoded({"stdout": "", "stderr": ""})
        )
        descriptor = os.open(
            self.journal.directory / "output.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_mode & 0o077
                or before.st_nlink != 1
                or before.st_size > limit
            ):
                raise ValueError("private build output ownership or bound changed")
            observed = hashlib.sha256()
            count = 0
            while chunk := stream.read(65536):
                count += len(chunk)
                if count > limit:
                    raise ValueError("private build output exceeds its bound")
                observed.update(chunk)
            after = os.fstat(stream.fileno())
            attributes = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if count != before.st_size or any(
                getattr(before, name) != getattr(after, name) for name in attributes
            ):
                raise ValueError("private build output changed during verification")
        return observed.hexdigest()

    def reconcile(self) -> Json:
        """Dispose the retained lease without inventing its lost exit result."""
        with self._lock:
            if self._closed:
                raise RuntimeError("build supervisor is closed")
            self._require_no_result_reader()
            if self._active or not self._state or self._state["terminal"] is not None:
                return self.status()
            self._active = True
        try:
            if self._finish_cancel():
                self._publish()
            elif self._state["lease"] is not None:
                self._dispose()
                with self._lock:
                    self._save(reason="owned_exit_requires_reconciliation")
        except Exception:
            with self._lock:
                self._save(reason="owned_cleanup_requires_reconciliation")
        finally:
            with self._lock:
                self._active = False
        return self.status()

    def close(self) -> None:
        """Keep ownership while an active operation still has journal access."""
        with self._lock:
            self._require_no_result_reader()
            if self._active:
                raise RuntimeError("active build must stop before journal release")
            if not self._closed:
                self._closed = True
                self.journal.close()
                os.close(self._workspace_descriptor)
