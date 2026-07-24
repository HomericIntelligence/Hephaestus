"""Stage queues and cross-thread completion channel. Pure data, zero I/O (epic #1809).

The StageQueue is FIFO and deliberately not thread-safe — owned exclusively by
the coordinator thread. The CompletionQueue is the only cross-thread channel.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from queue import Empty, Full, Queue
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .work_item import WorkItem

@dataclass(frozen=True)
class CompletionRejection:
    """A completion that could not fit in the result channel."""

    handle: Any
    result: Any


class CompletionQueue(Queue[tuple[Any, Any]]):
    """Bounded completion channel with a bounded coordinator-owned fallback."""

    def __init__(self, *, capacity: int) -> None:
        """Create a queue with result and rejection-mailbox capacity."""
        if capacity < 1:
            raise ValueError("completion queue capacity must be positive")
        super().__init__(maxsize=capacity)
        self.capacity = capacity
        self._rejections: Queue[CompletionRejection] = Queue(maxsize=capacity)
        self._rejection_overflowed = False
        self._rejection_lock = threading.Lock()

    def offer(self, value: tuple[Any, Any]) -> bool:
        """Publish without blocking, retaining rejected work for the coordinator."""
        try:
            self.put_nowait(value)
        except Full:
            try:
                self._rejections.put_nowait(CompletionRejection(*value))
            except Full:
                with self._rejection_lock:
                    self._rejection_overflowed = True
            return False
        return True

    def take_rejections(self) -> tuple[list[CompletionRejection], bool]:
        """Drain rejected completions and return whether the mailbox overflowed."""
        rejected: list[CompletionRejection] = []
        while True:
            try:
                rejected.append(self._rejections.get_nowait())
            except Empty:
                break
        with self._rejection_lock:
            overflowed = self._rejection_overflowed
            self._rejection_overflowed = False
        return rejected, overflowed


class StageQueue:
    """FIFO queue of work items for a stage.

    Owned exclusively by the coordinator thread; not thread-safe.
    Used to route items through the pipeline stage by stage.
    """

    def __init__(self, *, capacity: int) -> None:
        """Initialize an empty queue with a positive item capacity."""
        if capacity < 1:
            raise ValueError("stage queue capacity must be positive")
        self.capacity = capacity
        self._items: deque[WorkItem] = deque()

    def push(self, item: WorkItem) -> bool:
        """Append an item, returning false when the queue is already full."""
        if len(self._items) >= self.capacity:
            return False
        self._items.append(item)
        return True

    def pop(self) -> WorkItem:
        """Remove and return the front item. Raises IndexError if empty."""
        return self._items.popleft()

    def __len__(self) -> int:
        """Return the number of items in the queue."""
        return len(self._items)

    def snapshot(self) -> list[WorkItem]:
        """Return a copy of all items in queue order (for inspection/debugging)."""
        return list(self._items)
