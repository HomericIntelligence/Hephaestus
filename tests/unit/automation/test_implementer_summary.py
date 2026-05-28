"""Tests for ImplementationSummaryPrinter accounting.

Focus: the summary must count outcome classes (successful / deferred /
skipped-because-PR-exists / failed) without double-counting. A skip-because-PR
result carries ``success=True`` and a ``pr_number`` but must NOT inflate the
"Successful" implementation count.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from hephaestus.automation.implementer_summary import ImplementationSummaryPrinter
from hephaestus.automation.models import WorkerResult


def _printer() -> ImplementationSummaryPrinter:
    wm = MagicMock()
    wm.preserved = []
    return ImplementationSummaryPrinter(wm)


def test_already_has_pr_counted_separately(caplog: pytest.LogCaptureFixture) -> None:
    """A skip-because-PR result is counted under its own line, not Successful."""
    results = {
        1: WorkerResult(issue_number=1, success=True, pr_number=101),  # real success
        2: WorkerResult(issue_number=2, success=True, pr_number=202, already_has_pr=True),
        3: WorkerResult(issue_number=3, success=True, plan_review_not_approved=True),
        4: WorkerResult(issue_number=4, success=False, error="boom"),
    }
    with caplog.at_level(logging.INFO):
        _printer().print(results)

    text = caplog.text
    assert "Successful: 1" in text
    assert "Deferred (awaiting APPROVED plan-review): 1" in text
    assert "Skipped (open PR already exists): 1" in text
    assert "Failed: 1" in text
    # The skipped issue lists its PR but is NOT under "Successful PRs".
    assert "#2: PR #202" in text


def test_no_skips_omits_skip_section(caplog: pytest.LogCaptureFixture) -> None:
    """With no skips, the skip count is zero (line still present, value 0)."""
    results = {1: WorkerResult(issue_number=1, success=True, pr_number=101)}
    with caplog.at_level(logging.INFO):
        _printer().print(results)

    assert "Successful: 1" in caplog.text
    assert "Skipped (open PR already exists): 0" in caplog.text
