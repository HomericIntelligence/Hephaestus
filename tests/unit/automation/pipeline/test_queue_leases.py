"""Contract tests for lossless, bounded ``StageQueue`` handoffs."""

from hephaestus.automation.pipeline import ItemKind, StageQueue, WorkItem


def _item(name: str) -> WorkItem:
    """Create a distinct valid pipeline item for queue identity assertions."""
    return WorkItem(repo=name, kind=ItemKind.REPO)


class TestStageQueueLeases:
    """Lossless handoff behavior for bounded source and destination queues."""

    def test_claim_reserves_capacity_and_blocks_new_source_offer(self) -> None:
        """Claiming ready work keeps a full source occupied until release."""
        source = StageQueue(capacity=2)
        claimed_item = _item("claimed")
        still_ready = _item("still-ready")
        source.push(claimed_item)
        source.push(still_ready)

        lease = source.claim()

        assert lease is not None
        assert lease.item is claimed_item
        assert source.snapshot() == [still_ready]
        assert source.occupancy == source.capacity == 2
        assert source.offer(_item("replacement")) is False

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
        """Multiple active claims retain capacity and original FIFO tickets."""
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

    def test_selected_claim_restores_original_queue_position(self) -> None:
        """Topo scheduling may lease a non-head item without reordering retry."""
        source = StageQueue(capacity=3)
        first = _item("first")
        selected = _item("selected")
        third = _item("third")
        for item in (first, selected, third):
            source.push(item)

        selected_lease = source.claim_at(1)

        assert selected_lease is not None
        assert selected_lease.item is selected
        assert source.snapshot() == [first, third]
        first_lease = source.claim()
        assert first_lease is not None
        assert first_lease.item is first

        first_lease.restore()
        selected_lease.restore()

        assert source.snapshot() == [first, selected, third]

    def test_selected_claim_release_does_not_remove_queue_head(self) -> None:
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

    def test_successful_handoff_releases_source_and_enqueues_once(self) -> None:
        """A successful handoff transfers the source reservation exactly once."""
        source = StageQueue(capacity=1)
        destination = StageQueue(capacity=1)
        item = _item("handoff")
        source.push(item)
        lease = source.claim()

        assert lease is not None
        assert lease.handoff(destination) is True

        assert source.occupancy == 0
        assert source.offer(_item("replacement")) is True
        assert destination.occupancy == 1
        assert destination.snapshot() == [item]
        popped = destination.pop()
        assert popped is item
        assert destination.occupancy == 0

    def test_full_destination_retains_source_lease_for_retry(self) -> None:
        """A failed handoff neither drops nor spills work and remains retryable."""
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

        popped_destination_item = destination.pop()
        assert popped_destination_item is destination_item
        assert lease.handoff(destination) is True
        assert source.occupancy == 0
        assert destination.snapshot() == [item]
        handed_off_item = destination.pop()
        assert handed_off_item is item
        assert destination.occupancy == 0

    def test_empty_claim_is_explicit(self) -> None:
        """An empty source reports no lease instead of inventing work."""
        source = StageQueue(capacity=1)

        assert source.claim() is None
