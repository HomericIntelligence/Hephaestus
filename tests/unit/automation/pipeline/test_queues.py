"""Tests for StageQueue and CompletionQueue."""

import queue

import pytest

from hephaestus.automation.pipeline import (
    CompletionQueue,
    ItemKind,
    StageQueue,
    WorkItem,
)


class TestStageQueue:
    """Tests for StageQueue (FIFO, not thread-safe)."""

    def test_stage_queue_creation(self) -> None:
        """Create an empty StageQueue with observable bounds."""
        q = StageQueue(capacity=3)
        assert len(q) == 0
        assert q.capacity == 3
        assert q.occupancy == 0

    @pytest.mark.parametrize("capacity", (0, -1))
    def test_stage_queue_rejects_non_positive_capacity(self, capacity: int) -> None:
        """A stage queue must have a positive, explicit capacity."""
        with pytest.raises(ValueError, match="capacity"):
            StageQueue(capacity=capacity)

    def test_stage_queue_push_pop(self) -> None:
        """Push and pop items in FIFO order."""
        q = StageQueue(capacity=3)
        item1 = WorkItem(repo="repo1", kind=ItemKind.REPO)
        item2 = WorkItem(repo="repo2", kind=ItemKind.ISSUE, issue=1)

        q.push(item1)
        q.push(item2)

        assert len(q) == 2
        first = q.pop()
        assert first == item1
        second = q.pop()
        assert second == item2
        assert len(q) == 0

    def test_stage_queue_pop_empty_raises(self) -> None:
        """Popping from empty queue raises IndexError."""
        q = StageQueue(capacity=1)
        with pytest.raises(IndexError):
            q.pop()

    def test_stage_queue_snapshot(self) -> None:
        """snapshot() returns a list copy of all items."""
        q = StageQueue(capacity=3)
        item1 = WorkItem(repo="repo1", kind=ItemKind.REPO)
        item2 = WorkItem(repo="repo2", kind=ItemKind.ISSUE, issue=1)
        item3 = WorkItem(repo="repo3", kind=ItemKind.PR, pr=2)

        q.push(item1)
        q.push(item2)
        q.push(item3)

        snap = q.snapshot()
        assert len(snap) == 3
        assert snap == [item1, item2, item3]

    def test_stage_queue_snapshot_returns_copy(self) -> None:
        """Mutating snapshot() does not affect the queue."""
        q = StageQueue(capacity=1)
        item = WorkItem(repo="repo", kind=ItemKind.REPO)
        q.push(item)

        snap = q.snapshot()
        snap.clear()

        assert len(q) == 1
        assert q.snapshot() == [item]

    def test_stage_queue_len_tracking(self) -> None:
        """len() tracks the queue size accurately."""
        q = StageQueue(capacity=5)
        assert len(q) == 0

        for i in range(5):
            q.push(WorkItem(repo=f"repo{i}", kind=ItemKind.REPO))
            assert len(q) == i + 1

        for i in range(5, 0, -1):
            q.pop()
            assert len(q) == i - 1

    def test_stage_queue_refuses_offer_above_capacity_without_mutation(self) -> None:
        """A full queue refuses another item without evicting accepted work."""
        q = StageQueue(capacity=2)
        first = WorkItem(repo="repo1", kind=ItemKind.REPO)
        second = WorkItem(repo="repo2", kind=ItemKind.ISSUE, issue=2)
        overflow = WorkItem(repo="repo3", kind=ItemKind.PR, pr=3)

        assert q.offer(first) is True
        assert q.offer(second) is True
        assert q.occupancy == 2

        assert q.offer(overflow) is False
        assert q.occupancy == 2
        assert q.snapshot() == [first, second]

    def test_stage_queue_retains_fifo_after_temporary_full_condition(self) -> None:
        """A rejected offer does not reorder later admitted work."""
        q = StageQueue(capacity=2)
        first = WorkItem(repo="repo1", kind=ItemKind.REPO)
        second = WorkItem(repo="repo2", kind=ItemKind.ISSUE, issue=2)
        rejected = WorkItem(repo="repo3", kind=ItemKind.PR, pr=3)
        admitted_later = WorkItem(repo="repo4", kind=ItemKind.REPO)

        assert q.offer(first) is True
        assert q.offer(second) is True
        assert q.offer(rejected) is False
        assert q.pop() == first

        assert q.offer(admitted_later) is True
        assert [q.pop(), q.pop()] == [second, admitted_later]


class TestCompletionQueue:
    """Tests for CompletionQueue type alias."""

    def test_completion_queue_type(self) -> None:
        """CompletionQueue is a queue.Queue type alias."""
        cq = CompletionQueue()
        assert isinstance(cq, queue.Queue)

    def test_completion_queue_put_get(self) -> None:
        """Put and get items from CompletionQueue."""
        cq = CompletionQueue()
        item = WorkItem(repo="repo", kind=ItemKind.REPO)
        data = (item, "completed")

        cq.put(data)
        result = cq.get(timeout=1)

        assert result == data

    def test_completion_queue_empty_get_blocks(self) -> None:
        """Getting from empty queue times out."""
        cq = CompletionQueue()
        with pytest.raises(queue.Empty):
            cq.get(timeout=0.1)
