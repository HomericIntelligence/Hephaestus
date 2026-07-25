"""Stage queues and cross-thread completion channel. Pure data, zero I/O (epic #1809).

The StageQueue is FIFO and deliberately not thread-safe — owned exclusively by
the coordinator thread. The CompletionQueue is the only cross-thread channel.
"""

from __future__ import annotations

from collections import deque
from queue import Queue
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .work_item import WorkItem

# Payload is (JobHandle, JobResult) per docs/architecture.md §8;
# both types land with the worker pool (epic #1809 worker-pool slice), so the
# alias stays shape-only until then.
CompletionQueue = Queue[tuple[Any, Any]]


class StageQueue:
    """FIFO queue of work items for a stage.

    Owned exclusively by the coordinator thread; not thread-safe.
    Used to route items through the pipeline stage by stage.  Every queue has
    an explicit, positive capacity.  Callers that can defer admission should
    use :meth:`offer`, which reports a full queue without changing it.
    """

    def __init__(self, capacity: int) -> None:
        """Initialize an empty queue with a positive item capacity.

        Args:
            capacity: Maximum number of work items the queue may hold.

        Raises:
            ValueError: If ``capacity`` is not positive.

        """
        if capacity <= 0:
            raise ValueError("StageQueue capacity must be positive")

        self._capacity = capacity
        self._items: deque[WorkItem] = deque()

    @property
    def capacity(self) -> int:
        """Return the maximum number of items the queue can hold."""
        return self._capacity

    @property
    def occupancy(self) -> int:
        """Return the number of items currently held by the queue."""
        return len(self._items)

    def push(self, item: WorkItem) -> None:
        """Append an item or raise if the queue is full.

        Callers that need to retain an item for a later retry should use
        :meth:`offer` instead.

        Raises:
            OverflowError: If the queue has reached its capacity.

        """
        if not self.offer(item):
            raise OverflowError("StageQueue is full")

    def offer(self, item: WorkItem) -> bool:
        """Append an item when capacity is available.

        Returns:
            ``True`` when ``item`` was appended, otherwise ``False``.  A
            rejected offer leaves the queue unchanged so the caller retains
            ownership of the work item and can retry it later.

        """
        if len(self._items) >= self._capacity:
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
