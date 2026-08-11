"""On-disk post-merge learning-record persistence for the drive-green flow.

Owns the ``drive-green-armed-<n>.json`` files under the driver's
``state_dir``. Extracted from
:class:`hephaestus.automation.ci_driver.CIDriver` as part of the #1178
decomposition. Best-effort: IO/JSON errors are logged and swallowed — no
caller is ever gated on arming-record availability.

The class deliberately mirrors the inline methods that lived on
``CIDriver`` (``_arming_state_path``, ``_load_arming_state``,
``_save_arming_state``, ``_clear_arming_state``) so the driver can
delegate without changing call sites.

The store resolves ``state_dir`` through a zero-argument provider rather
than capturing it at construction. ``CIDriver.state_dir`` is reassigned
after ``__init__`` by characterization tests (and could be in production),
so the store must always read the *current* value, not a snapshot taken
before the reassignment.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from hephaestus.io.utils import write_secure
from hephaestus.utils.file_lock import file_lock

from ._review_utils import load_state_file

logger = logging.getLogger(__name__)

_LEARNING_STATUSES = frozenset({"pending", "claimed", "succeeded", "failed"})


class LearningJournalStore:
    """Persist strict, bounded state for auxiliary learning intents."""

    def __init__(self, state_dir_provider: Callable[[], Path]) -> None:
        """Use the current automation state directory on every operation."""
        self._state_dir_provider = state_dir_provider

    @staticmethod
    def _digest(key: str) -> str:
        """Return a path-safe stable identifier for an intent key."""
        return sha256(key.encode("utf-8")).hexdigest()

    def path(self, key: str) -> Path:
        """Return the journal record path for ``key``."""
        return self._state_dir_provider() / f"learning-intent-{self._digest(key)}.json"

    def lock_path(self, key: str) -> Path:
        """Return the stable sibling lock path for ``key``."""
        return self.path(key).with_suffix(".lock")

    def load(self, key: str) -> dict[str, Any] | None:
        """Read one valid journal record, or return ``None``."""
        path = self.path(key)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("Could not read learning intent %s: %s", key, exc)
            return None
        if not isinstance(raw, dict) or raw.get("key") != key:
            return None
        if raw.get("status") not in _LEARNING_STATUSES:
            return None
        return dict(raw)

    def ensure_pending(
        self, key: str, *, kind: str, identity: dict[str, object] | None = None
    ) -> dict[str, Any]:
        """Create a pending record once and return its current value."""
        if not key or not kind:
            raise ValueError("learning intent key and kind must be non-empty")
        with file_lock(self.lock_path(key), require_exclusive=True):
            current = self.load(key)
            if current is not None:
                if current.get("kind") != kind:
                    raise ValueError("learning intent key was reused for another kind")
                return current
            now = datetime.now(UTC).isoformat()
            record = {
                "key": key,
                "kind": kind,
                "status": "pending",
                "attempts": 0,
                "created_at": now,
                "updated_at": now,
                **dict(identity or {}),
            }
            self._write(key, record)
            return record

    def incomplete_for_issue(self, *, repo: str, issue: int) -> list[dict[str, Any]]:
        """Return bounded nonterminal intent records for one issue."""
        state_dir = self._state_dir_provider()
        records: list[dict[str, Any]] = []
        for path in sorted(state_dir.glob("learning-intent-*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if (
                isinstance(raw, dict)
                and raw.get("repo") == repo
                and raw.get("issue") == issue
                and raw.get("status") in {"pending", "claimed"}
            ):
                records.append(dict(raw))
        return records

    def disable(self, key: str) -> dict[str, Any]:
        """Mark reconstructed nonterminal work disabled without host dispatch."""
        with file_lock(self.lock_path(key), require_exclusive=True):
            record = self.load(key)
            if record is None:
                raise KeyError(key)
            if record["status"] not in {"pending", "claimed"}:
                return record
            record["status"] = "failed"
            record["error"] = "learning_disabled"
            record["updated_at"] = datetime.now(UTC).isoformat()
            self._write(key, record)
            return record

    def claim(self, key: str) -> bool:
        """Commit ``pending → claimed`` and return whether this call won."""
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
            return True

    def finish(
        self,
        key: str,
        *,
        succeeded: bool,
        error: str = "",
        receipt_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Commit a claimed intent to a terminal result."""
        with file_lock(self.lock_path(key), require_exclusive=True):
            record = self.load(key)
            if record is None:
                raise KeyError(key)
            if record["status"] != "claimed":
                raise ValueError(f"cannot finish learning intent from {record['status']}")
            record["status"] = "succeeded" if succeeded else "failed"
            record["updated_at"] = datetime.now(UTC).isoformat()
            if error:
                record["error"] = error[:1000]
            if receipt_summary:
                record["receipt_summary"] = dict(receipt_summary)
            self._write(key, record)
            return record

    def retry(self, key: str, *, error: str) -> dict[str, Any]:
        """Return a known failed claim to pending for a bounded retry."""
        with file_lock(self.lock_path(key), require_exclusive=True):
            record = self.load(key)
            if record is None:
                raise KeyError(key)
            if record["status"] != "claimed":
                raise ValueError(f"cannot retry learning intent from {record['status']}")
            record["status"] = "pending"
            record["updated_at"] = datetime.now(UTC).isoformat()
            record["error"] = error[:1000]
            self._write(key, record)
            return record

    def _write(self, key: str, record: dict[str, Any]) -> None:
        write_secure(self.path(key), json.dumps(record, indent=2, sort_keys=True) + "\n")


