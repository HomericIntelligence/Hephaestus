"""Read registered terminal bundles without granting build authority."""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_CONFIGURATION_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_LOG_BYTES = 64 * 1024 * 1024
MAX_REGISTRATIONS = 256
MAX_RESPONSE_BYTES = 400000
_INTEGER_MAX = 2**63 - 1
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_DIGEST = re.compile(r"[a-f0-9]{64}")


def fields(value: Any, names: str) -> dict[str, Any]:
    """Require the exact fields of a versioned object."""
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise ValueError("The private object has invalid fields.")
    return value


def integer(value: Any, maximum: int = _INTEGER_MAX, minimum: int = 0) -> int:
    """Require an integer without Boolean or floating-point coercion."""
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("The private integer is outside its range.")
    return value


def identifier(value: Any) -> str:
    """Require one bounded opaque identifier, without a filesystem path."""
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("The private identifier is invalid.")
    return value


def digest_text(value: Any) -> str:
    """Require a lowercase SHA-256 commitment."""
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("The private digest is invalid.")
    return value


def canonical(value: Any) -> bytes:
    """Encode the producer's sorted compact UTF-8 JSON representation."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def decode_object(data: bytes) -> dict[str, Any]:
    """Reject duplicate keys, nonfinite values, excessive depth, and invalid UTF-8."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("The private JSON has a duplicate key.")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 16:
                raise ValueError("The private JSON is too deep.")
            if isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
        canonical(value)
    except (UnicodeError, RecursionError, OverflowError) as error:
        raise ValueError("The private JSON is invalid.") from error
    if not isinstance(value, dict):
        raise ValueError("The private JSON is not an object.")
    return value


def _fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


@contextmanager
def _open_descriptor(path: str, flags: int, *, dir_fd: int | None = None) -> Iterator[int]:
    descriptor = os.open(path, flags, dir_fd=dir_fd)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def read_private_file(path: Path, maximum: int) -> bytes:
    """Read one bounded owner-only regular file through no-follow descriptors."""
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 3:
        raise ValueError("A private input requires an absolute path.")
    if os.name != "posix" or any(
        not hasattr(os, name)
        for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK", "O_CLOEXEC", "geteuid")
    ):
        raise ValueError("Private descriptor reads are unavailable.")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as owned:
        parent = owned.enter_context(_open_descriptor(path.anchor, directory_flags))
        bindings: list[tuple[int, str, int]] = []
        for part in path.parts[1:-1]:
            child = owned.enter_context(_open_descriptor(part, directory_flags, dir_fd=parent))
            bindings.append((parent, part, child))
            parent = child
        metadata = os.fstat(parent)
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) not in (0o500, 0o700):
            raise ValueError("The private input directory is not owner-only.")
        descriptor = owned.enter_context(
            _open_descriptor(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent
            )
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) not in (0o400, 0o600)
            or before.st_nlink != 1
            or before.st_size > maximum
        ):
            raise ValueError("The private input file is invalid or too large.")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            data = stream.read(maximum + 1)
        if (
            len(data) > maximum
            or len(data) != before.st_size
            or _fingerprint(os.fstat(descriptor)) != _fingerprint(before)
            or _fingerprint(os.stat(path.name, dir_fd=parent, follow_symlinks=False))
            != _fingerprint(before)
        ):
            raise ValueError("The private input changed during its read.")
        for ancestor, name, child in bindings:
            expected = os.fstat(child)
            actual = os.stat(name, dir_fd=ancestor, follow_symlinks=False)
            if (actual.st_dev, actual.st_ino, actual.st_mode) != (
                expected.st_dev,
                expected.st_ino,
                expected.st_mode,
            ):
                raise ValueError("The private input directory changed.")
        return data


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _logs_digest(stdout: bytes, stderr: bytes) -> str:
    """Hash canonical strings in small pieces without large escaped copies."""
    result = hashlib.sha256()
    result.update(b'{"stderr":"')
    for index, data in enumerate((stderr, stdout)):
        if index:
            result.update(b'","stdout":"')
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        for offset in range(0, len(data), 8192):
            text = decoder.decode(data[offset : offset + 8192])
            result.update(canonical(text)[1:-1])
        result.update(canonical(decoder.decode(b"", final=True))[1:-1])
    result.update(b'"}')
    return result.hexdigest()


