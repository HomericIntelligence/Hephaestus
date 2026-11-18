"""Dependency-block backoff ladder for the implementation stage (#3625)."""

from __future__ import annotations

from types import SimpleNamespace

from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages.implementation.implementation import (
    _clear_dependency_retry,
    _dependency_retry,
)


def _item() -> SimpleNamespace:
    return SimpleNamespace(issue=4242, payload={})


def test_ladder_starts_at_one_second_and_caps() -> None:
    """The park delay walks 1/5/15/60/300 and holds at 300."""
    item = _item()
    expected = [1.0, 5.0, 15.0, 60.0, 300.0, 300.0, 300.0]
    seen: list[float] = []
    for _ in range(len(expected)):
        outcome = _dependency_retry(item, "dependency #3325 is still open")  # type: ignore[arg-type]
        assert outcome.disposition == Disposition.RETRY
        seen.append(item.payload["retry_delay_s"])
    assert seen == expected


def test_reason_is_sticky_until_cleared() -> None:
    """Reason and counter survive across cycles; clear resets both."""
    item = _item()
    _dependency_retry(item, "reason one")  # type: ignore[arg-type]
    _dependency_retry(item, "reason two")  # type: ignore[arg-type]
    assert item.payload["dependency_blocked_reason"] == "reason two"
    assert item.payload["dependency_retry_attempt"] == 2
    _clear_dependency_retry(item)  # type: ignore[arg-type]
    assert "dependency_blocked_reason" not in item.payload
    assert "retry_delay_s" not in item.payload
    assert "dependency_retry_attempt" not in item.payload


def test_backoff_restarts_after_a_successful_read() -> None:
    """A successful dependency read resets the ladder to 1.0 s."""
    item = _item()
    for _ in range(4):
        _dependency_retry(item, "reason")  # type: ignore[arg-type]
    _clear_dependency_retry(item)  # type: ignore[arg-type]
    _dependency_retry(item, "reason again")  # type: ignore[arg-type]
    assert item.payload["retry_delay_s"] == 1.0


def test_no_item_identity_does_not_crash() -> None:
    """Missing issue identity degrades the log line only, never raises."""
    item = SimpleNamespace(issue=None, payload={})
    outcome = _dependency_retry(item, "reason")  # type: ignore[arg-type]
    assert outcome.disposition == Disposition.RETRY
