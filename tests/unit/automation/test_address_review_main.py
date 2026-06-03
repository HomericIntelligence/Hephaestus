"""Smoke tests for address_review.main() to lock in current CLI behavior.

These tests capture the current behavior of ``address_review.main()`` so
the upcoming dedupe of helpers shared with ``pr_reviewer.py`` (issue #599)
can be verified as a pure move-and-delegate refactor.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hephaestus.automation import address_review
from hephaestus.automation.models import WorkerResult


def _patched_run(return_value: dict[int, WorkerResult]):
    """Return a context-manager-friendly patch of AddressReviewer.run."""
    return patch.object(address_review.AddressReviewer, "run", return_value=return_value)


def test_main_returns_0_when_no_unresolved_threads(monkeypatch) -> None:
    """When run() returns an empty dict, main() exits 0."""
    monkeypatch.setattr(
        "sys.argv", ["address_review", "--issues", "1", "--no-ui", "--dry-run", "--agent", "claude"]
    )
    with (
        patch.object(address_review.AddressReviewer, "__init__", return_value=None),
        _patched_run({}),
    ):
        assert address_review.main() == 0


def test_main_returns_0_on_all_success(monkeypatch) -> None:
    """When every WorkerResult.success is True, main() exits 0."""
    monkeypatch.setattr(
        "sys.argv", ["address_review", "--issues", "1", "--no-ui", "--dry-run", "--agent", "claude"]
    )
    results = {1: WorkerResult(issue_number=1, success=True, pr_number=42)}
    with (
        patch.object(address_review.AddressReviewer, "__init__", return_value=None),
        _patched_run(results),
    ):
        assert address_review.main() == 0


def test_main_returns_1_on_any_failure(monkeypatch) -> None:
    """When any WorkerResult.success is False, main() exits 1."""
    monkeypatch.setattr(
        "sys.argv", ["address_review", "--issues", "1", "--no-ui", "--dry-run", "--agent", "claude"]
    )
    results = {1: WorkerResult(issue_number=1, success=False, error="boom")}
    with (
        patch.object(address_review.AddressReviewer, "__init__", return_value=None),
        _patched_run(results),
    ):
        assert address_review.main() == 1


def test_main_returns_130_on_keyboard_interrupt(monkeypatch) -> None:
    """A KeyboardInterrupt during run() is caught and returns 130."""
    monkeypatch.setattr(
        "sys.argv", ["address_review", "--issues", "1", "--no-ui", "--dry-run", "--agent", "claude"]
    )
    boom = MagicMock(side_effect=KeyboardInterrupt())
    with (
        patch.object(address_review.AddressReviewer, "__init__", return_value=None),
        patch.object(address_review.AddressReviewer, "run", boom),
    ):
        assert address_review.main() == 130