def _outcome(receipt: dict[str, Any]) -> None:
    outcome, code = receipt["outcome"], receipt["exitCode"]
    if outcome in ("cancelled", "timed_out"):
        if code is not None:
            raise ValueError("The interrupted receipt contains an exit code.")
    elif (
        outcome not in ("completed", "failed")
        or type(code) is not int
        or not -255 <= code <= 255
        or (outcome == "completed") != (code == 0)
    ):
        raise ValueError("The terminal receipt has an invalid outcome.")


@dataclass(frozen=True)
class RetainedLogs:
    """Keep immutable registered output and its exact lookup commitments."""

    build_id: str
    attempt: int
    snapshot_digest: str
    reference: str
    digest: str
    stdout: bytes
    stderr: bytes


def _receipt_identity(receipt: dict[str, Any], registration: dict[str, Any]) -> int:
    identity = receipt["identity"]
    if (
        not isinstance(identity, dict)
        or _sha(canonical(identity)) != registration["identityDigest"]
    ):
        raise ValueError("The complete result identity differs from registration.")
    try:
        allocation = identity["policy"]["allocation"]
        actual = (
            identifier(identity["buildId"]),
            integer(identity["attempt"], minimum=1),
            digest_text(identity["snapshot"]["manifestDigest"]),
            identifier(allocation["workerId"]),
            identifier(allocation["id"]),
            integer(allocation["generation"], minimum=1),
        )
        expected = (
            registration["buildId"],
            registration["attempt"],
            registration["snapshotDigest"],
            registration["workerId"],
            registration["allocationId"],
            registration["generation"],
        )
        if actual != expected:
            raise ValueError("The result identity differs from registration.")
        if registration["logs"]["reference"] != "output-" + identifier(identity["leaseId"]):
            raise ValueError("The retained log reference differs from its result lease.")
        return integer(identity["policy"]["recipe"]["resources"]["outputBytes"], MAX_LOG_BYTES, 1)
    except (KeyError, TypeError) as error:
        raise ValueError("The registered result identity is incomplete.") from error


def _registration(value: Any) -> dict[str, Any]:
    registration = fields(
        value,
        "directory receiptDigest identityDigest buildId attempt "
        "snapshotDigest workerId allocationId generation logs",
    )
    for name in ("buildId", "workerId", "allocationId"):
        identifier(registration[name])
    for name in ("receiptDigest", "identityDigest", "snapshotDigest"):
        digest_text(registration[name])
    for name in ("attempt", "generation"):
        integer(registration[name], minimum=1)
    logs = fields(registration["logs"], "reference digest")
    identifier(logs["reference"])
    digest_text(logs["digest"])
    if not isinstance(registration["directory"], str):
        raise ValueError("The result directory is invalid.")
    return registration


