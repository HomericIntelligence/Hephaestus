"""Protected storage for unfinished, source-bound rebase operations."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from hephaestus.automation.direct_review_recovery import _write_receipt
from hephaestus.automation.host_capabilities import _private_entry
from hephaestus.automation.pipeline.git_jobs import PendingRebaseRecord
from hephaestus.automation.pipeline.host_capabilities import CapabilityDeadline

_MAX_BYTES = 64 * 1024
_MAX_ENTRIES = 4096
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_Identity = tuple[tuple[int, int], ...]


def _owned_directory(info: os.stat_result, *, private: bool) -> None:
    """Distinguish protected shared parents from private recovery state."""
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
        or (private and stat.S_IMODE(info.st_mode) != 0o700)
    ):
        raise ValueError("The pending rebase directory permissions are unsafe.")


@contextmanager
def _namespace(
    common_dir: Path, *, create: bool, namespace_name: str = "pending-rebases"
) -> Iterator[tuple[Path, int, _Identity] | None]:
    """Open each state component without following a symlink."""
    if namespace_name not in {"pending-rebases", "first-publications"}:
        raise ValueError("The recovery namespace is invalid.")
    if not common_dir.is_absolute() or common_dir.resolve(strict=True) != common_dir:
        raise ValueError("The pending rebase common directory is not canonical.")
    descriptor = os.open(common_dir, _DIRECTORY_FLAGS)
    identities: list[tuple[int, int]] = []
    path = common_dir
    try:
        info = os.fstat(descriptor)
        _owned_directory(info, private=False)
        identities.append((info.st_dev, info.st_ino))
        for component in ("hephaestus-source-workspaces", namespace_name):
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                else:
                    os.fsync(descriptor)
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if create:
                    raise
                yield None
                return
            os.close(descriptor)
            descriptor = child
            path /= component
            info = os.fstat(descriptor)
            _owned_directory(info, private=component == namespace_name)
            identities.append((info.st_dev, info.st_ino))
        yield path, descriptor, tuple(identities)
    finally:
        os.close(descriptor)


def _require_current(
    common_dir: Path, identities: _Identity, *, namespace_name: str = "pending-rebases"
) -> None:
    """Reject a renamed or replaced live namespace before reporting success."""
    with _namespace(common_dir, create=False, namespace_name=namespace_name) as current:
        if current is None or current[2] != identities:
            raise ValueError("The pending rebase namespace changed.")


@contextmanager
def _record_lock(
    descriptor: int,
    deadline: CapabilityDeadline,
    *,
    lock_name: str = "pending-rebases.lock",
) -> Iterator[None]:
    """Take only the short record lock within the caller's operation budget."""
    deadline.remaining()
    if lock_name not in {"pending-rebases.lock", "first-publications.lock"}:
        raise ValueError("The recovery lock name is invalid.")
    handle = os.open(
        lock_name,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
        dir_fd=descriptor,
    )
    try:
        _private_entry(os.fstat(handle), directory=False)
        if stat.S_IMODE(os.fstat(handle).st_mode) != 0o600:
            raise ValueError("The pending rebase lock mode is invalid.")
        while True:
            deadline.remaining()
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                time.sleep(min(0.05, deadline.remaining()))
            else:
                break
        try:
            _require_current_lock(descriptor, handle, lock_name=lock_name)
            yield
            _require_current_lock(descriptor, handle, lock_name=lock_name)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


def _require_current_lock(
    descriptor: int, handle: int, *, lock_name: str = "pending-rebases.lock"
) -> None:
    """Require the held lock to name the same live private file."""
    current = os.stat(lock_name, dir_fd=descriptor, follow_symlinks=False)
    _private_entry(current, directory=False)
    if stat.S_IMODE(current.st_mode) != 0o600 or not os.path.samestat(os.fstat(handle), current):
        raise ValueError("The pending rebase lock identity changed.")


def _record_name(issue: int, candidate: str) -> str:
    """Accept only issue-qualified record names, never arbitrary paths."""
    if (
        type(issue) is not int
        or issue <= 0
        or type(candidate) is not str
        or re.fullmatch(r"[0-9a-f]{32}", candidate) is None
    ):
        raise ValueError("The pending rebase record name is invalid.")
    return f"{issue}-{candidate}.json"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject repeated JSON keys instead of selecting one authority value."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("The pending rebase record repeats a key.")
        result[key] = value
    return result


