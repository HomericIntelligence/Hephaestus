"""Cohesive observability helpers extracted from the coordinator runtime."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hephaestus.automation.event_log_io import (
    EventLogCandidate,
    EventLogHandle,
    open_event_log_handle,
)
from hephaestus.observability.alerts import evaluate_alerts

from .coordinator_types import _json_safe
from .routing import AUXILIARY_PIPELINE_ORDER, MAIN_PIPELINE_ORDER


def _append_event_log(
    path: Path,
    record: dict[str, Any],
    *,
    handle: EventLogHandle | None = None,
) -> None:
    """Append one record through a bound private directory."""
    line = json.dumps(record, sort_keys=True) + "\n"
    if handle is not None:
        handle.append_line(line)
        return
    candidate = EventLogCandidate(path=path, private_root=path.parent)
    with open_event_log_handle(candidate) as transient_handle:
        transient_handle.append_line(line)


def record_event(
    coordinator: Any,
    event: str,
    *fields: Any,
    now_fn: Callable[[], float],
    logger: logging.Logger,
) -> None:
    """Append an event to memory and, when configured, to JSONL on disk."""
    coordinator.event_log.append((event, *fields))
    if coordinator._event_log_disabled:
        return
    path = coordinator.config.event_log_path
    if path is None:
        return
    record = {
        "ts": now_fn(),
        "event": event,
        "fields": [_json_safe(field) for field in fields],
    }
    try:
        _append_event_log(path, record, handle=coordinator.config.event_log_handle)
    except OSError as exc:
        logger.warning("failed to write pipeline event log %s: %s", path, exc)
        coordinator._event_log_disabled = True


def observability_snapshot(coordinator: Any, *, logger: logging.Logger) -> dict[str, Any]:
    """Read the coordinator lifecycle values exposed to observability."""
    circuit_breakers: dict[str, dict[str, Any]] = {}
    snapshot_errors: list[str] = []
    provider = coordinator.config.circuit_breaker_snapshot_provider
    if provider is not None:
        try:
            circuit_breakers = provider()
        except Exception:
            # Observability must not terminate a production automation loop if
            # an optional diagnostic provider is broken.
            logger.exception("circuit-breaker snapshot provider failed")
            snapshot_errors.append("circuit_breaker_snapshot_provider_failed")

    queue_depths = {name.value: len(queue) for name, queue in coordinator.queues.items()}
    queue_capacities = {name.value: queue.capacity for name, queue in coordinator.queues.items()}
    # Occupancy counts ready and leased items, so a queue that reserved its
    # capacity for in-flight work is saturated even when ``queue_depths`` is
    # small. The latched completion fault joins the same list.
    saturated_queues = sorted(
        name.value
        for name, queue in coordinator.queues.items()
        if queue.occupancy >= queue.capacity
    )
    if coordinator._completion_saturation.is_set():
        saturated_queues = sorted({*saturated_queues, "completion"})
    lane_queue_depths = {
        "main": sum(queue_depths[stage.value] for stage in MAIN_PIPELINE_ORDER),
        "auxiliary": sum(queue_depths[stage.value] for stage in AUXILIARY_PIPELINE_ORDER),
    }
    inflight_by_lane = {
        "main": len(coordinator.in_flight),
        "auxiliary": len(coordinator.auxiliary_in_flight),
    }
    snapshot = {
        "queue_depths": queue_depths,
        "queue_capacities": queue_capacities,
        "saturated_queues": saturated_queues,
        "lane_queue_depths": lane_queue_depths,
        "inflight_per_repo": dict(coordinator.inflight_per_repo),
        "inflight_by_lane": inflight_by_lane,
        "inflight_jobs": sum(inflight_by_lane.values()),
        "circuit_breakers": circuit_breakers,
        "loops_run": coordinator._loops_run,
        "stalled_ticks": coordinator._stalled_ticks,
    }
    if snapshot_errors:
        snapshot["snapshot_errors"] = snapshot_errors
    return snapshot


def health_snapshot(
    coordinator: Any,
    *,
    logger: logging.Logger,
    stalled_ticks_threshold: int,
) -> dict[str, Any]:
    """Return the local server's JSON readiness response without external I/O."""
    snapshot = observability_snapshot(coordinator, logger=logger)
    active_alerts = evaluate_alerts(
        snapshot,
        queue_depth_threshold=coordinator.config.alert_queue_depth_threshold,
        stalled_ticks_threshold=stalled_ticks_threshold,
    )
    if coordinator.shutdown.is_set():
        status = "stopping"
    elif snapshot.get("snapshot_errors"):
        status = "error"
    elif active_alerts:
        status = "degraded"
    else:
        status = "ok"
    snapshot["status"] = status
    return snapshot
