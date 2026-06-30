"""Tests for status tracker."""

import threading
import time

import pytest

from hephaestus.automation.status_tracker import StatusTracker


class TestStatusTracker:
    """Tests for StatusTracker class."""

    def test_initialization(self) -> None:
        """Test tracker initialization."""
        tracker = StatusTracker(num_slots=3)

        assert tracker.num_slots == 3
        assert len(tracker.slots) == 3
        assert all(slot is None for slot in tracker.slots)

    def test_acquire_slot(self) -> None:
        """Test acquiring a slot."""
        tracker = StatusTracker(num_slots=2)

        slot_id = tracker.acquire_slot()

        assert slot_id is not None
        assert 0 <= slot_id < 2
        assert tracker.slots[slot_id] == "acquired"

    def test_acquire_all_slots(self) -> None:
        """Test acquiring all available slots."""
        tracker = StatusTracker(num_slots=2)

        slot1 = tracker.acquire_slot()
        slot2 = tracker.acquire_slot()

        assert slot1 is not None
        assert slot2 is not None
        assert slot1 != slot2
        assert tracker.get_active_count() == 2

    def test_acquire_slot_timeout(self) -> None:
        """Test slot acquisition timeout when all slots occupied."""
        tracker = StatusTracker(num_slots=1)

        # Acquire the only slot
        slot1 = tracker.acquire_slot()
        assert slot1 is not None

        # Try to acquire when full - should timeout
        slot2 = tracker.acquire_slot(timeout=0.1)
        assert slot2 is None

    def test_release_slot(self) -> None:
        """Test releasing a slot."""
        tracker = StatusTracker(num_slots=2)

        slot_id = tracker.acquire_slot()
        assert slot_id is not None

        tracker.release_slot(slot_id)

        assert tracker.slots[slot_id] is None
        assert tracker.get_active_count() == 0

    def test_release_invalid_slot(self) -> None:
        """Test releasing invalid slot ID."""
        tracker = StatusTracker(num_slots=2)

        # Should not crash, just log error
        tracker.release_slot(999)
        tracker.release_slot(-1)

    def test_update_slot(self) -> None:
        """Test updating slot status."""
        tracker = StatusTracker(num_slots=2)

        slot_id = tracker.acquire_slot()
        assert slot_id is not None
        tracker.update_slot(slot_id, "Processing issue #123")

        assert tracker.slots[slot_id] == "Processing issue #123"

    def test_update_invalid_slot(self) -> None:
        """Test updating invalid slot ID."""
        tracker = StatusTracker(num_slots=2)

        # Should not crash, just log error
        tracker.update_slot(999, "invalid")
        tracker.update_slot(-1, "invalid")

    def test_get_status(self) -> None:
        """Test getting status snapshot."""
        tracker = StatusTracker(num_slots=3)

        slot1 = tracker.acquire_slot()
        assert slot1 is not None
        tracker.update_slot(slot1, "Working")

        status = tracker.get_status()

        assert len(status) == 3
        assert status[slot1] == "Working"
        # Verify it's a copy
        status[slot1] = "Modified"
        assert tracker.slots[slot1] == "Working"

    def test_get_active_count(self) -> None:
        """Test getting active slot count."""
        tracker = StatusTracker(num_slots=3)

        assert tracker.get_active_count() == 0

        slot1 = tracker.acquire_slot()
        assert slot1 is not None
        assert tracker.get_active_count() == 1

        _ = tracker.acquire_slot()
        assert tracker.get_active_count() == 2

        tracker.release_slot(slot1)
        assert tracker.get_active_count() == 1

    @pytest.mark.slow
    def test_wait_for_available(self) -> None:
        """Test waiting for slot availability."""
        tracker = StatusTracker(num_slots=1)

        # Acquire the only slot
        slot_id = tracker.acquire_slot()
        assert slot_id is not None

        # Start thread that releases slot after delay
        def release_after_delay() -> None:
            time.sleep(0.1)
            tracker.release_slot(slot_id)

        thread = threading.Thread(target=release_after_delay, daemon=True)
        thread.start()

        # Wait for availability - should succeed
        result = tracker.wait_for_available(timeout=1.0)
        assert result is True

        thread.join()

    def test_wait_for_available_timeout(self) -> None:
        """Test wait_for_available timeout."""
        tracker = StatusTracker(num_slots=1)

        # Acquire the only slot and don't release
        tracker.acquire_slot()

        # Wait should timeout
        result = tracker.wait_for_available(timeout=0.1)
        assert result is False

    @pytest.mark.slow
    def test_wait_all_complete(self) -> None:
        """Test waiting for all slots to complete."""
        tracker = StatusTracker(num_slots=2)

        slot1 = tracker.acquire_slot()
        slot2 = tracker.acquire_slot()
        assert slot1 is not None
        assert slot2 is not None

        # Start thread that releases slots after delay
        def release_after_delay() -> None:
            time.sleep(0.05)
            tracker.release_slot(slot1)
            time.sleep(0.05)
            tracker.release_slot(slot2)

        thread = threading.Thread(target=release_after_delay, daemon=True)
        thread.start()

        # Wait for all to complete
        result = tracker.wait_all_complete(timeout=1.0)
        assert result is True

        thread.join()

    def test_wait_all_complete_timeout(self) -> None:
        """Test wait_all_complete timeout."""
        tracker = StatusTracker(num_slots=1)

        # Acquire slot and don't release
        tracker.acquire_slot()

        # Wait should timeout
        result = tracker.wait_all_complete(timeout=0.1)
        assert result is False

    def test_clear(self) -> None:
        """Test clearing all slots."""
        tracker = StatusTracker(num_slots=3)

        slot1 = tracker.acquire_slot()
        slot2 = tracker.acquire_slot()
        assert slot1 is not None
        assert slot2 is not None
        tracker.update_slot(slot1, "Working")
        tracker.update_slot(slot2, "Working")

        tracker.clear()

        assert all(slot is None for slot in tracker.slots)
        assert tracker.get_active_count() == 0

    @pytest.mark.slow
    def test_concurrent_acquire_release(self) -> None:
        """Test concurrent slot acquisition and release."""
        tracker = StatusTracker(num_slots=5)
        acquired_slots = []
        lock = threading.Lock()

        def worker() -> None:
            slot_id = tracker.acquire_slot(timeout=2.0)
            if slot_id is not None:
                with lock:
                    acquired_slots.append(slot_id)
                time.sleep(0.01)  # Simulate work
                tracker.release_slot(slot_id)

        # Start 10 threads competing for 5 slots
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads should have acquired a slot at some point
        assert len(acquired_slots) == 10
        # All slots should be released
        assert tracker.get_active_count() == 0

    @pytest.mark.slow
    def test_notify_all_on_release(self) -> None:
        """Test that release_slot wakes all waiting threads."""
        tracker = StatusTracker(num_slots=1)

        # Acquire the only slot
        slot_id = tracker.acquire_slot()
        assert slot_id is not None

        results = []

        def waiter() -> None:
            # Both wait_for_available and acquire_slot should wake
            if tracker.wait_for_available(timeout=1.0):
                results.append("available")

        # Start multiple waiting threads
        threads = [threading.Thread(target=waiter, daemon=True) for _ in range(3)]
        for t in threads:
            t.start()

        # Give threads time to start waiting
        time.sleep(0.1)

        # Release should wake all waiters
        tracker.release_slot(slot_id)

        for t in threads:
            t.join()

        # All waiters should have been notified
        assert len(results) == 3

    @pytest.mark.slow
    def test_notify_all_on_clear(self) -> None:
        """Test that clear wakes waiting threads."""
        tracker = StatusTracker(num_slots=1)

        # Acquire the only slot
        tracker.acquire_slot()

        result = []

        def waiter() -> None:
            if tracker.wait_for_available(timeout=1.0):
                result.append(True)

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()

        # Give thread time to start waiting
        time.sleep(0.1)

        # Clear should wake waiter
        tracker.clear()

        thread.join()

        assert result == [True]


