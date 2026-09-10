"""Bounded cross-lane handoff recovery."""

from .coordinator_contract import _CoordinatorHost
from .coordinator_types import _PendingHandoff


class PendingHandoffCoordinator(_CoordinatorHost):
    """Resolve complementary queue handoffs without early lease release."""

    def _complete_pending_handoff_pair(
        self,
        first_id: int,
        first: _PendingHandoff,
        second_id: int,
        second: _PendingHandoff,
    ) -> bool:
        first_lease = self._leases[first_id]
        second_lease = self._leases[second_id]
        source1, source2 = self.queues[first.item.stage], self.queues[second.item.stage]
        target1, target2 = self.queues[first.target], self.queues[second.target]
        if target1 is source2 and target2 is source1:
            accepted = first_lease.exchange(target1, second_lease, target2)
        elif target1.can_offer() and (target2 is source1 or target2.can_offer()):
            accepted = first_lease.handoff(target1) and second_lease.handoff(target2)
        elif target2.can_offer() and (target1 is source2 or target1.can_offer()):
            accepted = second_lease.handoff(target2) and first_lease.handoff(target1)
        else:
            return False
        if not accepted:  # pragma: no cover
            raise RuntimeError("handoff exchange lost capacity")
        for item_id, pending in ((first_id, first), (second_id, second)):
            self._leases.pop(item_id, None)
            self._pending_handoffs.pop(item_id, None)
            self._activate_handoff(
                pending.item, pending.target, enter=pending.enter, result=pending.result
            )
            self._record_event(
                "handoff_retry", pending.item.stage.value, self._item_key(pending.item)
            )
        self._progress = True
        return True

    def _drain_complementary_handoff_pairs(self) -> None:
        entries = list(self._pending_handoffs.items())
        for index, (first_id, first) in enumerate(entries):
            if first_id not in self._pending_handoffs:
                continue
            source_aux = self._is_auxiliary_stage(first.item.stage)
            target_aux = self._is_auxiliary_stage(first.target)
            if source_aux == target_aux:
                continue
            for second_id, second in entries[index + 1 :]:
                if second_id not in self._pending_handoffs:
                    continue
                complementary = (
                    self._is_auxiliary_stage(second.item.stage) == target_aux
                    and self._is_auxiliary_stage(second.target) == source_aux
                )
                if complementary and self._complete_pending_handoff_pair(
                    first_id, first, second_id, second
                ):
                    break