class ArmingStateStore:
    """Reads/writes per-issue drive-green arming records under ``state_dir``.

    Attributes:
        _state_dir_provider: Zero-arg callable returning the live directory
            that holds the ``drive-green-armed-<n>.json`` files.

    """

    def __init__(self, state_dir_provider: Callable[[], Path]) -> None:
        """Initialize the store.

        Args:
            state_dir_provider: Zero-argument callable returning the directory
                to read/write arming-record files. Resolved on every call so
                the store tracks reassignments of the owner's ``state_dir``.
                The directory is NOT created here — the owning ``CIDriver`` is
                responsible for ``mkdir(parents=True, exist_ok=True)`` so the
                record paths remain identical to the pre-extraction layout.

        """
        self._state_dir_provider = state_dir_provider

    def path(self, issue_number: int) -> Path:
        """Return the arming-record path for ``issue_number``."""
        return self._state_dir_provider() / f"drive-green-armed-{issue_number}.json"

    def learn_claim_lock_path(self, issue_number: int) -> Path:
        """Return the stable sentinel used to serialize one /learn claim.

        This lock is intentionally a sibling rather than the JSON record
        itself: replacing the record atomically while a lock is held on that
        record's inode would let a second process lock the replacement inode.
        The sentinel is never replaced or removed by record persistence.
        """
        return self._state_dir_provider() / f"drive-green-armed-{issue_number}.learn.lock"

    def load(self, issue_number: int) -> dict[str, Any] | None:
        """Return the parsed arming record for ``issue_number`` or ``None``.

        Routes through the canonical ``load_state_file`` helper (raw-dict
        overload) so malformed-file handling matches the rest of the
        automation state stores (#1432).
        """
        record = load_state_file(
            self._state_dir_provider(),
            "drive-green-armed",
            issue_number,
            state_logger=logger,
        )
        return dict(record) if record is not None else None

    def save(self, issue_number: int, record: dict[str, Any]) -> bool:
        """Persist the arming record and report whether the write succeeded."""
        path = self.path(issue_number)
        try:
            write_secure(path, json.dumps(record, indent=2, sort_keys=True))
        except OSError as exc:
            logger.warning(
                "Could not write arming record for issue #%s: %s",
                issue_number,
                exc,
            )
            return False
        return True

    def clear(self, issue_number: int) -> None:
        """Delete the arming record for ``issue_number`` if present."""
        path = self.path(issue_number)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "Could not delete arming record for issue #%s: %s",
                issue_number,
                exc,
            )
