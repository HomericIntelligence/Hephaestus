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

    def __init__(self, source: StageQueue, item: WorkItem, ticket: int) -> None:
        """Create an active lease owned by *source* for *item*."""
        self._source = source
        self.item = item
        self._ticket = ticket
        self._active = True

    def restore(self) -> None:
        """Return this active lease's item to its original FIFO position.

        A released lease is a harmless no-op.  This makes cleanup paths safe
        to repeat while preserving the item's exact-once queue ownership.
        """
        if not self._active:
            return

        self._source._restore_lease(self.item, self._ticket)
        self._active = False

    def handoff(self, destination: StageQueue) -> bool:
        """Move this active lease to *destination* when it has capacity.

        Destination admission happens before the source reservation is
        released.  Therefore a full destination leaves the item held by this
        lease and does not create a spill buffer or lose work.
        """
        if not self._active or not destination.offer(self.item):
            return False

        self._source._release_lease(self.item, self._ticket)
        self._active = False
        return True

    def release(self) -> None:
        """Release a lease when an external owner takes the item.

        Timers and the terminal ledger are neither source restores nor stage
        handoffs. Their coordinator-owned containers take responsibility for
        the item after this method returns, while the source slot becomes
        available without disturbing another ready item's FIFO position.
        """
        if not self._active:
            return

        self._source._release_lease(self.item, self._ticket)
        self._active = False


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
        self._items: deque[tuple[int, WorkItem]] = deque()
        self._leased: dict[int, int] = {}
        self._next_ticket = 0

    @property
    def capacity(self) -> int:
        """Return the maximum number of items the queue can hold."""
        return self._capacity

    @property
    def occupancy(self) -> int:
        """Return all ready and leased items currently held by the queue."""
        return len(self._items) + len(self._leased)

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

        A claimed item retains its source capacity until it is restored or
        handed off, while an immutable FIFO ticket lets other ready work use
        remaining capacity without allowing a retry to overtake it.
        """
        return self.occupancy < self._capacity

    def offer(self, item: WorkItem) -> bool:
        """Append an item when capacity is available.

        Returns:
            ``True`` when ``item`` was appended, otherwise ``False``.  A
            rejected offer leaves the queue unchanged so the caller retains
            ownership of the work item and can retry it later.

        """
        # Each ready item receives a monotonic ticket. A held lease keeps its
        # original ticket, so a later restore can reinsert before newer work
        # without wasting otherwise available capacity now.
        if not self.can_offer():
            return False

        self._items.append((self._next_ticket, item))
        self._next_ticket += 1
        return True

    def pop(self) -> WorkItem:
        """Remove and return the front item. Raises IndexError if empty."""
        _ticket, item = self._items.popleft()
        return item

    def claim(self) -> StageQueueLease | None:
        """Claim the FIFO-ready item without releasing this queue's capacity.

        The returned lease owns the item until it is restored or handed off.
        Several claims may be active concurrently up to :attr:`capacity`.
        Their immutable tickets retain the original FIFO order whenever any
        retry restores, while ready work inspection remains available through
        :meth:`snapshot`.
        """
        return self.claim_at(0)

    def claim_at(self, index: int) -> StageQueueLease | None:
        """Claim one ready item at *index* while preserving its retry position.

        Implementation's topological scheduler may need to execute a
        dependency that is ready behind the FIFO head. A selected lease keeps
        its immutable ticket, so restoring it returns the item to the exact
        order it occupied when claimed even while other work is active.
        """
        if not 0 <= index < len(self._items):
            return None

        ticket, item = self._items[index]
        del self._items[index]
        self._leased[id(item)] = ticket
        return StageQueueLease(self, item, ticket=ticket)

    def _restore_lease(self, item: WorkItem, ticket: int) -> None:
        """Restore one active lease in original FIFO order without opening capacity."""
        if self._leased.pop(id(item), None) != ticket:
            raise RuntimeError("StageQueue lease ticket mismatch")
        index = next(
            (
                position
                for position, (ready_ticket, _ready) in enumerate(self._items)
                if ticket < ready_ticket
            ),
            len(self._items),
        )
        self._items.insert(index, (ticket, item))

    def _release_lease(self, item: WorkItem, ticket: int) -> None:
        """Release one active lease after its item was admitted elsewhere."""
        if self._leased.pop(id(item), None) != ticket:
            raise RuntimeError("StageQueue lease ticket mismatch")

    def __len__(self) -> int:
        """Return the number of items in the queue."""
        return len(self._items)

    def snapshot(self) -> list[WorkItem]:
        """Return a copy of all items in queue order (for inspection/debugging)."""
        return [item for _ticket, item in self._items]
