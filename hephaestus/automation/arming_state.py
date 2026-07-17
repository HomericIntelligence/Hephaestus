"""On-disk arming-record persistence for the drive-green flow.

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
from pathlib import Path
from typing import Any

from hephaestus.io.utils import write_secure

from ._review_utils import load_state_file

logger = logging.getLogger(__name__)

# ``armed_at`` records the intent written before the remote GraphQL mutation;
# it is deliberately *not* evidence that GitHub accepted auto-merge.  The
# pipeline therefore persists this small state machine so restart recovery can
# distinguish a pre-RPC record from an arm that was read back from GitHub.
ARM_STATUS_PREPARED = "prepared"
ARM_STATUS_CONFIRMED = "confirmed"


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