class TestSlotContextManager:
    """Tests for the slot() context manager (#1435)."""

    def test_slot_acquires_and_releases(self) -> None:
        """slot() acquires on entry and releases on exit."""
        tracker = StatusTracker(num_slots=2)
        with tracker.slot() as slot_id:
            assert slot_id is not None
            assert tracker.get_active_count() == 1
        assert tracker.get_active_count() == 0

    def test_slot_releases_on_exception(self) -> None:
        """slot() releases even when the body raises (no leak)."""
        tracker = StatusTracker(num_slots=1)
        with pytest.raises(ValueError):
            with tracker.slot() as slot_id:
                assert slot_id is not None
                raise ValueError("boom")
        assert tracker.get_active_count() == 0  # no leak

    def test_slot_sets_initial_message(self) -> None:
        """A non-empty initial_msg is applied right after acquire."""
        tracker = StatusTracker(num_slots=1)
        with tracker.slot("Starting work") as slot_id:
            assert slot_id is not None
            assert tracker.slots[slot_id] == "Starting work"

    def test_slot_no_initial_message_keeps_acquired(self) -> None:
        """Without initial_msg the slot keeps its 'acquired' marker."""
        tracker = StatusTracker(num_slots=1)
        with tracker.slot() as slot_id:
            assert slot_id is not None
            assert tracker.slots[slot_id] == "acquired"

    def test_slot_yields_none_on_timeout(self) -> None:
        """On acquisition timeout slot() yields None and release is a no-op."""
        tracker = StatusTracker(num_slots=1)
        tracker.acquire_slot()  # exhaust the only slot
        with tracker.slot(timeout=0.1) as slot_id:
            assert slot_id is None  # caller must guard
        # release_slot(None) must be a safe no-op (no TypeError)
        assert tracker.get_active_count() == 1

    def test_slot_post_sleep_runs_before_release(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """post_sleep runs before release by the requested duration."""
        tracker = StatusTracker(num_slots=1)

        sleep_calls: list[float] = []

        def fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)
            assert tracker.get_active_count() == 1

        monkeypatch.setattr("hephaestus.automation.status_tracker.time.sleep", fake_sleep)

        with tracker.slot(post_sleep=0.2):
            pass

        assert sleep_calls == [0.2]
        assert tracker.get_active_count() == 0

    def test_slot_initial_message_skipped_on_timeout(self) -> None:
        """No update_slot is attempted when acquisition times out."""
        tracker = StatusTracker(num_slots=1)
        held_slot = tracker.acquire_slot()
        assert held_slot is not None
        with tracker.slot("should not set", timeout=0.1) as slot_id:
            assert slot_id is None  # no update_slot attempted on None
        assert tracker.slots[held_slot] == "acquired"
