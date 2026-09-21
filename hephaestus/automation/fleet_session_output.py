"""Retain completed command output on private worker storage."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hephaestus.automation.fleet_build_artifacts import (
    canonical,
    decode_object,
    digest_text,
    fields,
    integer,
    read_private_file,
)

MAX_ITEMS = 64
MAX_OBSERVED = 4096
MAX_OUTPUT_BYTES = 64 * 1024
MAX_ITEM_BYTES = 1024 * 1024
MAX_BUNDLE_BYTES = 2 * 1024 * 1024
_MAX_STATE_BYTES = 2 * 1024 * 1024
_MAX_JOURNAL_BYTES = 64 * 1024 * 1024
_SAFE_INTEGER = 2**53 - 1
_IDENTITY_FIELDS = (
    "workerId generation allocationId sessionId executionId taskId agentId providerThreadId"
)
_ITEM_FIELDS = "turnId itemId completedAtMs command cwd status exitCode durationMs output"
_STATE_FIELDS = "identity entries recordBytes retentionLimited conflict"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(value: Any, maximum: int | None = None, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ValueError("invalid_output_text")
    if maximum is not None and len(value.encode("utf-8")) > maximum:
        raise ValueError("output_text_limit")
    return value


def _identity(session: dict[str, Any]) -> dict[str, Any]:
    result = {key: session.get(key) for key in _IDENTITY_FIELDS.split()}
    for key, value in result.items():
        if key == "generation":
            integer(value, _SAFE_INTEGER, 1)
        else:
            _text(value, 1024, nonempty=True)
    return result


def _private_directory(path: Path, *, create: bool = False) -> None:
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("output_path_not_canonical")
    metadata = path.stat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("output_directory_not_private")


def _directory(state_dir: Path, session_id: str, generation: int, *, create: bool) -> Path:
    _text(session_id, 1024, nonempty=True)
    integer(generation, _SAFE_INTEGER, 1)
    _private_directory(state_dir)
    root = state_dir / "session-output"
    _private_directory(root, create=create)
    selector = _digest(canonical({"sessionId": session_id, "generation": generation}))
    directory = root / selector
    _private_directory(directory, create=create)
    return directory


@contextmanager
def _lock(directory: Path, *, writer: bool) -> Iterator[None]:
    path = directory / "capture.lock"
    flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    flags |= (os.O_RDWR | os.O_CREAT) if writer else os.O_RDONLY
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size != 0
        ):
            raise ValueError("invalid_output_lock")
        fcntl.flock(descriptor, (fcntl.LOCK_EX if writer else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _stopped_worker(state_dir: Path) -> Iterator[None]:
    """Hold the existing journal lock without opening a journal writer."""
    descriptor = os.open(
        state_dir / "writer.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
            or metadata.st_nlink != 1
            or metadata.st_size != 0
        ):
            raise ValueError("invalid_worker_journal_lock")
        # The existing empty lock can be mode 0644 inside the private state root.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _create_file(path: Path, data: bytes) -> None:
    _private_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


def _write_manifest(directory: Path, value: dict[str, Any]) -> None:
    data = canonical(value)
    if len(data) > _MAX_STATE_BYTES:
        raise ValueError("output_state_limit")
    # The item file is durable before its reference enters this atomic state.
    with tempfile.NamedTemporaryFile(dir=directory, prefix="pending-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, directory / "capture.json")
            _sync_directory(directory)
        finally:
            temporary.unlink(missing_ok=True)


def _manifest(directory: Path, identity: dict[str, Any], *, create: bool) -> dict[str, Any]:
    if os.path.lexists(directory / "incomplete"):
        raise ValueError("output_capture_unavailable")
    try:
        result = decode_object(read_private_file(directory / "capture.json", _MAX_STATE_BYTES))
    except FileNotFoundError:
        if not create:
            raise
        return {
            "identity": identity,
            "entries": [],
            "recordBytes": 0,
            "retentionLimited": False,
            "conflict": False,
        }
    fields(result, _STATE_FIELDS)
    if result["identity"] != identity:
        raise ValueError("output_identity_conflict")
    entries = result["entries"]
    if not isinstance(entries, list) or len(entries) > MAX_OBSERVED:
        raise ValueError("invalid_output_observations")
    seen: set[str] = set()
    for entry in entries:
        fields(entry, "key sourceDigest recordDigest")
        key = digest_text(entry["key"])
        digest_text(entry["sourceDigest"])
        if entry["recordDigest"] is not None:
            digest_text(entry["recordDigest"])
        if key in seen:
            raise ValueError("duplicate_output_identity")
        seen.add(key)
    integer(result["recordBytes"], MAX_ITEM_BYTES)
    if any(type(result[key]) is not bool for key in ("retentionLimited", "conflict")):
        raise ValueError("invalid_output_state")
    if result["conflict"]:
        raise ValueError("output_capture_unavailable")
    return result


def _source(params: dict[str, Any]) -> dict[str, Any] | None:
    """Select valid terminal command fields without changing their values."""
    item = params.get("item")
    if not isinstance(item, dict) or item.get("type") != "commandExecution":
        return None
    try:
        result = {
            "turnId": _text(params["turnId"], 1024, nonempty=True),
            "itemId": _text(item["id"], 1024, nonempty=True),
            "completedAtMs": integer(params["completedAtMs"], _SAFE_INTEGER),
            **{key: item[key] for key in ("command", "cwd", "status", "exitCode", "durationMs")},
            "output": item["aggregatedOutput"],
        }
        _text(result["command"])
        _text(result["cwd"])
        if result["status"] not in ("completed", "failed", "declined"):
            return None
        if result["exitCode"] is not None:
            integer(result["exitCode"], 2**31 - 1, -(2**31))
        if result["durationMs"] is not None:
            integer(result["durationMs"], _SAFE_INTEGER)
        if result["output"] is not None:
            _text(result["output"])
        canonical(result)
        return result
    except (KeyError, ValueError, UnicodeError, TypeError):
        return None


def _record(identity: dict[str, Any], source: dict[str, Any]) -> dict[str, Any] | None:
    if (
        len(source["command"].encode("utf-8")) > 16 * 1024
        or len(source["cwd"].encode("utf-8")) > 4 * 1024
    ):
        return None
    text = source["output"]
    original = None if text is None else text.encode("utf-8")
    retained = None if original is None else original[:MAX_OUTPUT_BYTES].decode("utf-8", "ignore")
    encoded = None if retained is None else retained.encode("utf-8")
    result = {
        **source,
        "output": {
            "kind": "provider_aggregate",
            "text": retained,
            "byteCount": None if encoded is None else len(encoded),
            "sha256": None if encoded is None else _digest(encoded),
            "providerTruncated": None,
            "captureTruncated": original is not None and len(original) > MAX_OUTPUT_BYTES,
        },
    }
    result["recordDigest"] = _digest(canonical({"identity": identity, "item": result}))
    return result


def retain_command(state_dir: Path, session: dict[str, Any], params: dict[str, Any]) -> None:
    """Retain one owned command; leave malformed notifications outside the counts."""
    source = _source(params)
    if source is None:
        return
    identity = _identity(session)
    directory = _directory(state_dir, identity["sessionId"], identity["generation"], create=True)
    with _lock(directory, writer=True):
        manifest = _manifest(directory, identity, create=True)
        key = _digest(canonical({"turnId": source["turnId"], "itemId": source["itemId"]}))
        source_digest = _digest(canonical(source))
        existing = next((entry for entry in manifest["entries"] if entry["key"] == key), None)
        if existing is not None and existing["sourceDigest"] == source_digest:
            return
        # An interrupted transition must not export the preceding capture state.
        _create_file(directory / "incomplete", b"")
        if existing is not None:
            manifest["conflict"] = True
            _write_manifest(directory, manifest)
            raise ValueError("output_identity_conflict")
        if len(manifest["entries"]) >= MAX_OBSERVED:
            manifest["conflict"] = True
            _write_manifest(directory, manifest)
            raise ValueError("output_observation_limit")
        record = _record(identity, source)
        retained_count = sum(entry["recordDigest"] is not None for entry in manifest["entries"])
        data = canonical(record) if record is not None else b""
        if retained_count >= MAX_ITEMS or manifest["recordBytes"] + len(data) > MAX_ITEM_BYTES:
            record = None
        record_digest = None
        if record is None:
            manifest["retentionLimited"] = True
        else:
            record_digest = record["recordDigest"]
            path = directory / f"{record_digest}.json"
            try:
                _create_file(path, data)
            except FileExistsError:
                if read_private_file(path, MAX_ITEM_BYTES) != data:
                    raise ValueError("output_record_conflict") from None
            manifest["recordBytes"] += len(data)
            manifest["retentionLimited"] |= record["output"]["captureTruncated"]
        manifest["entries"].append(
            {"key": key, "sourceDigest": source_digest, "recordDigest": record_digest}
        )
        _write_manifest(directory, manifest)
        (directory / "incomplete").unlink()
        _sync_directory(directory)


def _retained_session(state_dir: Path, session_id: str, generation: int) -> dict[str, Any]:
    data = read_private_file(state_dir / "receipts.jsonl", _MAX_JOURNAL_BYTES)
    if not data.endswith(b"\n"):
        raise ValueError("incomplete_worker_journal")
    session = None
    for line in data.splitlines():
        if len(line) > 1024 * 1024:
            raise ValueError("worker_receipt_limit")
        entry = fields(decode_object(line), "kind value")
        if entry["kind"] == "session":
            value = entry["value"]
            if not isinstance(value, dict):
                raise ValueError("invalid_worker_session")
            if value.get("sessionId") == session_id:
                session = value
    if session is None or session.get("generation") != generation:
        raise ValueError("retained_session_not_found")
    if session.get("outputCaptureUnavailable", False) is not False:
        raise ValueError("output_capture_unavailable")
    return _identity(session)


def _checked_record(data: bytes, identity: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    record = fields(decode_object(data), _ITEM_FIELDS + " recordDigest")
    unsigned = {key: value for key, value in record.items() if key != "recordDigest"}
    if (
        record["recordDigest"] != entry["recordDigest"]
        or _digest(canonical({"identity": identity, "item": unsigned})) != record["recordDigest"]
        or _digest(canonical({"turnId": record["turnId"], "itemId": record["itemId"]}))
        != entry["key"]
    ):
        raise ValueError("output_record_digest_mismatch")
    output = fields(
        record["output"], "kind text byteCount sha256 providerTruncated captureTruncated"
    )
    if output["kind"] != "provider_aggregate" or output["providerTruncated"] is not None:
        raise ValueError("invalid_output_profile")
    if type(output["captureTruncated"]) is not bool:
        raise ValueError("invalid_output_truncation")
    original = _source(
        {
            "turnId": record["turnId"],
            "completedAtMs": record["completedAtMs"],
            "item": {
                **unsigned,
                "id": record["itemId"],
                "type": "commandExecution",
                "aggregatedOutput": output["text"],
            },
        }
    )
    expected = None if original is None else _record(identity, original)
    if expected is None or any(
        output[key] != expected["output"][key] for key in ("byteCount", "sha256")
    ):
        raise ValueError("invalid_retained_output")
    if output["text"] is not None:
        _text(output["text"], MAX_OUTPUT_BYTES)
        integer(output["byteCount"], MAX_OUTPUT_BYTES)
    elif output["captureTruncated"]:
        raise ValueError("invalid_null_output")
    return record


def export_session_output(
    state_dir: Path, session_id: str, generation: int, output: Path
) -> dict[str, Any]:
    """Export retained records only; do not connect to or start a worker."""
    directory = _directory(state_dir, session_id, generation, create=False)
    with _stopped_worker(state_dir), _lock(directory, writer=False):
        identity = _retained_session(state_dir, session_id, generation)
        manifest = _manifest(directory, identity, create=False)
        items = []
        total_bytes = 0
        for entry in manifest["entries"]:
            if entry["recordDigest"] is not None:
                data = read_private_file(
                    directory / f"{entry['recordDigest']}.json", MAX_ITEM_BYTES
                )
                total_bytes += len(data)
                items.append(_checked_record(data, identity, entry))
        omitted = len(manifest["entries"]) - len(items)
        limited = omitted > 0 or any(item["output"]["captureTruncated"] for item in items)
        if (
            total_bytes != manifest["recordBytes"]
            or total_bytes > MAX_ITEM_BYTES
            or len(items) > MAX_ITEMS
            or limited != manifest["retentionLimited"]
        ):
            raise ValueError("output_capture_counts_mismatch")
        bundle = {
            "schema": "hi/fleet/session-output/v1",
            "identity": identity,
            "provider": {"name": "codex", "version": "0.153.4"},
            "capture": {
                "profile": "completed_command_items",
                "complete": False,
                "observedCompletedItems": len(manifest["entries"]),
                "retainedItems": len(items),
                "omittedItems": omitted,
                "retentionLimited": limited,
            },
            "items": items,
        }
        encoded = canonical(bundle)
        if len(encoded) > MAX_BUNDLE_BYTES:
            raise ValueError("output_export_limit")
        _create_file(output, encoded)
    return {
        "schema": "hi/fleet/session-output-receipt/v1",
        "path": str(output),
        "identity": identity,
        "byteCount": len(encoded),
        "receiptDigest": _digest(encoded),
    }
