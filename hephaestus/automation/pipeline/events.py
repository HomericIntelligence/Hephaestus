"""Bounded stage-originated events for the durable pipeline JSONL log."""

from __future__ import annotations

type EventField = str | int | bool
type StageEvent = object


def encode_stage_event(event: StageEvent) -> tuple[str, dict[str, EventField]]:
    """Reject stage events until a non-authoritative event contract exists."""
    raise TypeError(f"unsupported stage event: {type(event).__name__}")
