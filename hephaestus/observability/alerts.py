"""Transition-based alerts derived only from live coordinator lifecycle data."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


def _validated_percentage(value: int, *, name: str) -> int:
    """Return one validated 0-100 percentage threshold."""
    if isinstance(value, bool) or not 0 <= value <= 100:
        raise ValueError(f"{name} must be a percentage between 0 and 100")
    return value


@dataclass(frozen=True)
class AlertEvent:
    """A durable transition for one currently observable alert condition."""

    name: str
    severity: str
    status: str
    message: str


def evaluate_alerts(
    snapshot: Mapping[str, Any],
    *,
    queue_depth_threshold: int = 100,
    stalled_ticks_threshold: int = 3,
) -> list[AlertEvent]:
    """Evaluate conditions that the coordinator's runtime snapshot really contains.

    The function deliberately contains no speculative rules: every rule reads
    ``queue_depths``, ``queue_capacities``, ``saturated_queues``,
    ``circuit_breakers``, or ``stalled_ticks`` emitted by the coordinator.
    Returned events describe active conditions; :class:`AlertTracker` turns
    those into fire/resolve transitions.

    ``queue_depth_threshold`` is a percentage of a stage queue's bounded
    capacity. ``0`` fires on any non-empty ready backlog, and ``100`` fires
    only when the ready backlog fills the queue.
    """
    _validated_percentage(queue_depth_threshold, name="queue depth threshold")
    if stalled_ticks_threshold < 0:
        raise ValueError("stalled ticks threshold must be non-negative")
    events: list[AlertEvent] = []
    raw_breakers = snapshot.get("circuit_breakers", {})
    if isinstance(raw_breakers, Mapping):
        opened = sorted(
            str(name)
            for name, value in raw_breakers.items()
            if isinstance(value, Mapping) and value.get("state") == "open"
        )
        if opened:
            events.append(
                AlertEvent(
                    name="circuit_breaker_open",
                    severity="critical",
                    status="fired",
                    message=f"circuit breakers open: {', '.join(opened)}",
                )
            )

    raw_depths = snapshot.get("queue_depths", {})
    raw_capacities = snapshot.get("queue_capacities", {})
    if isinstance(raw_depths, Mapping) and isinstance(raw_capacities, Mapping):
        exceeded: list[str] = []
        for stage, depth in raw_depths.items():
            capacity = raw_capacities.get(stage)
            if (
                isinstance(depth, (int, float))
                and not isinstance(depth, bool)
                and isinstance(capacity, (int, float))
                and not isinstance(capacity, bool)
                and capacity > 0
                and depth > 0
                and depth * 100 >= capacity * queue_depth_threshold
            ):
                exceeded.append(str(stage))
        if exceeded:
            events.append(
                AlertEvent(
                    name="queue_depth_exceeds",
                    severity="warning",
                    status="fired",
                    message=f"queue depth reaches {queue_depth_threshold}% of capacity: "
                    f"{', '.join(sorted(exceeded))}",
                )
            )

    raw_saturated = snapshot.get("saturated_queues")
    if isinstance(raw_saturated, Sequence) and not isinstance(raw_saturated, (str, bytes)):
        saturated = sorted({str(name) for name in raw_saturated})
        if saturated:
            events.append(
                AlertEvent(
                    name="queue_saturated",
                    severity="critical",
                    status="fired",
                    message=f"queues at capacity: {', '.join(saturated)}",
                )
            )

    raw_stalled = snapshot.get("stalled_ticks")
    if (
        isinstance(raw_stalled, (int, float))
        and not isinstance(raw_stalled, bool)
        and raw_stalled >= stalled_ticks_threshold
    ):
        events.append(
            AlertEvent(
                name="pipeline_stalled",
                severity="warning",
                status="fired",
                message=f"pipeline made no progress for {int(raw_stalled)} ticks",
            )
        )
    return events


class AlertTracker:
    """Emit a durable event only when an alert condition changes state."""

    def __init__(
        self, *, queue_depth_threshold: int = 100, stalled_ticks_threshold: int = 3
    ) -> None:
        """Create a tracker with the coordinator's alert thresholds."""
        _validated_percentage(queue_depth_threshold, name="queue depth threshold")
        if stalled_ticks_threshold < 0:
            raise ValueError("stalled ticks threshold must be non-negative")
        self._queue_depth_threshold = queue_depth_threshold
        self._stalled_ticks_threshold = stalled_ticks_threshold
        self._active: dict[str, AlertEvent] = {}
        self._lock = threading.Lock()

    def observe(self, snapshot: Mapping[str, Any]) -> list[AlertEvent]:
        """Return newly fired and newly resolved conditions, never duplicates."""
        current = {
            event.name: event
            for event in evaluate_alerts(
                snapshot,
                queue_depth_threshold=self._queue_depth_threshold,
                stalled_ticks_threshold=self._stalled_ticks_threshold,
            )
        }
        with self._lock:
            transitions = [event for name, event in current.items() if name not in self._active]
            transitions.extend(
                AlertEvent(
                    name=previous.name,
                    severity=previous.severity,
                    status="resolved",
                    message=f"resolved: {previous.message}",
                )
                for name, previous in self._active.items()
                if name not in current
            )
            self._active = current
        return sorted(transitions, key=lambda event: (event.name, event.status))
