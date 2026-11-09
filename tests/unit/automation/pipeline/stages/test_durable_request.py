"""Tests for the shared pending GitHub request payload helper."""

from __future__ import annotations

from pathlib import Path

import hephaestus
from hephaestus.automation.pipeline.stages.durable_request import (
    PENDING_GITHUB_REQUEST,
    pending_request,
    pending_request_matches,
)
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem

# The single home of the payload key literal. The pin names the path
# components of the owner file relative to the package root, so a module
# that merely shares the stem is not the owner.
_KEY_OWNER = Path("automation") / "pipeline" / "stages" / "durable_request.py"
_PIPELINE_TESTS = Path(__file__).resolve().parents[1]


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


def test_the_key_literal_has_one_production_home() -> None:
    """Only durable_request defines the payload key literal (#3522)."""
    package_root = Path(hephaestus.__file__).parent
    owner = package_root / _KEY_OWNER
    assert owner.is_file()

    offenders = sorted(
        py.relative_to(package_root).as_posix()
        for py in package_root.rglob("*.py")
        if py != owner and PENDING_GITHUB_REQUEST in py.read_text(encoding="utf-8")
    )

    assert offenders == []


def test_pipeline_tests_import_the_key_constant() -> None:
    """No pipeline test rewrites the key as a string literal (#3522)."""
    offenders = sorted(
        py.relative_to(_PIPELINE_TESTS).as_posix()
        for py in _PIPELINE_TESTS.rglob("*.py")
        if PENDING_GITHUB_REQUEST in py.read_text(encoding="utf-8")
    )

    assert offenders == []
