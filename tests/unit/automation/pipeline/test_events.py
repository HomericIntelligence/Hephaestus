"""Tests for closed stage-event schemas."""

from dataclasses import dataclass
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
        encode_stage_event(cast(Any, UnsafeEvent("token=secret private-endpoint")))
