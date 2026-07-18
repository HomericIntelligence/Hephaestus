"""Host-local ownership guard for loop-owned strict PR review.

The automation-loop approval decision is deliberately CI-free.  This guard
serializes strict review for one repository/PR across cooperating loop
processes on the same host, so two same-head reviewer runs cannot race their
opposing verdicts into the single loop-owned approval label.
"""

from __future__ import annotations

import threading
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path

from hephaestus.utils.file_lock import LockUnavailableError, file_lock


class StrictReviewGuard:
    """Hold one cross-process strict-review claim per ``(org, repo, PR)``."""

    def __init__(self, lock_dir: Path | None = None) -> None:
        """Create a guard, optionally anchoring its sentinels in ``lock_dir``."""
        self._lock_dir = lock_dir or Path.home() / ".cache" / "hephaestus" / "strict-review-locks"
        self._owners: dict[tuple[str, str, int], tuple[int, ExitStack]] = {}
        self._mutex = threading.Lock()

    @staticmethod
    def _key(org: str, repo: str, pr_number: int) -> tuple[str, str, int]:
        return (org.casefold(), repo.casefold(), pr_number)

    def try_claim(self, org: str, repo: str, pr_number: int, owner: int) -> bool:
        """Acquire a non-blocking local claim, returning false when held."""
        key = self._key(org, repo, pr_number)
        lock_name = sha256(f"{key[0]}:{key[1]}:{pr_number}".encode()).hexdigest()
        with self._mutex:
            existing = self._owners.get(key)
            if existing is not None:
                return existing[0] == owner
            stack = ExitStack()
            try:
                stack.enter_context(
                    file_lock(
                        self._lock_dir / f"{lock_name}.lock",
                        blocking=False,
                        require_exclusive=True,
                    )
                )
            except (LockUnavailableError, OSError, RuntimeError):
                stack.close()
                return False
            self._owners[key] = (owner, stack)
            return True

    def release(self, org: str, repo: str, pr_number: int, owner: int) -> None:
        """Release a claim owned by this work item, if it still owns one."""
        key = self._key(org, repo, pr_number)
        with self._mutex:
            existing = self._owners.get(key)
            if existing is None or existing[0] != owner:
                return
            _, stack = self._owners.pop(key)
            stack.close()

    def release_all(self) -> None:
        """Release every local claim during coordinator shutdown."""
        with self._mutex:
            entries = list(self._owners.values())
            self._owners.clear()
        for _, stack in entries:
            stack.close()
