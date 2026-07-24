"""Tests for StageQueue and CompletionQueue."""

import queue

import pytest

from hephaestus.automation.pipeline import (
    CompletionQueue,
    ItemKind,
    StageQueue,
    WorkItem,
)
from hephaestus.automation.pipeline.queues import CompletionRejection


class TestStageQueue:
    """Tests for StageQueue (FIFO, not thread-safe)."""

    def test_stage_queue_creation(self) -> None:
        """Create an empty StageQueue."""
        q = StageQueue(capacity=2)
        assert len(q) == 0
        assert q.capacity == 2

    def test_stage_queue_push_pop(self) -> None:
        """Push and pop items in FIFO order."""
        q = StageQueue(capacity=2)
        item1 = WorkItem(repo="repo1", kind=ItemKind.REPO)
        item2 = WorkItem(repo="repo2", kind=ItemKind.ISSUE, issue=1)

        assert q.push(item1)
        assert q.push(item2)

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

        assert q.push(item1)
        assert q.push(item2)
        assert q.push(item3)

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
            assert q.push(WorkItem(repo=f"repo{i}", kind=ItemKind.REPO))
            assert len(q) == i + 1

        for i in range(5, 0, -1):
            q.pop()
            assert len(q) == i - 1

    @pytest.mark.parametrize("capacity", [0, -1])
    def test_stage_queue_rejects_invalid_capacity(self, capacity: int) -> None:
        """A stage queue cannot be configured without storage."""
        with pytest.raises(ValueError):
            StageQueue(capacity=capacity)

    def test_stage_queue_rejects_burst_without_overflow_storage(self) -> None:
        """A full stage queue rejects work explicitly and retains its bound."""
        q = StageQueue(capacity=1)
        first = WorkItem(repo="repo1", kind=ItemKind.REPO)
        second = WorkItem(repo="repo2", kind=ItemKind.REPO)

        assert q.push(first)
        assert q.push(second) is False
        assert q.snapshot() == [first]


class TestCompletionQueue:
    """Tests for the bounded cross-thread completion channel."""

    def test_completion_queue_type(self) -> None:
        """CompletionQueue is a queue.Queue type alias."""
        cq = CompletionQueue(capacity=2)
        assert isinstance(cq, queue.Queue)
        assert cq.maxsize == 2
        assert cq.capacity == 2

    def test_completion_queue_put_get(self) -> None:
        """Put and get items from CompletionQueue."""
        cq = CompletionQueue(capacity=1)
        item = WorkItem(repo="repo", kind=ItemKind.REPO)
        data = (item, "completed")

        assert cq.offer(data)
        result = cq.get(timeout=1)

        assert result == data

    def test_completion_queue_empty_get_blocks(self) -> None:
        """Getting from empty queue times out."""
        cq = CompletionQueue(capacity=1)
        with pytest.raises(queue.Empty):
            cq.get(timeout=0.1)

    def test_completion_queue_rejection_is_retained_for_coordinator(self) -> None:
        """A full result channel preserves the rejected completion in its mailbox."""
        cq = CompletionQueue(capacity=1)

        assert cq.offer(("first", "ok"))
        assert cq.offer(("second", "result")) is False

        rejected, overflowed = cq.take_rejections()

        assert rejected == [CompletionRejection("second", "result")]
        assert overflowed is False
        assert cq.get_nowait() == ("first", "ok")

    def test_completion_queue_marks_rejection_mailbox_overflow(self) -> None:
        """The bounded fallback reports when it cannot retain every rejection."""
        cq = CompletionQueue(capacity=1)

        assert cq.offer(("first", "ok"))
        assert cq.offer(("second", "result")) is False
        assert cq.offer(("third", "result")) is False

        rejected, overflowed = cq.take_rejections()

        assert rejected == [CompletionRejection("second", "result")]
        assert overflowed is True

    @pytest.mark.parametrize("capacity", [0, -1])
    def test_completion_queue_rejects_invalid_capacity(self, capacity: int) -> None:
        """A completion queue cannot be configured without storage."""
        with pytest.raises(ValueError):
            CompletionQueue(capacity=capacity)
