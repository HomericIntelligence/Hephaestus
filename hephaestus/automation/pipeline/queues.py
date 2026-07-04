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

# Payload is (JobHandle, JobResult) per docs/AUTOMATION_LOOP_ARCHITECTURE.md;
# both types land with the worker pool (epic #1809 worker-pool slice), so the
# alias stays shape-only until then.
CompletionQueue = Queue[tuple[Any, Any]]


class StageQueue:
    """FIFO queue of work items for a stage.

    Owned exclusively by the coordinator thread; not thread-safe.
    Used to route items through the pipeline stage by stage.
    """

    def __init__(self) -> None:
        """Initialize an empty queue."""
        self._items: deque[WorkItem] = deque()

    def push(self, item: WorkItem) -> None:
        """Append an item to the queue."""
        self._items.append(item)

    def pop(self) -> WorkItem:
        """Remove and return the front item. Raises IndexError if empty."""
        return self._items.popleft()

    def __len__(self) -> int:
        """Return the number of items in the queue."""
        return len(self._items)

    def snapshot(self) -> list[WorkItem]:
        """Return a copy of all items in queue order (for inspection/debugging)."""
        return list(self._items)