def _load_registration(value: Any, remaining: int) -> RetainedLogs:
    registration = _registration(value)
    logs = registration["logs"]
    root = Path(registration["directory"])
    raw = read_private_file(root / "receipt.json", MAX_RECORD_BYTES)
    if _sha(raw) != registration["receiptDigest"]:
        raise ValueError("The retained receipt digest differs from registration.")
    receipt = fields(
        decode_object(raw), "schema identity argv outcome exitCode cleanup files artifacts"
    )
    if (
        canonical(receipt) != raw
        or receipt["schema"] != "hi/hephaestus/build-result/v1"
        or receipt["artifacts"] != []
    ):
        raise ValueError("The retained receipt does not match the terminal profile.")
    _outcome(receipt)
    maximum = min(remaining, _receipt_identity(receipt, registration))
    descriptors = fields(receipt["files"], "manifest stdout stderr")
    streams: dict[str, bytes] = {}
    for role, name in (
        ("manifest", "manifest.json"),
        ("stdout", "stdout.txt"),
        ("stderr", "stderr.txt"),
    ):
        record = fields(descriptors[role], "path bytes digest")
        limit = MAX_RECORD_BYTES if role == "manifest" else maximum
        size = integer(record["bytes"], limit)
        if record["path"] != name:
            raise ValueError("The retained member has an invalid path.")
        data = read_private_file(root / name, size)
        if len(data) != size or _sha(data) != digest_text(record["digest"]):
            raise ValueError("The retained member differs from its receipt.")
        if role == "manifest":
            if _sha(data) != registration["snapshotDigest"]:
                raise ValueError("The retained source manifest differs from registration.")
            decode_object(data)
        else:
            streams[role] = data
            maximum -= len(data)
    if _logs_digest(streams["stdout"], streams["stderr"]) != logs["digest"]:
        raise ValueError("The retained log commitment differs from registration.")
    return RetainedLogs(
        registration["buildId"],
        registration["attempt"],
        registration["snapshotDigest"],
        logs["reference"],
        logs["digest"],
        streams["stdout"],
        streams["stderr"],
    )


class ArtifactRequestError(ValueError):
    """Return a fixed public error status without exposing retained input."""

    def __init__(self, status: int) -> None:
        """Keep only the public status for the transport boundary."""
        super().__init__("The private artifact request is invalid.")
        self.status = status


class ArtifactCatalog:
    """Load a bounded historical view with no HTTP registration operation."""

    def __init__(self, registrations: Any) -> None:
        """Read and verify every registered bundle before a listener can start."""
        if not isinstance(registrations, list) or len(registrations) > MAX_REGISTRATIONS:
            raise ValueError("The private registration count exceeds its bound.")
        self._records: dict[tuple[str, int], RetainedLogs] = {}
        roots: set[str] = set()
        remaining = MAX_LOG_BYTES
        for registration in registrations:
            record = _load_registration(registration, remaining)
            key = (record.build_id, record.attempt)
            directory = str(Path(registration["directory"]))
            if key in self._records or directory in roots:
                raise ValueError("The private registrations conflict.")
            self._records[key] = record
            roots.add(directory)
            remaining -= len(record.stdout) + len(record.stderr)

    def page(
        self, build_id: str, attempt: int, snapshot_digest: str, stream: str, after: int, limit: int
    ) -> dict[str, Any]:
        """Read one UTF-8 byte page from the exact retained terminal attempt."""
        try:
            identifier(build_id)
            integer(attempt, minimum=1)
            digest_text(snapshot_digest)
            integer(after)
            integer(limit, 65536, 1)
            if stream not in ("stdout", "stderr"):
                raise ValueError("Invalid stream.")
        except ValueError as error:
            raise ArtifactRequestError(400) from error
        record = self._records.get((build_id, attempt))
        if record is None or record.snapshot_digest != snapshot_digest:
            raise ArtifactRequestError(404)
        data = record.stdout if stream == "stdout" else record.stderr
        if after > len(data):
            raise ArtifactRequestError(409)
        if after < len(data) and data[after] & 0xC0 == 0x80:
            raise ArtifactRequestError(400)
        end = min(after + limit, len(data))
        while end < len(data) and data[end] & 0xC0 == 0x80:
            end -= 1
        if end == after and after < len(data):
            raise ArtifactRequestError(422)
        chunk = data[after:end]
        page = {
            "schema": "hi/fleet/build-logs/v1",
            "buildId": build_id,
            "attempt": attempt,
            "snapshotDigest": snapshot_digest,
            "stream": stream,
            "after": after,
            "next": end,
            "data": chunk.decode("utf-8"),
            "chunkDigest": _sha(chunk),
            "complete": end == len(data),
            "truncated": True,
            "manifest": {"reference": record.reference, "digest": record.digest},
        }
        if len(canonical(page)) > MAX_RESPONSE_BYTES:
            raise ArtifactRequestError(503)
        return page
