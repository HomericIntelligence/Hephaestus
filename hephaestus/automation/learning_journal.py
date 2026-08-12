"""Durable, cross-process journal for auxiliary learning intents."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any

from hephaestus.io.utils import write_secure
from hephaestus.utils.file_lock import LockUnavailableError, file_lock

logger = logging.getLogger(__name__)

_STATUSES = frozenset({"pending", "claimed", "succeeded", "failed"})
_REQUIRED_FIELDS = frozenset({"key", "kind", "status", "attempts", "created_at", "updated_at"})
_RESERVED_FIELDS = _REQUIRED_FIELDS | frozenset(
    {"error", "receipt_summary", "claim_owner", "cleanup_status", "cleanup_error"}
)


class LearningJournalError(RuntimeError):
    """Report unreadable or invalid durable learning evidence."""


class LearningClaimRegistry:
    """Retain process-owned claim locks across repository context eviction."""

    def __init__(self) -> None:
        """Create an empty thread-safe ownership registry."""
        self.locks: dict[str, AbstractContextManager[None]] = {}
        self.guard = Lock()


class LearningJournalStore:
    """Persist strict, bounded state for auxiliary learning intents."""

    def __init__(
        self,
        state_dir_provider: Callable[[], Path],
        *,
        claim_registry: LearningClaimRegistry | None = None,
    ) -> None:
        """Use the current automation state directory on every operation."""
        self._state_dir_provider = state_dir_provider
        self._claim_registry = claim_registry or LearningClaimRegistry()
        self._claim_locks = self._claim_registry.locks
        self._claim_locks_guard = self._claim_registry.guard

    @staticmethod
    def _digest(key: str) -> str:
        return sha256(key.encode("utf-8")).hexdigest()

    def path(self, key: str) -> Path:
        """Return the journal record path for ``key``."""
        return self._state_dir_provider() / f"learning-intent-{self._digest(key)}.json"

    def lock_path(self, key: str) -> Path:
        """Return the stable sibling lock path for ``key``."""
        return self.path(key).with_suffix(".lock")

    def claim_lock_path(self, key: str) -> Path:
        """Return the lock held for the complete external delivery attempt."""
        return self.path(key).with_suffix(".claim.lock")

    def load(self, key: str) -> dict[str, Any] | None:
        """Read one valid record, or return ``None`` only when it is absent."""
        path = self.path(key)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise LearningJournalError(f"learning intent {key} has invalid JSON: {exc}") from exc
        except (OSError, TypeError) as exc:
            raise LearningJournalError(f"could not read learning intent {key}: {exc}") from exc
        if not isinstance(raw, dict):
            raise LearningJournalError(f"learning intent {key} is not an object")
        missing = _REQUIRED_FIELDS.difference(raw)
        if missing:
            names = ", ".join(sorted(missing))
            raise LearningJournalError(f"learning intent {key} has missing fields: {names}")
        if raw.get("key") != key:
            raise LearningJournalError(f"learning intent {key} has a different key")
        if raw.get("status") not in _STATUSES:
            raise LearningJournalError(f"learning intent {key} has an invalid status")
        if not isinstance(raw.get("kind"), str) or not raw["kind"]:
            raise LearningJournalError(f"learning intent {key} has an invalid kind")
        if not isinstance(raw.get("attempts"), int) or int(raw["attempts"]) < 0:
            raise LearningJournalError(f"learning intent {key} has invalid attempts")
        cleanup_status = raw.get("cleanup_status")
        if cleanup_status is not None and cleanup_status not in {
            "pending",
            "succeeded",
            "failed",
        }:
            raise LearningJournalError(f"learning intent {key} has invalid cleanup status")
        if "post_processing" in raw and not isinstance(raw["post_processing"], dict):
            raise LearningJournalError(f"learning intent {key} has invalid post-processing data")
        return dict(raw)

    def ensure_pending(
        self, key: str, *, kind: str, identity: dict[str, object] | None = None
    ) -> dict[str, Any]:
        """Create a pending record once and return its current value."""
        if not key or not kind:
            raise ValueError("learning intent key and kind must be non-empty")
        identity_fields = dict(identity or {})
        reserved = _RESERVED_FIELDS.intersection(identity_fields)
        if reserved:
            names = ", ".join(sorted(reserved))
            raise ValueError(f"learning intent identity uses reserved fields: {names}")
        with file_lock(self.lock_path(key), require_exclusive=True):
            current = self.load(key)
            if current is not None:
                if current.get("kind") != kind:
                    raise ValueError("learning intent key was reused for another kind")
                changed = False
                for field, value in identity_fields.items():
                    if field in current and current[field] != value:
                        raise ValueError(f"learning intent identity changed field: {field}")
                    if field not in current:
                        current[field] = value
                        changed = True
                if (
                    isinstance(current.get("post_processing"), dict)
                    and current.get("cleanup_status") is None
                ):
                    current["cleanup_status"] = "pending"
                    changed = True
                if changed:
                    current["updated_at"] = datetime.now(UTC).isoformat()
                    self._write(key, current)
                return current
            now = datetime.now(UTC).isoformat()
            record = {
                "key": key,
                "kind": kind,
                "status": "pending",
                "attempts": 0,
                "created_at": now,
                "updated_at": now,
                **identity_fields,
            }
            if "post_processing" in identity_fields:
                record["cleanup_status"] = "pending"
            self._write(key, record)
            return record

    def incomplete_for_issue(self, *, repo: str, issue: int) -> list[dict[str, Any]]:
        """Return bounded nonterminal intent records for one issue."""
        records: list[dict[str, Any]] = []
        for raw in self._scan_nonterminal(repo=repo):
            if raw.get("issue") == issue:
                records.append(raw)
        return records

    def incomplete_for_repo(self, *, repo: str) -> Iterator[dict[str, Any]]:
        """Yield nonterminal records for one repository without a bulk list."""
        yield from self._scan_nonterminal(repo=repo)

    def _scan_nonterminal(self, *, repo: str) -> Iterator[dict[str, Any]]:
        for path in sorted(self._state_dir_provider().glob("learning-intent-*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                logger.warning("Could not scan learning intent %s: %s", path.name, exc)
                continue
            if (
                isinstance(raw, dict)
                and raw.get("repo") == repo
                and (
                    raw.get("status") in {"pending", "claimed"}
                    or (
                        isinstance(raw.get("post_processing"), dict)
                        and raw.get("cleanup_status") != "succeeded"
                        and raw.get("cleanup_status") != "failed"
                    )
                )
            ):
                yield dict(raw)

    def disable(self, key: str) -> dict[str, Any] | None:
        """Mark reconstructed nonterminal work disabled without host dispatch."""
        claim_lock = file_lock(self.claim_lock_path(key), blocking=False, require_exclusive=True)
        try:
            claim_lock.__enter__()
        except LockUnavailableError:
            return None
        try:
            with file_lock(self.lock_path(key), require_exclusive=True):
                record = self.load(key)
                if record is None:
                    raise KeyError(key)
                if record["status"] not in {"pending", "claimed"}:
                    return record
                record.update(
                    status="failed",
                    error="learning_disabled",
                    updated_at=datetime.now(UTC).isoformat(),
                )
                self._write(key, record)
                return record
        finally:
            claim_lock.__exit__(None, None, None)

    def claim(self, key: str) -> bool:
        """Commit ``pending -> claimed`` and return whether this call won."""
        claim_lock = file_lock(self.claim_lock_path(key), blocking=False, require_exclusive=True)
        try:
            claim_lock.__enter__()
        except LockUnavailableError:
            return False
        keep_lock = False
        try:
            with file_lock(self.lock_path(key), require_exclusive=True):
                record = self.load(key)
                if record is None:
                    raise KeyError(key)
                if record["status"] != "pending":
                    return False
                record["status"] = "claimed"
                record["attempts"] = int(record.get("attempts", 0)) + 1
                record["updated_at"] = datetime.now(UTC).isoformat()
                self._write(key, record)
            with self._claim_locks_guard:
                self._claim_locks[key] = claim_lock
            keep_lock = True
            return True
        finally:
            if not keep_lock:
                claim_lock.__exit__(None, None, None)

    def claim_is_active(self, key: str) -> bool:
        """Return whether another live owner holds the delivery claim lock."""
        probe = file_lock(self.claim_lock_path(key), blocking=False, require_exclusive=True)
        try:
            probe.__enter__()
        except LockUnavailableError:
            return True
        probe.__exit__(None, None, None)
        return False

    def fail_abandoned_claim(self, key: str, *, error: str) -> dict[str, Any] | None:
        """Fail an abandoned claim, or return ``None`` if a live owner won."""
        claim_lock = file_lock(self.claim_lock_path(key), blocking=False, require_exclusive=True)
        try:
            claim_lock.__enter__()
        except LockUnavailableError:
            return None
        try:
            with file_lock(self.lock_path(key), require_exclusive=True):
                record = self.load(key)
                if record is None:
                    raise KeyError(key)
                if record["status"] != "claimed":
                    return record
                record.update(
                    status="failed",
                    updated_at=datetime.now(UTC).isoformat(),
                    error=error[:1000],
                )
                self._write(key, record)
                return record
        finally:
            claim_lock.__exit__(None, None, None)

    def finish(
        self,
        key: str,
        *,
        succeeded: bool,
        error: str = "",
        receipt_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Commit a claimed intent to a terminal result."""
        self._require_claimed(key)
        self._require_local_claim(key)
        with file_lock(self.lock_path(key), require_exclusive=True):
            record = self._require_claimed(key)
            record["status"] = "succeeded" if succeeded else "failed"
            record["updated_at"] = datetime.now(UTC).isoformat()
            if error:
                record["error"] = error[:1000]
            if receipt_summary:
                record["receipt_summary"] = dict(receipt_summary)
            self._write(key, record)
        self._release_claim_lock(key)
        return record

    def retry(self, key: str, *, error: str) -> dict[str, Any]:
        """Return a known failed claim to pending for a bounded retry."""
        self._require_claimed(key)
        self._require_local_claim(key)
        with file_lock(self.lock_path(key), require_exclusive=True):
            record = self._require_claimed(key)
            record.update(
                status="pending",
                updated_at=datetime.now(UTC).isoformat(),
                error=error[:1000],
            )
            self._write(key, record)
        self._release_claim_lock(key)
        return record

    def finish_cleanup(self, key: str, *, succeeded: bool, error: str = "") -> dict[str, Any]:
        """Record that terminal cleanup reached a bounded outcome."""
        with file_lock(self.lock_path(key), require_exclusive=True):
            record = self.load(key)
            if record is None:
                raise KeyError(key)
            if not isinstance(record.get("post_processing"), dict):
                return record
            if record["status"] not in {"succeeded", "failed"}:
                raise ValueError("learning must be terminal before cleanup")
            record["cleanup_status"] = "succeeded" if succeeded else "failed"
            record["updated_at"] = datetime.now(UTC).isoformat()
            if error:
                record["cleanup_error"] = error[:1000]
            self._write(key, record)
            return record

    def _require_local_claim(self, key: str) -> None:
        with self._claim_locks_guard:
            if key not in self._claim_locks:
                raise ValueError("learning intent claim is not owned by this process")

    def _require_claimed(self, key: str) -> dict[str, Any]:
        record = self.load(key)
        if record is None:
            raise KeyError(key)
        if record["status"] != "claimed":
            raise ValueError(f"cannot finish learning intent from {record['status']}")
        return record

    def _release_claim_lock(self, key: str) -> None:
        with self._claim_locks_guard:
            claim_lock = self._claim_locks.pop(key, None)
        if claim_lock is not None:
            claim_lock.__exit__(None, None, None)

    def _write(self, key: str, record: dict[str, Any]) -> None:
        write_secure(self.path(key), json.dumps(record, indent=2, sort_keys=True) + "\n")
