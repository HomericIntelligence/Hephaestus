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
        """Create an empty StageQueue."""
        q = StageQueue()
        assert len(q) == 0

    def test_stage_queue_push_pop(self) -> None:
        """Push and pop items in FIFO order."""
        q = StageQueue()
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
        q = StageQueue()
        with pytest.raises(IndexError):
            q.pop()

    def test_stage_queue_snapshot(self) -> None:
        """snapshot() returns a list copy of all items."""
        q = StageQueue()
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
        q = StageQueue()
        item = WorkItem(repo="repo", kind=ItemKind.REPO)
        q.push(item)

        snap = q.snapshot()
        snap.clear()

        assert len(q) == 1
        assert q.snapshot() == [item]

    def test_stage_queue_len_tracking(self) -> None:
        """len() tracks the queue size accurately."""
        q = StageQueue()
        assert len(q) == 0

        for i in range(5):
            q.push(WorkItem(repo=f"repo{i}", kind=ItemKind.REPO))
            assert len(q) == i + 1

        for i in range(5, 0, -1):
            q.pop()
            assert len(q) == i - 1


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
