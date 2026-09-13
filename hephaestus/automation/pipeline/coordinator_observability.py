"""Cohesive observability helpers extracted from the coordinator runtime."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from hephaestus.automation.review_finding_history import (
    compact_terminal_review_finding_collection,
    empty_review_finding_compacted_outcomes,
    normalize_review_finding_collection,
    normalize_review_finding_compacted_outcomes,
)
from hephaestus.observability.alerts import evaluate_alerts

from .coordinator_types import _json_safe
from .routing import AUXILIARY_PIPELINE_ORDER, MAIN_PIPELINE_ORDER
from .work_item import WorkItem


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

    queue_depths = {name.value: len(queue) for name, queue in coordinator.queues.items()}
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


def record_review_finding_events(
    coordinator: Any, item: WorkItem, *, logger: logging.Logger
) -> None:
    """Stream bounded finding identities without review text."""
    raw_records = item.payload.get("review_finding_records", [])
    raw_compacted = item.payload.get(
        "review_finding_compacted_outcomes",
        empty_review_finding_compacted_outcomes(),
    )
    try:
        validated_compacted = normalize_review_finding_compacted_outcomes(raw_compacted)
        has_compacted_history = bool(validated_compacted["identities"])
        legacy_records, legacy_compacted = normalize_review_finding_collection(
            raw_records,
            compacted_outcomes=validated_compacted if has_compacted_history else None,
        )
        if not has_compacted_history and any(
            record["status"] == "pending" for record in legacy_records
        ):
            records, compacted = legacy_records, legacy_compacted
        else:
            records, compacted = compact_terminal_review_finding_collection(
                legacy_records,
                validated_compacted,
            )
    except ValueError:
        logger.warning(
            "terminal:%s: invalid review finding records; "
            "the coordinator did not write finding events",
            coordinator._item_key(item),
        )
        return
    for record in records:
        coordinator._record_event(
            "review_finding_outcome",
            {
                "repo": item.repo,
                "issue": item.issue,
                "pr": item.pr,
                "finding_id": record["finding_id"],
                "source_head": record["source_head"],
                "severity": record["severity"],
                "status": record["status"],
                "surface": record["surface"],
                "original_anchor": record["original_anchor"],
                "final_anchor": record["final_anchor"],
                "reason": record["reason"],
            },
        )
    for identity in compacted["identities"]:
        coordinator._record_event(
            "review_finding_compacted_outcome",
            {
                "repo": item.repo,
                "issue": item.issue,
                "pr": item.pr,
                "finding_id": identity[0],
                "source_head": identity[1],
                "outcome": {
                    "c": "corrected",
                    "n": "not_publishable",
                    "p": "published",
                }[identity[2]],
                "blocking": identity[3] == "b",
            },
        )
