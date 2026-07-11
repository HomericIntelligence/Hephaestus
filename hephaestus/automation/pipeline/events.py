"""Bounded stage-originated events for the durable pipeline JSONL log."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

_REPO_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
EventField: TypeAlias = str | int | bool


class ZeroThreadNogoAction(str, Enum):
    """Action taken after a zero-thread NOGO anomaly."""

    RETRY_FRESH_REVIEW = "retry_fresh_review"
    FAIL_BACK_AGENT_ERROR = "fail_back_agent_error"


@dataclass(frozen=True)
class PrReviewZeroThreadNogoEvent:
    """Closed-schema audit record for an artifactless NOGO verdict."""

    repo: str
    issue: int
    pr: int
    completed_rounds: int
    retry_attempt: int
    retry_cap: int
    action: ZeroThreadNogoAction
    artifact_written: bool


StageEvent: TypeAlias = PrReviewZeroThreadNogoEvent


def encode_stage_event(event: StageEvent) -> tuple[str, dict[str, EventField]]:
    """Validate and encode a stage event using only fixed JSON-safe fields."""
    if type(event) is not PrReviewZeroThreadNogoEvent:
        raise TypeError(f"unsupported stage event: {type(event).__name__}")
    if not _REPO_NAME_RE.fullmatch(event.repo):
        raise ValueError("stage event repo must be a bounded repository name")
    for name in ("issue", "pr", "retry_attempt", "retry_cap"):
        value = getattr(event, name)
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if type(event.completed_rounds) is not int or event.completed_rounds < 0:
        raise ValueError("completed_rounds must be a non-negative integer")
    if type(event.artifact_written) is not bool:
        raise ValueError("artifact_written must be boolean")
    if not isinstance(event.action, ZeroThreadNogoAction):
        raise ValueError("action must be a ZeroThreadNogoAction")
    if (
        event.action is ZeroThreadNogoAction.RETRY_FRESH_REVIEW
        and event.retry_attempt > event.retry_cap
    ):
        raise ValueError("fresh-review action exceeds retry cap")
    if (
        event.action is ZeroThreadNogoAction.FAIL_BACK_AGENT_ERROR
        and event.retry_attempt <= event.retry_cap
    ):
        raise ValueError("fail-back action has not exceeded retry cap")
    return (
        "pr_review_zero_thread_nogo",
        {
            "repo": event.repo,
            "issue": event.issue,
            "pr": event.pr,
            "completed_rounds": event.completed_rounds,
            "retry_attempt": event.retry_attempt,
            "retry_cap": event.retry_cap,
            "action": event.action.value,
            "artifact_written": event.artifact_written,
            "posted_threads": 0,
            "unresolved_threads": 0,
            "round_consumed": False,
        },
    )
