"""Cohesive observability helpers extracted from the coordinator runtime."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from hephaestus.observability.alerts import evaluate_alerts

from .coordinator_types import _json_safe


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
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
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

    snapshot = {
        "queue_depths": {name.value: len(queue) for name, queue in coordinator.queues.items()},
        "inflight_per_repo": dict(coordinator.inflight_per_repo),
        "inflight_jobs": len(coordinator.in_flight),
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
