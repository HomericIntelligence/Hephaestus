"""Bounded cross-lane handoff recovery."""

from typing import Any, Protocol, cast

from .coordinator_types import _PendingHandoff
from .queues import StageQueueLease


class _HandoffHost(Protocol):
    def _activate_handoff(self, *args: Any, **kwargs: Any) -> None: ...
    def _record_event(self, *args: Any) -> None: ...
    def _item_key(self, item: Any) -> str: ...
    def _is_auxiliary_stage(self, stage: Any) -> bool: ...


class PendingHandoffCoordinator:
    """Resolve complementary queue handoffs without early lease release."""

    _leases: dict[int, StageQueueLease]
    _pending_handoffs: dict[int, _PendingHandoff]
    queues: Any
    _progress: bool

    def _complete_pending_handoff_pair(
        self,
        first_id: int,
        first: _PendingHandoff,
        second_id: int,
        second: _PendingHandoff,
    ) -> bool:
        host = cast(_HandoffHost, self)
        leases = self._leases.get(first_id), self._leases.get(second_id)
        if None in leases:
            return False
        first_lease, second_lease = leases
        assert first_lease is not None and second_lease is not None  # noqa: S101
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
            host._activate_handoff(
                pending.item, pending.target, enter=pending.enter, result=pending.result
            )
            host._record_event(
                "handoff_retry", pending.item.stage.value, host._item_key(pending.item)
            )
        self._progress = True
        return True

    def _drain_complementary_handoff_pairs(self) -> None:
        host = cast(_HandoffHost, self)
        entries = list(self._pending_handoffs.items())
        for index, (first_id, first) in enumerate(entries):
            if first_id not in self._pending_handoffs:
                continue
            source_aux = host._is_auxiliary_stage(first.item.stage)
            target_aux = host._is_auxiliary_stage(first.target)
            if source_aux == target_aux:
                continue
            for second_id, second in entries[index + 1 :]:
                if second_id not in self._pending_handoffs:
                    continue
                complementary = (
                    host._is_auxiliary_stage(second.item.stage) == target_aux
                    and host._is_auxiliary_stage(second.target) == source_aux
                )
                if complementary and self._complete_pending_handoff_pair(
                    first_id, first, second_id, second
                ):
                    break
