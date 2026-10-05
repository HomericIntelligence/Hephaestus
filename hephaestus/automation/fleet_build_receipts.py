"""Finalize captured Fleet evidence and require live acknowledgment to read it."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn, SupportsIndex
from weakref import WeakSet

from hephaestus.automation.fleet_build_contract import encoded, equal
from hephaestus.automation.fleet_build_executor import budget, read_private_file
from hephaestus.automation.fleet_build_storage import private_directory
from hephaestus.automation.pipeline.job_results import JobResult

Json = dict[str, Any]
_MAX_RECEIPT_BYTES = 4 * 1024 * 1024
_RECONCILIATION_SECONDS = 2.0
_LIVE: WeakSet[FleetBuildAcknowledgment] = WeakSet()
_LIVE_LOCK = threading.Lock()


def _successful_collection(value: Any) -> bool:
    return (
        type(value) is dict
        and value.get("schema") == "hi/hephaestus/build-collection/v1"
        and value.get("status") == "verified_current"
        and value.get("sourceCurrent") is True
        and value.get("outcome") == "completed"
        and type(value.get("exitCode")) is int
        and value["exitCode"] == 0
    )


def _read(path: Path, deadline: float) -> tuple[bytes, str]:
    if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent:
        raise ValueError("Fleet receipt path must be canonical")
    data, hashed, _size = read_private_file(
        path.parent, path.name, max_bytes=_MAX_RECEIPT_BYTES, deadline=deadline, retain=True
    )
    return data, hashed


@dataclass(frozen=True, eq=False)
class FleetBuildAcknowledgment:
    """Carry process-local evidence eligibility without retaining a lease.

    Only the finalizer registers live instances. Copies and reconstructed fields
    are not registered and cannot qualify a reader. This is not task authority.
    """

    _path: Path
    _receipt_digest: str
    _collection: bytes = field(repr=False)
    _pid: int = field(repr=False)

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        """Refuse serialization of an acknowledgment into another lifetime."""
        raise TypeError("Fleet acknowledgment is private to its completion lifetime")


@dataclass(frozen=True)
class FleetBuildReceipt:
    """Keep one captured candidate through release and final durability."""

    path: Path
    payload: Json
    deadline: float
    writer: Callable[[Path, str], None]

    def __post_init__(self) -> None:
        """Retain copied capture bytes independently of the returned collection."""
        object.__setattr__(self, "payload", dict(json.loads(encoded(self.payload))))

    def _write(self, payload: Json, deadline: float) -> str:
        data = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
        raw = data.encode()
        if len(raw) > _MAX_RECEIPT_BYTES:
            raise ValueError("Fleet completion receipt exceeds its byte limit")
        with private_directory(self.path.parent, deadline):
            self.writer(self.path, data)
            actual, hashed = _read(self.path, deadline)
            if actual != raw:
                raise RuntimeError("Fleet receipt changed during publication")
        budget(deadline)
        return hashed

    def pending(self) -> None:
        """Capture only an ineligible record while both leases remain held."""
        if (
            self.payload.get("fleet_receipt_state") != "pending"
            or self.payload.get("ok") is not False
            or self.payload.get("succeeded") is not False
        ):
            raise ValueError("Fleet capture must be pending and ineligible")
        self._write(self.payload, self.deadline)

    def _invalidate(self, result: JobResult, original: Exception) -> NoReturn:
        failed = {
            **self.payload,
            "fleet_receipt_state": "failed",
            "ok": False,
            "succeeded": False,
            "interrupted": result.interrupted,
            "fleet_receipt_error": "finalization_failed",
        }
        try:
            # Corrective storage gets no ability to submit, observe or cancel work.
            self._write(failed, time.monotonic() + _RECONCILIATION_SECONDS)
        except Exception as error:
            raise RuntimeError(
                "Fleet receipt requires reconciliation; invalidation failed"
            ) from error
        raise RuntimeError(
            "Fleet receipt finalization failed; retained failed evidence"
        ) from original

    def finalize(
        self, result: JobResult, *, shutdown: threading.Event
    ) -> FleetBuildAcknowledgment | None:
        """Issue acknowledgment only after release, exact durable bytes and budget."""
        if shutdown.is_set():
            result = replace(result, ok=False, interrupted=True)
        if result.ok is True and (
            not _successful_collection(result.value)
            or not equal(result.value, self.payload["fleet_collection"])
        ):
            raise ValueError("Fleet completion is not verified current success")
        payload = {
            **self.payload,
            "fleet_receipt_state": "finalized" if result.ok else "failed",
            "ok": result.ok,
            "succeeded": result.ok,
            "interrupted": result.interrupted,
        }
        try:
            hashed = self._write(payload, self.deadline)
            budget(self.deadline)
            if shutdown.is_set():
                raise InterruptedError("Fleet completion interrupted after final durability")
        except Exception as error:
            if shutdown.is_set():
                result = replace(result, ok=False, interrupted=True)
            self._invalidate(result, error)
        if result.ok is not True or result.interrupted is not False or result.error is not None:
            return None
        acknowledgment = FleetBuildAcknowledgment(
            self.path, hashed, encoded(self.payload["fleet_collection"]), os.getpid()
        )
        with _LIVE_LOCK:
            budget(self.deadline)
            if shutdown.is_set():
                raise InterruptedError("Fleet completion interrupted before acknowledgment")
            _LIVE.add(acknowledgment)
        return acknowledgment


def read_fleet_build_receipt(result: JobResult, *, expected: Json, deadline: float) -> Json:
    """Read actual private bytes using the successful completion's live capability."""
    try:
        budget(deadline)
        acknowledgment = result.fleet_receipt
        if (
            type(acknowledgment) is not FleetBuildAcknowledgment
            or acknowledgment._pid != os.getpid()
            or result.ok is not True
            or result.interrupted is not False
            or result.error is not None
            or not _successful_collection(result.value)
        ):
            raise ValueError("Fleet completion has no eligible acknowledgment")
        with _LIVE_LOCK:
            if acknowledgment not in _LIVE:
                raise ValueError("Fleet acknowledgment was not issued in this lifetime")
        if encoded(result.value) != acknowledgment._collection:
            raise ValueError("Fleet acknowledgment belongs to another completion")
        if not equal(result.value["identity"], expected):
            raise ValueError("Fleet completion differs from the expected identity")
        raw, hashed = _read(acknowledgment._path, deadline)
        payload = json.loads(raw)
        if (
            hashed != acknowledgment._receipt_digest
            or payload["fleet_receipt_state"] != "finalized"
            or payload["ok"] is not True
            or payload["succeeded"] is not True
            or payload["interrupted"] is not False
            or not equal(payload["fleet_collection"], result.value)
        ):
            raise ValueError("Fleet receipt differs from its acknowledged candidate")
        budget(deadline)
        return dict(payload["fleet_collection"])
    except (OSError, TypeError, KeyError, ValueError) as error:
        raise RuntimeError("Fleet receipt is not eligible") from error