def _read_record_bytes(descriptor: int, name: str, deadline: CapabilityDeadline) -> bytes | None:
    """Read bounded private bytes while checking the open and live identities."""
    try:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(handle)
        _private_entry(before, directory=False)
        if stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > _MAX_BYTES:
            raise ValueError("The pending rebase record mode or size is invalid.")
        chunks = bytearray()
        while len(chunks) <= _MAX_BYTES:
            deadline.remaining()
            value = os.read(handle, min(8192, _MAX_BYTES + 1 - len(chunks)))
            if not value:
                break
            chunks.extend(value)
        after = os.fstat(handle)
        current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if (
            len(chunks) > _MAX_BYTES
            or not os.path.samestat(before, current)
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise ValueError("The pending rebase record changed during its read.")
        return bytes(chunks)
    finally:
        os.close(handle)


def _read_record(
    descriptor: int, name: str, deadline: CapabilityDeadline
) -> tuple[PendingRebaseRecord, bytes] | None:
    """Decode only the rebase schema from verified private record bytes."""
    content = _read_record_bytes(descriptor, name, deadline)
    if content is None:
        return None
    try:
        record = PendingRebaseRecord.from_dict(
            json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
        )
    except (UnicodeError, TypeError, KeyError, AttributeError) as error:
        raise ValueError("The pending rebase record is malformed.") from error
    if _record_name(record.request.issue_number, record.request.request_id) != name:
        raise ValueError("The pending rebase record identity does not match its name.")
    return record, content


def _records_for_issue(
    descriptor: int, issue: int, deadline: CapabilityDeadline
) -> list[PendingRebaseRecord]:
    """Bound discovery work and reject conflicting active operation identities."""
    _record_name(issue, "0" * 32)
    records: list[PendingRebaseRecord] = []
    with os.scandir(descriptor) as entries:
        for index, entry in enumerate(entries):
            deadline.remaining()
            if index >= _MAX_ENTRIES:
                raise ValueError("The pending rebase namespace exceeds its entry limit.")
            if entry.name.startswith(f"{issue}-") and entry.name.endswith(".json"):
                value = _read_record(descriptor, entry.name, deadline)
                if value is None:
                    raise ValueError("The pending rebase record disappeared during discovery.")
                if value[0].phase not in {"complete", "aborted"}:
                    records.append(value[0])
    return records


def _check_transition(record: PendingRebaseRecord, expected: PendingRebaseRecord | None) -> None:
    """Permit only the next durable phase of the same immutable operation."""
    if expected is None:
        if record.phase != "intent":
            raise ValueError("A pending rebase must start with an intent.")
        return
    replace(expected)
    if record.schema_version != expected.schema_version and not (
        expected.schema_version == 1
        and record.schema_version == 2
        and expected.phase == "intent"
        and record.phase == "aborted"
    ):
        raise ValueError("The pending rebase schema transition is invalid.")
    restored = replace(
        record,
        schema_version=expected.schema_version,
        phase=expected.phase,
        resulting_workspace=expected.resulting_workspace,
        resulting_tree_sha=expected.resulting_tree_sha,
        restored_workspace=expected.restored_workspace,
        restored_tree_sha=expected.restored_tree_sha,
    )
    if restored != expected:
        raise ValueError("The pending rebase operation identity changed.")
    if expected.phase != "intent" and (
        record.resulting_workspace != expected.resulting_workspace
        or record.resulting_tree_sha != expected.resulting_tree_sha
        or record.restored_workspace != expected.restored_workspace
        or record.restored_tree_sha != expected.restored_tree_sha
    ):
        raise ValueError("The pending rebase result changed.")
    next_phases = {
        "intent": {"pending_validation", "aborted"},
        "pending_validation": {
            "complete" if record.publication_mode == "none" else "publication_intent"
        },
        "publication_intent": {"complete"},
    }.get(expected.phase, set())
    if record != expected and record.phase not in next_phases:
        raise ValueError("The pending rebase phase transition is invalid.")


class PendingRebaseStore:
    """Keep one operation's durable state without admitting source execution."""

    def __init__(self, common_dir: Path, *, deadline: CapabilityDeadline) -> None:
        """Use an admitted Git common directory and the caller's absolute deadline."""
        self.common_dir = common_dir
        self.deadline = deadline

    def read(self, issue: int, candidate: str) -> PendingRebaseRecord | None:
        """Read a candidate without creating missing state directories."""
        name = _record_name(issue, candidate)
        self.deadline.remaining()
        with _namespace(self.common_dir, create=False) as opened:
            if opened is None:
                return None
            _, descriptor, identities = opened
            with _record_lock(descriptor, self.deadline):
                value = _read_record(descriptor, name, self.deadline)
                _require_current(self.common_dir, identities)
                self.deadline.remaining()
                return value[0] if value is not None else None

    def candidate(self, issue: int) -> PendingRebaseRecord | None:
        """Return untrusted pending data; ambiguous mutation intents must block."""
        _record_name(issue, "0" * 32)
        self.deadline.remaining()
        with _namespace(self.common_dir, create=False) as opened:
            if opened is None:
                return None
            _, descriptor, identities = opened
            with _record_lock(descriptor, self.deadline):
                active = _records_for_issue(descriptor, issue, self.deadline)
                _require_current(self.common_dir, identities)
                self.deadline.remaining()
                if len(active) > 1 or (active and active[0].phase == "intent"):
                    raise ValueError("The pending rebase state needs operator recovery.")
                return active[0] if active else None

    def write(
        self, record: PendingRebaseRecord, *, expected: PendingRebaseRecord | None
    ) -> PendingRebaseRecord:
        """Compare, durably replace, and read back one exact phase transition."""
        replace(record)
        _check_transition(record, expected)
        name = _record_name(record.request.issue_number, record.request.request_id)
        content = json.dumps(record.to_dict(), sort_keys=True) + "\n"
        if len(content.encode("utf-8")) > _MAX_BYTES:
            raise ValueError("The pending rebase record exceeds its byte limit.")
        self.deadline.remaining()
        with _namespace(self.common_dir, create=True) as opened:
            if opened is None:
                raise ValueError("The pending rebase namespace is unavailable.")
            directory, descriptor, identities = opened
            with _record_lock(descriptor, self.deadline):
                current = _read_record(descriptor, name, self.deadline)
                if (current[0] if current is not None else None) != expected:
                    raise ValueError("The pending rebase phase changed before replacement.")
                if expected is None and _records_for_issue(
                    descriptor, record.request.issue_number, self.deadline
                ):
                    raise ValueError("Another pending rebase already owns this issue.")
                _require_current(self.common_dir, identities)
                self.deadline.remaining()
                _write_receipt(directory, descriptor, directory / name, content)
                verified = _read_record(descriptor, name, self.deadline)
                if verified != (record, content.encode("utf-8")):
                    raise ValueError("The pending rebase readback does not match.")
                _require_current(self.common_dir, identities)
                self.deadline.remaining()
                return record
