"""Tests for the shared pending GitHub request payload helper."""

from __future__ import annotations

from hephaestus.automation.pipeline.stages.durable_request import (
    PENDING_GITHUB_REQUEST,
    pending_request,
    pending_request_matches,
)
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem


def _item(payload: dict[str, object] | None = None) -> WorkItem:
    """Build one work item with the supplied payload."""
    item = WorkItem(repo="test-repo", kind=ItemKind.ISSUE, issue=1)
    if payload:
        item.payload.update(payload)
    return item


def test_pending_request_returns_the_stored_request() -> None:
    """A stored request is returned unchanged."""
    request = object()
    assert pending_request(_item({PENDING_GITHUB_REQUEST: request})) is request


def test_pending_request_returns_none_for_an_absent_key() -> None:
    """An absent key reads as no live request."""
    assert pending_request(_item()) is None


def test_pending_request_matches_accepts_the_same_request() -> None:
    """The live request matches an equal request object."""
    request = object()
    assert pending_request_matches(_item({PENDING_GITHUB_REQUEST: request}), request) is True


def test_pending_request_matches_rejects_a_different_request() -> None:
    """A different live request does not match."""
    assert pending_request_matches(_item({PENDING_GITHUB_REQUEST: object()}), object()) is False


def test_pending_request_matches_rejects_an_absent_pending() -> None:
    """No live request never matches, even against a request value."""
    assert pending_request_matches(_item(), object()) is False
