"""RED contract tests for lossless, bounded ``StageQueue`` handoffs.

The lease API specified here is intentionally small and public:

* ``StageQueue.claim()`` returns a lease for the FIFO-ready item, or ``None``
  when the queue has no ready work.
* A claimed item remains part of the source queue's ``occupancy`` while its
  lease is outstanding, so it cannot create an admission slot by itself.
* ``lease.restore()`` puts the item back at the front of its source queue.
* ``lease.handoff(destination)`` atomically reserves the destination and then
  releases the source reservation.  It returns ``False`` without losing the
  source item when the destination is full; callers can retry that same lease.

The production queue currently exposes only push/offer/pop.  These tests are
deliberately RED until the bounded lease protocol is implemented.
"""

from hephaestus.automation.pipeline import ItemKind, StageQueue, WorkItem


def _item(name: str) -> WorkItem:
    """Create a distinct valid pipeline item for queue identity assertions."""
    return WorkItem(repo=name, kind=ItemKind.REPO)


class TestStageQueueLeases:
    """Lossless handoff behavior for a bounded source and destination queue."""

    def test_claim_reserves_capacity_and_blocks_new_source_offer(self) -> None:
        """Claiming ready work keeps a full source occupied until it is released."""
        source = StageQueue(capacity=2)
        claimed_item = _item("claimed")
        still_ready = _item("still-ready")
        replacement = _item("replacement")
        source.push(claimed_item)
        source.push(still_ready)

        lease = source.claim()

        assert lease is not None
        assert lease.item is claimed_item
        assert source.snapshot() == [still_ready]
        assert source.occupancy == source.capacity == 2
        assert source.offer(replacement) is False

    def test_restore_preserves_claimed_item_and_uses_remaining_capacity(self) -> None:
        """A lease reserves its item but does not serialize unused capacity."""
        source = StageQueue(capacity=2)
        claimed_item = _item("claimed")
        source.push(claimed_item)

        lease = source.claim()

        assert lease is not None
        later = _item("may-enter-before-restore")
        assert source.offer(later) is True
        lease.restore()

        assert source.occupancy == 2
        assert source.snapshot() == [claimed_item, later]

    def test_concurrent_claims_restore_in_original_fifo_order(self) -> None:
        """Multiple active claims retain capacity and restore by original order."""
        source = StageQueue(capacity=3)
        first = _item("first")
        second = _item("second")
        third = _item("third")
        source.push(first)
        source.push(second)
        source.push(third)

        first_lease = source.claim()
        second_lease = source.claim()

        assert first_lease is not None
        assert second_lease is not None
        assert first_lease.item is first
        assert second_lease.item is second
        assert source.snapshot() == [third]

        second_lease.restore()
        first_lease.restore()

        assert source.snapshot() == [first, second, third]

    def test_selected_claim_restores_the_original_queue_position(self) -> None:
        """Topo scheduling may lease a non-head item without reordering a retry."""
        source = StageQueue(capacity=3)
        first = _item("first")
        selected = _item("selected")
        third = _item("third")
        for item in (first, selected, third):
            source.push(item)

        lease = source.claim_at(1)

        assert lease is not None
        assert lease.item is selected
        assert source.snapshot() == [first, third]
        first_lease = source.claim()
        assert first_lease is not None
        assert first_lease.item is first

        first_lease.restore()
        lease.restore()

        assert source.snapshot() == [first, selected, third]

    def test_selected_claim_release_does_not_remove_the_queue_head(self) -> None:
        """A timer can take selected work without accidentally popping a peer."""
        source = StageQueue(capacity=2)
        first = _item("first")
        selected = _item("selected")
        source.push(first)
        source.push(selected)

        lease = source.claim_at(1)

        assert lease is not None
        lease.release()

        assert source.snapshot() == [first]
        assert source.occupancy == 1

    def test_successful_handoff_releases_source_and_enqueues_item_once(self) -> None:
        """A successful handoff frees only the source reservation it transfers."""
        source = StageQueue(capacity=1)
        destination = StageQueue(capacity=1)
        item = _item("handoff")
        replacement = _item("replacement")
        source.push(item)
        lease = source.claim()

        assert lease is not None
        assert lease.handoff(destination) is True

        assert source.occupancy == 0
        assert source.offer(replacement) is True
        assert destination.occupancy == 1
        assert destination.snapshot() == [item]
        assert destination.pop() is item
        assert destination.occupancy == 0

    def test_full_destination_retains_source_lease_for_later_handoff(self) -> None:
        """A failed handoff neither drops nor spills work, and can be retried."""
        source = StageQueue(capacity=1)
        destination = StageQueue(capacity=1)
        item = _item("source-item")
        destination_item = _item("destination-item")
        source.push(item)
        destination.push(destination_item)
        lease = source.claim()

        assert lease is not None
        assert lease.handoff(destination) is False
        assert source.occupancy == source.capacity == 1
        assert source.offer(_item("must-not-enter-while-leased")) is False
        assert destination.snapshot() == [destination_item]

        assert destination.pop() is destination_item
        assert lease.handoff(destination) is True
        assert source.occupancy == 0
        assert destination.snapshot() == [item]
        assert destination.pop() is item
        assert destination.occupancy == 0

    def test_empty_claim_is_explicit(self) -> None:
        """An empty source queue reports no lease instead of raising or inventing work."""
        source = StageQueue(capacity=1)

        assert source.claim() is None
