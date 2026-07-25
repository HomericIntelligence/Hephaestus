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


class StageQueueLease:
    """A temporary, capacity-reserving claim on one stage-queue item.

    Leases are coordinator-thread objects: they are not thread-safe and their
    lifetime is bounded by one successful :meth:`handoff` or :meth:`restore`.
    A failed handoff intentionally leaves the lease active so the caller can
    retry after the destination makes capacity available.
    """

    def __init__(self, source: StageQueue, item: WorkItem) -> None:
        """Create an active lease owned by *source* for *item*."""
        self._source = source
        self.item = item
        self._active = True

    def restore(self) -> None:
        """Return this active lease's item to the front of its source queue.

        A released lease is a harmless no-op.  This makes cleanup paths safe
        to repeat while preserving the item's exact-once queue ownership.
        """
        if not self._active:
            return

        self._source._restore_lease(self.item)
        self._active = False

    def handoff(self, destination: StageQueue) -> bool:
        """Move this active lease to *destination* when it has capacity.

        Destination admission happens before the source reservation is
        released.  Therefore a full destination leaves the item held by this
        lease and does not create a spill buffer or lose work.
        """
        if not self._active or not destination.offer(self.item):
            return False

        self._source._release_lease()
        self._active = False
        return True


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
        self._held = 0

    @property
    def capacity(self) -> int:
        """Return the maximum number of items the queue can hold."""
        return self._capacity

    @property
    def occupancy(self) -> int:
        """Return all ready and leased items currently held by the queue."""
        return len(self._items) + self._held

    def push(self, item: WorkItem) -> None:
        """Append an item or raise if the queue is full.

        Callers that need to retain an item for a later retry should use
        :meth:`offer` instead.

        Raises:
            OverflowError: If the queue has reached its capacity.

        """
        if not self.offer(item):
            raise OverflowError("StageQueue is full")

    def can_offer(self) -> bool:
        """Return whether :meth:`offer` can append one new item now.

        A claimed item retains the source queue's FIFO ownership until it is
        restored or handed off.  New producers must not insert behind that
        held item, even if the numeric capacity has remaining room.
        """
        return not self._held and self.occupancy < self._capacity

    def offer(self, item: WorkItem) -> bool:
        """Append an item when capacity is available.

        Returns:
            ``True`` when ``item`` was appended, otherwise ``False``.  A
            rejected offer leaves the queue unchanged so the caller retains
            ownership of the work item and can retry it later.

        """
        # A held lease may need to return to the front of this queue.  Do not
        # admit newer work ahead of it, even when the numeric bound has spare
        # capacity; admission resumes only once every lease is released.
        if not self.can_offer():
            return False

        self._items.append(item)
        return True

    def pop(self) -> WorkItem:
        """Remove and return the front item. Raises IndexError if empty."""
        return self._items.popleft()

    def claim(self) -> StageQueueLease | None:
        """Claim the FIFO-ready item without releasing this queue's capacity.

        The returned lease owns the item until it is restored or handed off.
        Only one lease may be active at a time, so an outstanding lease also
        returns ``None`` even when later items are ready. Ready work inspection
        remains available through :meth:`snapshot`, but the claim continues to
        count toward :attr:`occupancy`.
        """
        # A lease can restore its item to the front.  Serializing claims keeps
        # that restoration ahead of every item that was already ready, rather
        # than reversing two independently restored leases.
        if self._held or not self._items:
            return None

        self._held += 1
        return StageQueueLease(self, self._items.popleft())

    def _restore_lease(self, item: WorkItem) -> None:
        """Restore one active lease at the front without changing occupancy."""
        self._held -= 1
        self._items.appendleft(item)

    def _release_lease(self) -> None:
        """Release one active lease after its item was admitted elsewhere."""
        self._held -= 1

    def __len__(self) -> int:
        """Return the number of items in the queue."""
        return len(self._items)

    def snapshot(self) -> list[WorkItem]:
        """Return a copy of all items in queue order (for inspection/debugging)."""
        return list(self._items)
