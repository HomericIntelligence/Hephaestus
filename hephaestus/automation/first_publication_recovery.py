"""Keep protected first-publication facts without granting execution authority."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from hephaestus.automation.direct_review_recovery import _write_receipt
from hephaestus.automation.pipeline.git_jobs import FirstPublicationRecord
from hephaestus.automation.pipeline.host_capabilities import CapabilityDeadline
from hephaestus.automation.rebase_recovery import (
    _namespace,
    _read_record_bytes,
    _record_lock,
    _record_name,
    _require_current,
    _unique_object,
)

_NAMESPACE = "first-publications"
_LOCK = "first-publications.lock"
_MAX_BYTES = 64 * 1024
_MAX_ENTRIES = 4096


def _read(
    descriptor: int, name: str, deadline: CapabilityDeadline
) -> tuple[FirstPublicationRecord, bytes] | None:
    """Read one exact first-publication schema, never a rebase record."""
    content = _read_record_bytes(descriptor, name, deadline)
    if content is None:
        return None
    try:
        record = FirstPublicationRecord.from_dict(
            json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
        )
    except (UnicodeError, TypeError, KeyError, AttributeError) as error:
        raise ValueError("The first-publication record is malformed.") from error
    if _record_name(record.issue_number, record.operation_id) != name:
        raise ValueError("The first-publication record name does not match its identity.")
    return record, content


def _candidates(
    descriptor: int, issue: int, deadline: CapabilityDeadline
) -> list[FirstPublicationRecord]:
    """Keep completed operations available for callback recovery."""
    _record_name(issue, "0" * 32)
    records: list[FirstPublicationRecord] = []
    with os.scandir(descriptor) as entries:
        for index, entry in enumerate(entries):
            deadline.remaining()
            if index >= _MAX_ENTRIES:
                raise ValueError("The first-publication namespace exceeds its entry limit.")
            if entry.name.startswith(f"{issue}-") and entry.name.endswith(".json"):
                value = _read(descriptor, entry.name, deadline)
                if value is None:
                    raise ValueError("The first-publication record disappeared during discovery.")
                records.append(value[0])
    if len(records) > 1:
        raise ValueError("Conflicting first-publication operations need operator recovery.")
    return records


class FirstPublicationStore:
    """Persist one exact operation while leaving live admission to the worker."""

    def __init__(self, common_dir: Path, *, deadline: CapabilityDeadline) -> None:
        """Use the caller's verified Git common directory and operation budget."""
        self.common_dir = common_dir
        self.deadline = deadline

    def read(self, issue: int, operation_id: str) -> FirstPublicationRecord | None:
        """Read one untrusted candidate without creating missing state."""
        name = _record_name(issue, operation_id)
        self.deadline.remaining()
        with _namespace(self.common_dir, create=False, namespace_name=_NAMESPACE) as opened:
            if opened is None:
                return None
            _, descriptor, identities = opened
            with _record_lock(descriptor, self.deadline, lock_name=_LOCK):
                value = _read(descriptor, name, self.deadline)
                _require_current(self.common_dir, identities, namespace_name=_NAMESPACE)
                self.deadline.remaining()
                return value[0] if value is not None else None

    def candidate(self, issue: int) -> FirstPublicationRecord | None:
        """Discover intent or completion without authorizing a retry or callback."""
        _record_name(issue, "0" * 32)
        self.deadline.remaining()
        with _namespace(self.common_dir, create=False, namespace_name=_NAMESPACE) as opened:
            if opened is None:
                return None
            _, descriptor, identities = opened
            with _record_lock(descriptor, self.deadline, lock_name=_LOCK):
                values = _candidates(descriptor, issue, self.deadline)
                _require_current(self.common_dir, identities, namespace_name=_NAMESPACE)
                self.deadline.remaining()
                return values[0] if values else None

    def write(
        self, record: FirstPublicationRecord, *, expected: FirstPublicationRecord | None
    ) -> FirstPublicationRecord:
        """Compare, durably replace, and read back one exact phase transition."""
        replace(record)
        if expected is None:
            if record.phase != "publication_intent":
                raise ValueError("First publication must start with durable intent.")
        else:
            replace(expected)
            if replace(record, phase=expected.phase) != expected or (
                record.phase != expected.phase
                and (expected.phase, record.phase) != ("publication_intent", "complete")
            ):
                raise ValueError("The first-publication operation or phase changed.")
        name = _record_name(record.issue_number, record.operation_id)
        content = json.dumps(record.to_dict(), sort_keys=True) + "\n"
        if len(content.encode("utf-8")) > _MAX_BYTES:
            raise ValueError("The first-publication record exceeds its byte limit.")
        self.deadline.remaining()
        with _namespace(self.common_dir, create=True, namespace_name=_NAMESPACE) as opened:
            if opened is None:
                raise ValueError("The first-publication namespace is unavailable.")
            directory, descriptor, identities = opened
            with _record_lock(descriptor, self.deadline, lock_name=_LOCK):
                current = _read(descriptor, name, self.deadline)
                if (current[0] if current is not None else None) != expected:
                    raise ValueError("The first-publication state changed before replacement.")
                if expected is None and _candidates(descriptor, record.issue_number, self.deadline):
                    raise ValueError("Another first-publication operation owns this issue.")
                _require_current(self.common_dir, identities, namespace_name=_NAMESPACE)
                self.deadline.remaining()
                _write_receipt(directory, descriptor, directory / name, content)
                verified = _read(descriptor, name, self.deadline)
                if verified != (record, content.encode("utf-8")):
                    raise ValueError("The first-publication readback does not match.")
                _require_current(self.common_dir, identities, namespace_name=_NAMESPACE)
                self.deadline.remaining()
                return record
