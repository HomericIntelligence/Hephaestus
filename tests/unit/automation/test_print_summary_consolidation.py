"""Regression tests for issue #1461 worker-summary consolidation.

The four reviewer/driver classes must delegate worker-summary printing to
``print_worker_summary`` rather than re-implementing it (DRY).

The four classes ``CIDriver``, ``PRReviewer``, ``AddressReviewer`` and
``PlanReviewer`` once each carried a near-identical ``_print_summary`` body
(total/successful/failed computation, the ``"=" * 60`` banner, and the
failed-issue loop). PR #1612 consolidated that into the single canonical
``print_worker_summary`` helper in ``_review_utils.py``; these tests guard
against the duplication drifting back in.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import hephaestus.automation as automation_pkg
from hephaestus.automation.address_review import AddressReviewer
from hephaestus.automation.ci_driver import CIDriver
from hephaestus.automation.models import WorkerResult
from hephaestus.automation.plan_reviewer import PlanReviewer
from hephaestus.automation.pr_reviewer import PRReviewer

_AUTOMATION_DIR = Path(automation_pkg.__file__).parent

# The four classes the issue named, the module that holds each one, and the
# exact ``print_worker_summary`` call the delegate must make.
_DELEGATING_MODULES = (
    "ci_driver.py",
    "pr_reviewer.py",
    "address_review.py",
    "plan_reviewer.py",
)


def test_named_classes_carry_no_inline_summary_separator() -> None:
    """None of the four issue-named files may re-introduce the 60-char banner.

    The ``"=" * 60`` separator now lives only in the canonical
    ``print_worker_summary`` helper (and the two sanctioned implementer
    variants); re-appearance in any of these four files means the DRY
    consolidation from PR #1612 has drifted.
    """
    for name in _DELEGATING_MODULES:
        source = (_AUTOMATION_DIR / name).read_text(encoding="utf-8")
        assert '"=" * 60' not in source, (
            f"{name} re-introduced an inline summary separator; it must delegate "
            f"to print_worker_summary (issue #1461)."
        )


def test_ci_driver_delegates_to_print_worker_summary() -> None:
    """``CIDriver._print_summary`` must delegate with the CI Driver title."""
    results: dict[int, WorkerResult] = {}
    with patch("hephaestus.automation.ci_driver.print_worker_summary") as mock_summary:
        CIDriver._print_summary(object.__new__(CIDriver), results)
    mock_summary.assert_called_once_with("CI Driver Summary", results)


def test_pr_reviewer_delegates_to_print_worker_summary() -> None:
    """``PRReviewer._print_summary`` must delegate with the PR-specific args."""
    results: dict[int, WorkerResult] = {}
    with patch("hephaestus.automation.pr_reviewer.print_worker_summary") as mock_summary:
        PRReviewer._print_summary(object.__new__(PRReviewer), results)
    mock_summary.assert_called_once_with(
        "PR Review Summary",
        results,
        count_noun="PRs",
        failed_header="\nFailed issues:",
    )


def test_address_reviewer_delegates_to_print_worker_summary() -> None:
    """``AddressReviewer._print_summary`` must delegate with its header arg."""
    results: dict[int, WorkerResult] = {}
    with patch("hephaestus.automation.address_review.print_worker_summary") as mock_summary:
        AddressReviewer._print_summary(object.__new__(AddressReviewer), results)
    mock_summary.assert_called_once_with(
        "Address Review Summary",
        results,
        failed_header="\nFailed issues:",
    )


def test_plan_reviewer_delegates_to_print_worker_summary() -> None:
    """``PlanReviewer._print_summary`` must delegate with the Plan title."""
    results: dict[int, WorkerResult] = {}
    with patch("hephaestus.automation.plan_reviewer.print_worker_summary") as mock_summary:
        PlanReviewer._print_summary(object.__new__(PlanReviewer), results)
    mock_summary.assert_called_once_with("Plan Review Summary", results)
