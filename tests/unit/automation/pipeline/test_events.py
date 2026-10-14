"""Tests for the bounded stage-event surface."""

from __future__ import annotations

from typing import Any, cast

import pytest

from hephaestus.automation.pipeline.events import RepositoryBusyEvent, encode_stage_event


def test_legacy_zero_thread_event_contract_is_removed() -> None:
    """A textual zero-thread anomaly cannot become a durable stage event."""
    with pytest.raises(TypeError, match="unsupported stage event"):
        encode_stage_event(cast(Any, object()))


def test_repository_busy_event_has_an_exact_bounded_schema() -> None:
    """The terminal event contains only typed checkout contention data."""
    event = RepositoryBusyEvent(
        repository="repo-a",
        operation="sync_checkout",
        elapsed_s=12.5,
        cumulative_lock_wait_s=3.25,
    )

    name, fields = encode_stage_event(event)

    assert name == "repository_busy"
    assert fields == {
        "repository": "repo-a",
        "operation": "sync_checkout",
        "elapsed_s": 12.5,
        "cumulative_lock_wait_s": 3.25,
    }
    assert event.summary() == (
        "repository_busy: repository=repo-a operation=sync_checkout "
        "elapsed=12.500s lock_wait=3.250s cause=lock_timeout"
    )


@pytest.mark.parametrize(
    "event",
    [
        RepositoryBusyEvent("", "clone", 1.0, 1.0),
        RepositoryBusyEvent("repo-a", "", 1.0, 1.0),
        RepositoryBusyEvent("repo-a", "clone", -1.0, 1.0),
        RepositoryBusyEvent("repo-a", "clone", float("inf"), 1.0),
        RepositoryBusyEvent("repo-a", "clone", 1.0, float("nan")),
    ],
)
def test_repository_busy_event_rejects_invalid_fields(event: RepositoryBusyEvent) -> None:
    """Invalid event values cannot enter the durable log."""
    with pytest.raises((TypeError, ValueError)):
        encode_stage_event(event)
