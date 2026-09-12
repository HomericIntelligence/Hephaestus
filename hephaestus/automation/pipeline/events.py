"""Bounded stage-originated events for the durable pipeline JSONL log."""

from __future__ import annotations

import math
from dataclasses import dataclass

type EventField = str | int | float | bool


@dataclass(frozen=True, slots=True)
class RepositoryBusyEvent:
    """Describe one terminal repository checkout contention period."""

    repository: str
    operation: str
    elapsed_s: float
    cumulative_lock_wait_s: float

    def summary(self) -> str:
        """Return the terminal summary from the validated event fields."""
        _repository_busy_fields(self)
        return (
            f"repository_busy: repository={self.repository} "
            f"operation={self.operation} elapsed={float(self.elapsed_s):.3f}s "
            f"lock_wait={float(self.cumulative_lock_wait_s):.3f}s cause=lock_timeout"
        )


type StageEvent = RepositoryBusyEvent


def _bounded_text(value: object, *, name: str, limit: int) -> str:
    """Return one bounded event string without control characters."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"invalid {name}")
    return value


def _duration(value: object, *, name: str) -> float:
    """Return one finite nonnegative event duration."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"invalid {name}")
    return float(value)


def _repository_busy_fields(event: RepositoryBusyEvent) -> dict[str, EventField]:
    """Return the closed durable schema for repository contention."""
    return {
        "repository": _bounded_text(event.repository, name="repository", limit=256),
        "operation": _bounded_text(event.operation, name="operation", limit=128),
        "elapsed_s": _duration(event.elapsed_s, name="elapsed_s"),
        "cumulative_lock_wait_s": _duration(
            event.cumulative_lock_wait_s,
            name="cumulative_lock_wait_s",
        ),
    }


def encode_stage_event(event: StageEvent) -> tuple[str, dict[str, EventField]]:
    """Encode one supported stage event with a closed durable schema."""
    if isinstance(event, RepositoryBusyEvent):
        return "repository_busy", _repository_busy_fields(event)
    raise TypeError(f"unsupported stage event: {type(event).__name__}")
