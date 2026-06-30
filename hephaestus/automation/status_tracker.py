"""Thread-safe status tracking for parallel workers.

Provides slot-based tracking with condition variables for coordination.
"""

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class SlotUnavailable(RuntimeError):  # noqa: N818  # not an error-state; a control-flow signal
    """Raised by :meth:`StatusTracker.slot` when no slot is acquired before timeout."""


class StatusTracker:
    """Thread-safe tracker for worker status slots.

    Manages a fixed number of worker slots with condition variable
    coordination for efficient waiting.
    """

    def __init__(self, num_slots: int) -> None:
        """Initialize status tracker.

        Args:
            num_slots: Number of worker slots to manage

        """
        self.num_slots = num_slots
        self.slots: list[str | None] = [None] * num_slots
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)

    def acquire_slot(self, timeout: float | None = None) -> int | None:
        """Acquire an available slot, waiting if necessary.

        Args:
            timeout: Optional timeout in seconds

        Returns:
            Slot index or None if timeout

        """
        with self.condition:
            while True:
                # Find available slot
                for i, slot in enumerate(self.slots):
                    if slot is None:
                        self.slots[i] = "acquired"
                        return i

                # No slots available, wait
                if not self.condition.wait(timeout=timeout):
                    logger.warning("Slot acquisition timed out")
                    return None

    @contextmanager
    def slot(
        self,
        initial_msg: str = "",
        *,
        timeout: float | None = None,
        release_delay: float = 0.0,
    ) -> Iterator[int]:
        """Acquire a slot for the duration of the ``with`` block, then release it.

        Args:
            initial_msg: If non-empty, set as the slot's status immediately
                after acquisition.
            timeout: Optional acquisition timeout in seconds (see acquire_slot).
            release_delay: Seconds to sleep before releasing the slot. Used by
                workers that need a brief settle delay (default 0.0 = no sleep).

        Yields:
            The acquired slot index (always a valid ``int``).

        Raises:
            SlotUnavailable: If no slot becomes available before ``timeout``.

        """
        slot_id = self.acquire_slot(timeout=timeout)
        if slot_id is None:
            raise SlotUnavailable("Failed to acquire worker slot")
        try:
            if initial_msg:
                self.update_slot(slot_id, initial_msg)
            yield slot_id
        finally:
            if release_delay > 0:
                time.sleep(release_delay)
            self.release_slot(slot_id)

    def release_slot(self, slot_id: int) -> None:
        """Release a slot.

        Args:
            slot_id: Slot index to release

        """
        with self.condition:
            if 0 <= slot_id < self.num_slots:
                self.slots[slot_id] = None
                self.condition.notify_all()  # Wake all waiters
            else:
                logger.error("Invalid slot_id: %d", slot_id)

    def update_slot(self, slot_id: int, status: str) -> None:
        """Update slot status message.

        Args:
            slot_id: Slot index
            status: Status message

        """
        with self.lock:
            if 0 <= slot_id < self.num_slots:
                self.slots[slot_id] = status
            else:
                logger.error("Invalid slot_id: %d", slot_id)

    def get_status(self) -> list[str | None]:
        """Get current status of all slots.

        Returns:
            List of slot statuses

        """
        with self.lock:
            return self.slots.copy()

    def get_active_count(self) -> int:
        """Get count of active (non-None) slots.

        Returns:
            Number of active slots

        """
        with self.lock:
            return sum(1 for slot in self.slots if slot is not None)

    def wait_for_available(self, timeout: float | None = None) -> bool:
        """Wait until at least one slot is available.

        Args:
            timeout: Optional timeout in seconds

        Returns:
            True if slot became available, False on timeout

        """
        with self.condition:
            while all(slot is not None for slot in self.slots):
                if not self.condition.wait(timeout=timeout):
                    return False
            return True

    def wait_all_complete(self, timeout: float | None = None) -> bool:
        """Wait until all slots are released.

        Args:
            timeout: Optional timeout in seconds

        Returns:
            True if all complete, False on timeout

        """
        with self.condition:
            while any(slot is not None for slot in self.slots):
                if not self.condition.wait(timeout=timeout):
                    return False
            return True

    def clear(self) -> None:
        """Clear all slot statuses."""
        with self.condition:
            self.slots = [None] * self.num_slots
            self.condition.notify_all()  # Wake all waiters
