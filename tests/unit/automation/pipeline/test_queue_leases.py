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

    def test_restore_returns_claimed_item_without_opening_admission_slot(self) -> None:
        """Restoring a lease keeps the claimed item and FIFO-ready work lossless."""
        source = StageQueue(capacity=2)
        claimed_item = _item("claimed")
        source.push(claimed_item)

        lease = source.claim()

        assert lease is not None
        assert source.offer(_item("must-not-enter-before-restore")) is False
        lease.restore()

        assert source.occupancy == 1
        assert source.snapshot() == [claimed_item]
        assert source.offer(_item("may-enter-after-restore")) is True

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
