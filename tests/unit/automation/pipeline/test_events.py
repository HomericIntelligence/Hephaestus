"""Tests for closed stage-event schemas."""

from dataclasses import dataclass, replace
from typing import Any, cast

import pytest

from hephaestus.automation.pipeline.events import (
    PrReviewZeroThreadNogoEvent,
    ZeroThreadNogoAction,
    encode_stage_event,
)


def test_zero_thread_nogo_event_has_closed_json_safe_shape() -> None:
    """Encode only the fixed fields needed to audit the anomaly."""
    name, fields = encode_stage_event(
        PrReviewZeroThreadNogoEvent(
            repo="repo-a",
            issue=1985,
            pr=1984,
            completed_rounds=0,
            retry_attempt=1,
            retry_cap=2,
            action=ZeroThreadNogoAction.RETRY_FRESH_REVIEW,
            artifact_written=True,
        )
    )

    assert name == "pr_review_zero_thread_nogo"
    assert fields["round_consumed"] is False
    assert fields["posted_threads"] == fields["unresolved_threads"] == 0
    assert set(fields) == {
        "repo",
        "issue",
        "pr",
        "completed_rounds",
        "retry_attempt",
        "retry_cap",
        "action",
        "artifact_written",
        "posted_threads",
        "unresolved_threads",
        "round_consumed",
    }


def test_encoder_rejects_foreign_event_with_raw_content() -> None:
    """Reject event objects that could carry unbounded reviewer content."""

    @dataclass(frozen=True)
    class UnsafeEvent:
        reviewer_summary: str

    with pytest.raises(TypeError, match="unsupported stage event"):
        encode_stage_event(cast(Any, UnsafeEvent("untrusted reviewer data")))


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("repo", "", "repo"),
        ("issue", 0, "issue"),
        ("completed_rounds", -1, "completed_rounds"),
        ("artifact_written", 1, "artifact_written"),
    ],
)
def test_encoder_rejects_invalid_fixed_fields(field: str, value: Any, match: str) -> None:
    """Reject malformed values before they reach the durable JSONL log."""
    event = PrReviewZeroThreadNogoEvent(
        repo="repo-a",
        issue=1985,
        pr=1984,
        completed_rounds=0,
        retry_attempt=1,
        retry_cap=2,
        action=ZeroThreadNogoAction.RETRY_FRESH_REVIEW,
        artifact_written=True,
    )

    with pytest.raises(ValueError, match=match):
        encode_stage_event(replace(event, **{field: value}))


def test_encoder_rejects_action_retry_invariant_violations() -> None:
    """Retry and fail-back actions must agree with the bounded retry counter."""
    event = PrReviewZeroThreadNogoEvent(
        repo="repo-a",
        issue=1985,
        pr=1984,
        completed_rounds=0,
        retry_attempt=1,
        retry_cap=2,
        action=ZeroThreadNogoAction.RETRY_FRESH_REVIEW,
        artifact_written=True,
    )

    with pytest.raises(ValueError, match="exceeds retry cap"):
        encode_stage_event(replace(event, retry_attempt=3))
    with pytest.raises(ValueError, match="has not exceeded retry cap"):
        encode_stage_event(
            replace(event, action=ZeroThreadNogoAction.FAIL_BACK_AGENT_ERROR, retry_attempt=2)
        )
