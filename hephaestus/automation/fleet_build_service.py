"""Submit one retained build intent through the ordinary controller API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import uuid4
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from agamemnon_client import AgamemnonClient

    from .fleet_journal import WorkerJournal

Json = dict[str, Any]


def _encoded(value: Any) -> str:
    """Retain JSON types when comparing identities and policy commitments."""
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("invalid build JSON") from error


def _same(actual: Any, expected: Any) -> None:
    if _encoded(actual) != _encoded(expected):
        raise ValueError("build identity does not match retained intent")


def _object(value: Any, fields: set[str] | None = None) -> Json:
    if type(value) is not dict or (fields is not None and set(value) != fields):
        raise ValueError("build object has missing or unknown fields")
    return value


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ValueError("invalid build identifier")
    return value


def _integer(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ValueError("build quantity must be a positive exact integer")
    return value


def _hash(value: Any, length: int = 64) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value) is None:
        raise ValueError("invalid build digest")


def _digest(value: Any) -> str:
    return hashlib.sha256(_encoded(value).encode()).hexdigest()


def _remaining(deadline: float) -> float:
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("build deadline must be finite")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("build deadline expired")
    return remaining


def _submission(value: Json) -> str:
    _object(
        value,
        {"schema", "workspaceId", "recipeId", "parameters", "idempotencyKey", "parent", "snapshot"},
    )
    _same(value["schema"], "hi/fleet/build-submit/v1")
    _same(value["recipeId"], "hephaestus-test-unit-v1")
    _same(value["parameters"], {})
    for name in ("workspaceId", "recipeId", "idempotencyKey"):
        _identifier(value[name])
    parent = _object(
        value["parent"], {"targetKind", "targetId", "sessionId", "executionId", "generation"}
    )
    if parent["targetKind"] not in ("sessions", "executions"):
        raise ValueError("build parent must be a session or execution")
    for name in ("targetId", "sessionId", "executionId"):
        _identifier(parent[name])
    _integer(parent["generation"])
    snapshot = _object(
        value["snapshot"],
        {"reference", "manifestDigest", "baseCommit", "members", "bytes", "policyDigest"},
    )
    _identifier(snapshot["reference"])
    _hash(snapshot["manifestDigest"])
    _hash(snapshot["policyDigest"])
    _hash(snapshot["baseCommit"], 40)
    _integer(snapshot["members"])
    _integer(snapshot["bytes"])
    encoded = _encoded(value)
    if len(encoded.encode()) > 16384:
        raise ValueError("build submission exceeds its byte limit")
    return encoded


def _cancellation(value: Json) -> Json:
    _object(value, {"schema", "commandId", "idempotencyKey", "generation", "attempt"})
    _same(value["schema"], "hi/fleet/build-cancel/v1")
    _identifier(value["commandId"])
    _identifier(value["idempotencyKey"])
    _integer(value["generation"])
    _same(_integer(value["attempt"]), 1)
    return _object(json.loads(_encoded(value)))


class FleetBuildService:
    """Use the ordinary SDK for one immutable caller-owned submission.

    The caller owns the SDK lifetime and durable intent. It must retain the
    submission before submit and the complete cancellation before cancel.
    This facade does not open a journal or acquire execution authority.
    """

    def __init__(self, client: AgamemnonClient, submission: dict[str, Any]) -> None:
        """Freeze the bounded request without sending it or reading credentials."""
        self._submission = _submission(submission)
        self._client = client
        self._build_id = "build-" + _digest(
            {name: submission[name] for name in ("workspaceId", "idempotencyKey")}
        )

    @property
    def build_id(self) -> str:
        """Return the build identity retained before submission."""
        return self._build_id

    @property
    def submission(self) -> dict[str, Any]:
        """Return a copy of the retained submission."""
        return _object(json.loads(self._submission))

    async def _call(self, operation: Callable[[], Awaitable[Json]], deadline: float) -> Json:
        async with asyncio.timeout(_remaining(deadline)):
            return await operation()

    def _record(self, value: Any) -> Json:
        """Bind immutable identity while accepting changing controller lifecycle fields."""
        try:
            record = _object(value)
            _same(record["schema"], "hi/fleet/v1")
            _same(record["kind"], "build-jobs")
            _same(record["id"], self.build_id)
            _same(record["collectionVerified"], False)
            if not isinstance(record["status"], str) or not 1 <= len(record["status"]) <= 64:
                raise ValueError("invalid build status")
            build = _object(record["build"])
            _same(build["schema"], "hi/fleet/build/v1")
            _same(build["request"], self.submission)
            _same(_integer(build["attempt"]), 1)
            parent = _object(record["parent"])
            for key, item in self.submission["parent"].items():
                _same(parent[key], item)
            policy = _object(build["policy"], {"workspace", "recipe", "allocation"})
            _same(build["policyDigest"], _digest(policy))
            _same(build["parametersDigest"], _digest({}))
            allocation = _object(build["allocation"])
            _same(allocation, policy["allocation"])
            _same(_integer(record["generation"]), _integer(allocation["generation"]))
            _identifier(allocation["workerId"])
            _same(policy["workspace"]["id"], self.submission["workspaceId"])
            _same(
                policy["workspace"]["snapshotPolicyDigest"],
                self.submission["snapshot"]["policyDigest"],
            )
            _same(policy["recipe"]["id"], self.submission["recipeId"])
            _same(policy["recipe"]["parameters"], {})
            _same(policy["recipe"]["argv"], ["just", "test-unit"])
            _same(policy["recipe"]["platform"], "linux/aarch64")
            for key in ("platform", "imageDigest", "toolchainDigest"):
                _same(policy["recipe"][key], allocation[key])
            _same(build["snapshotWorkspace"], self.build_id + "-attempt-1")
            return _object(json.loads(_encoded(record)))
        except (KeyError, TypeError) as error:
            raise ValueError("invalid build record") from error

    def _response(self, value: Any, cancellation: Json | None = None) -> Json:
        try:
            response = _object(value, {"record", "command"})
            record = self._record(response["record"])
            build = record["build"]
            start_id = self.build_id + "-start"
            payload = {
                "schema": "hi/fleet/build-command/v1",
                "attempt": build["attempt"],
                "parent": record["parent"],
                "policy": build["policy"],
                "policyDigest": build["policyDigest"],
                "parametersDigest": build["parametersDigest"],
                "snapshot": self.submission["snapshot"],
                "snapshotWorkspace": build["snapshotWorkspace"],
                "requiresRunGrant": True,
            }
            command = {
                "schema": "hi/fleet/v1",
                "targetKind": "build-jobs",
                "targetId": self.build_id,
                "workerId": build["allocation"]["workerId"],
                "generation": record["generation"],
                "operation": "start",
                "commandId": start_id,
                "idempotencyKey": start_id,
                "payload": payload,
            }
            if cancellation is not None:
                _same(build["cancellation"], cancellation)
                _same(record["generation"], cancellation["generation"])
                _same(build["attempt"], cancellation["attempt"])
                command.update(
                    operation="cancel",
                    commandId=cancellation["commandId"],
                    idempotencyKey=cancellation["idempotencyKey"],
                )
                payload["stopStartCommandId"] = start_id
            _same(response["command"], command)
            return record
        except (KeyError, TypeError) as error:
            raise ValueError("invalid build response") from error

    async def submit(self, *, deadline: float) -> dict[str, Any]:
        """Submit once and return the validated controller record."""
        response = await self._call(
            lambda: self._client.fleet_build_submit(self.submission), deadline
        )
        return self._response(response)

    async def status(self, *, deadline: float) -> dict[str, Any]:
        """Read the record without granting execution or retrying a command."""
        record = await self._call(lambda: self._client.fleet_build_status(self.build_id), deadline)
        return self._record(record)

    async def prepare_cancel(
        self, command_id: str, idempotency_key: str, *, deadline: float
    ) -> dict[str, Any]:
        """Read cancellation identity for the caller to retain before sending."""
        _identifier(command_id)
        _identifier(idempotency_key)
        if command_id == self.build_id + "-start":
            raise ValueError("cancellation requires a distinct command identity")
        record = await self.status(deadline=deadline)
        return self.cancellation_for(record, command_id, idempotency_key)

    def cancellation_for(
        self, record: Json, command_id: str, idempotency_key: str
    ) -> dict[str, Any]:
        """Derive a stop from this exact validated observation without another read."""
        _identifier(command_id)
        _identifier(idempotency_key)
        if command_id == self.build_id + "-start":
            raise ValueError("cancellation requires a distinct command identity")
        record = self._record(record)
        return {
            "schema": "hi/fleet/build-cancel/v1",
            "commandId": command_id,
            "idempotencyKey": idempotency_key,
            "generation": record["generation"],
            "attempt": record["build"]["attempt"],
        }

    async def cancel(self, intent: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        """Send the complete retained cancellation without replacing its identity."""
        retained = _cancellation(intent)
        if retained["commandId"] == self.build_id + "-start":
            raise ValueError("cancellation requires a distinct command identity")
        response = await self._call(
            lambda: self._client.fleet_build_cancel(self.build_id, retained), deadline
        )
        return self._response(response, retained)


@dataclass
class _JournalAccess:
    """Serialize users of one borrowed journal on its existing event loop."""

    loop: asyncio.AbstractEventLoop
    lock: asyncio.Lock
    poisoned: bool = False


class FleetBuildOwner:
    """Retain ordinary caller intent in an already-qualified open journal.

    The caller owns journal creation durability, its lifetime and the SDK's
    event loop. This owner never opens or closes a writer. Its records hold
    immutable intent and admission identity, not current task state. MCP and
    a Fleet job must use the same trusted context and owner loop.
    """

    _journals: ClassVar[WeakKeyDictionary[WorkerJournal, _JournalAccess]] = WeakKeyDictionary()

    def __init__(
        self,
        service: FleetBuildService,
        journal: WorkerJournal,
        context_id: str,
        *,
        cancellation_ids: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        """Bind existing resources and validate retained context before any request."""
        self._service = service
        self._journal = journal
        self._context_id = _identifier(context_id)
        self._cancellation_ids = cancellation_ids or self._new_ids
        loop = asyncio.get_running_loop()
        access = self._journals.get(journal)
        if access is None:
            access = _JournalAccess(loop, asyncio.Lock())
            self._journals[journal] = access
        if access.loop is not loop:
            raise RuntimeError("build journal belongs to another event loop")
        self._access = access
        self._reload()

    @staticmethod
    def _new_ids() -> tuple[str, str]:
        return "build-stop-" + uuid4().hex, "build-stop-key-" + uuid4().hex

    @property
    def context_id(self) -> str:
        """Return the immutable registered context without reading mutable journal state."""
        return self._context_id

    @property
    def submission(self) -> dict[str, Any]:
        """Return the exact immutable intent for pre-submit registration checks."""
        return self._service.submission

    def _admission(self, record: Json) -> Json:
        """Retain the full identity needed for later independent collection."""
        build = record["build"]
        return {
            "buildId": record["id"],
            "commandId": self._service.build_id + "-start",
            "generation": record["generation"],
            "parent": record["parent"],
            "snapshot": self._service.submission["snapshot"],
            **{
                key: build[key]
                for key in (
                    "attempt",
                    "policy",
                    "policyDigest",
                    "parametersDigest",
                    "allocation",
                    "snapshotWorkspace",
                )
            },
        }

    def _check_admission(self, value: Any) -> Json:
        """Reuse the facade's typed binding checks for a retained admission."""
        try:
            admission = _object(value)
            record = self._service._record(
                {
                    "schema": "hi/fleet/v1",
                    "kind": "build-jobs",
                    "id": admission["buildId"],
                    "status": "admitted",
                    "collectionVerified": False,
                    "generation": admission["generation"],
                    "parent": admission["parent"],
                    "build": {
                        "schema": "hi/fleet/build/v1",
                        "request": self._service.submission,
                        **{
                            key: admission[key]
                            for key in (
                                "attempt",
                                "policy",
                                "policyDigest",
                                "parametersDigest",
                                "allocation",
                                "snapshotWorkspace",
                            )
                        },
                    },
                }
            )
            _same(admission, self._admission(record))
            return _object(json.loads(_encoded(admission)))
        except (KeyError, TypeError) as error:
            raise ValueError("invalid retained build admission") from error

    def _reload(self) -> None:
        state: Json = {
            "schema": "hi/hephaestus/build-consumer/v1",
            "contextId": self._context_id,
            "submission": self._service.submission,
            "admission": None,
            "cancellation": None,
        }
        self._saved = False
        for entry in self._journal.snapshot()["records"]:
            if entry["kind"] != "build-consumer":
                continue
            value = _object(entry["value"], set(state))
            _same(value["schema"], state["schema"])
            _identifier(value["contextId"])
            if value["contextId"] != self._context_id:
                other = _object(value["submission"])
                if all(
                    other.get(key) == state["submission"][key]
                    for key in ("workspaceId", "idempotencyKey")
                ):
                    raise ValueError("build intent is retained by another context")
                continue
            _same(value["submission"], state["submission"])
            if value["admission"] is not None:
                self._check_admission(value["admission"])
            if value["cancellation"] is not None:
                cancellation = _cancellation(value["cancellation"])
                if value["admission"] is None:
                    raise ValueError("retained cancellation requires admission identity")
                _same(cancellation["generation"], value["admission"]["generation"])
                _same(cancellation["attempt"], value["admission"]["attempt"])
                if cancellation["commandId"] == self._service.build_id + "-start":
                    raise ValueError("retained cancellation cannot replace the start")
            for key in ("admission", "cancellation"):
                if state[key] is not None:
                    _same(value[key], state[key])
            state = _object(json.loads(_encoded(value)))
            self._saved = True
        self._state = state

    def _retain(self, **changes: Any) -> None:
        value = {**self._state, **changes}
        try:
            self._journal.append("build-consumer", value)
        except BaseException:
            self._access.poisoned = True
            raise
        self._state = _object(json.loads(_encoded(value)))
        self._saved = True

    def _observe(self, record: Json) -> Json:
        admission = self._admission(record)
        if self._state["admission"] is None:
            self._retain(admission=admission)
        else:
            _same(admission, self._state["admission"])
        return record

    async def _operate(self, operation: str, deadline: float) -> Json:
        if asyncio.get_running_loop() is not self._access.loop:
            raise RuntimeError("build owner belongs to another event loop")
        async with asyncio.timeout(_remaining(deadline)):
            async with self._access.lock:
                _remaining(deadline)
                if self._access.poisoned:
                    raise RuntimeError("build journal requires recovery after uncertain write")
                with self._journal.transaction():
                    self._reload()
                    if not self._saved:
                        self._retain()
                if operation == "submit":
                    return self._observe(await self._service.submit(deadline=deadline))
                if operation == "status":
                    return self._observe(await self._service.status(deadline=deadline))
                if self._state["cancellation"] is None:
                    record = self._observe(await self._service.status(deadline=deadline))
                    command_id, idempotency_key = self._cancellation_ids()
                    intent = self._service.cancellation_for(record, command_id, idempotency_key)
                    self._retain(cancellation=intent)
                self._journal.require_writable()
                return self._observe(
                    await self._service.cancel(self._state["cancellation"], deadline=deadline)
                )

    async def submit(self, *, deadline: float) -> dict[str, Any]:
        """Retain the original submission before its first send."""
        return await self._operate("submit", deadline)

    async def status(self, *, deadline: float) -> dict[str, Any]:
        """Read a bound record without making it local task authority."""
        return await self._operate("status", deadline)

    async def cancel(self, *, deadline: float) -> dict[str, Any]:
        """Retain a complete first cancellation before its first send."""
        return await self._operate("cancel", deadline)
