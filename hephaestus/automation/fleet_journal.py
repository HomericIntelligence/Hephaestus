"""Keep private execution receipts in a bounded, single-writer journal."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any


class WorkerJournal:
    """Record execution facts without granting task admission authority."""

    def __init__(self, directory: Path, *, max_bytes: int = 64 * 1024 * 1024) -> None:
        """Lock and replay one private local journal."""
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise RuntimeError("journal directory must be private")
        self.directory = directory
        self.max_bytes = max_bytes
        self._lock = (directory / "writer.lock").open("a+b")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self._lock.close()
            raise RuntimeError("another journal writer is active") from error
        self.records: list[dict[str, Any]] = []
        self.commands: dict[str, dict[str, Any]] = {}
        self.command_ids: dict[str, str] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.runtime_pid: int | None = None
        self.runtime_uncertain = False
        self.generation = 0
        self.draining = False
        path = directory / "receipts.jsonl"
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            self._file = os.fdopen(descriptor, "a+b")
            self._file.seek(0)
            for line in self._file:
                if not line.endswith(b"\n"):
                    raise RuntimeError("journal has an incomplete receipt; recovery is required")
                self._apply(json.loads(line))
            if self._file.tell() > max_bytes:
                raise RuntimeError("journal size limit exceeded")
        except Exception:
            self._lock.close()
            if hasattr(self, "_file"):
                self._file.close()
            raise

    def _apply(self, record: dict[str, Any]) -> None:
        self.records.append(record)
        kind = record["kind"]
        value = record["value"]
        if kind == "intent":
            self.commands[value["key"]] = value
            self.command_ids[value["commandId"]] = value["key"]
        elif kind == "receipt":
            self.commands[value["key"]]["result"] = value["result"]
        elif kind == "session":
            self.sessions[value["sessionId"]] = value
        elif kind == "event":
            self.events.append(value)
        elif kind == "runtime":
            self.runtime_pid = value.get("pid")
            self.runtime_uncertain = value.get("uncertain", False)
        elif kind == "generation":
            self.generation = value["generation"]
        elif kind == "drain":
            self.draining = value["draining"]

    def append(self, kind: str, value: dict[str, Any]) -> None:
        """Flush a receipt before it can affect command acknowledgment."""
        record = {"kind": kind, "value": value}
        data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        self._file.seek(0, os.SEEK_END)
        if len(data) > 1024 * 1024 or self._file.tell() + len(data) > self.max_bytes:
            raise RuntimeError("journal is full; stop admission and retain receipts")
        self._file.write(data)
        self._file.flush()
        os.fsync(self._file.fileno())
        self._apply(json.loads(data))

    def begin(self, command: dict[str, Any]) -> dict[str, Any] | None:
        """Persist a command digest, or return its prior or uncertain receipt."""
        key = command["idempotencyKey"]
        content = {k: v for k, v in command.items() if k != "commandId"}
        digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        existing = self.commands.get(key)
        if existing is not None:
            if existing["digest"] != digest:
                return result_for(command, "failed", error="idempotency_conflict")
            return existing.get("result") or result_for(command, "failed", error="outcome_unknown")
        if command["commandId"] in self.command_ids:
            return result_for(command, "failed", error="command_id_conflict")
        self.append("intent", {"key": key, "commandId": command["commandId"], "digest": digest})
        return None

    def complete(self, command: dict[str, Any], result: dict[str, Any]) -> None:
        """Retain the command acknowledgment before returning it."""
        self.append("receipt", {"key": command["idempotencyKey"], "result": result})

    def close(self) -> None:
        """Release journal files and the single-writer lock."""
        self._file.close()
        self._lock.close()


def result_for(command: dict[str, Any], status: str, **receipt: Any) -> dict[str, Any]:
    """Build the common worker acknowledgment envelope."""
    identity = (
        command.get("workerId"),
        command.get("generation"),
        command.get("commandId"),
        status,
    )
    return {
        "schema": "hi/fleet/v1",
        "eventId": ":".join(str(value) for value in identity),
        "targetKind": command.get("targetKind"),
        "targetId": command.get("targetId"),
        "workerId": command.get("workerId"),
        "commandId": command.get("commandId"),
        "generation": command.get("generation"),
        "status": status,
        "receipt": receipt,
    }
