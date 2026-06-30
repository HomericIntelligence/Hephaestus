"""Regression tests for the consolidated worker-summary printers (#1461)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import hephaestus.automation as automation_pkg
from hephaestus.automation.address_review import AddressReviewer
from hephaestus.automation.ci_driver import CIDriver
from hephaestus.automation.models import WorkerResult
from hephaestus.automation.plan_reviewer import PlanReviewer
from hephaestus.automation.pr_reviewer import PRReviewer

_AUTOMATION_DIR = Path(automation_pkg.__file__).parent
_DELEGATING_MODULES = (
    "ci_driver.py",
    "pr_reviewer.py",
    "address_review.py",
    "plan_reviewer.py",
)


@pytest.mark.parametrize("module_name", _DELEGATING_MODULES)
def test_named_summary_modules_do_not_inline_standard_separator(module_name: str) -> None:
    """Issue-named modules must not reintroduce the duplicated summary banner."""
    source = (_AUTOMATION_DIR / module_name).read_text(encoding="utf-8")

    assert '"=" * 60' not in source


@pytest.mark.parametrize(
    ("reviewer_cls", "patch_target", "title", "expected_kwargs"),
    (
        (
            CIDriver,
            "hephaestus.automation.ci_driver.print_worker_summary",
            "CI Driver Summary",
            {},
        ),
        (
            PRReviewer,
            "hephaestus.automation.pr_reviewer.print_worker_summary",
            "PR Review Summary",
            {"count_noun": "PRs", "failed_header": "\nFailed issues:"},
        ),
        (
            AddressReviewer,
            "hephaestus.automation.address_review.print_worker_summary",
            "Address Review Summary",
            {"failed_header": "\nFailed issues:"},
        ),
        (
            PlanReviewer,
            "hephaestus.automation.plan_reviewer.print_worker_summary",
            "Plan Review Summary",
            {},
        ),
    ),
)
def test_named_summary_methods_delegate_to_print_worker_summary(
    reviewer_cls: type[Any],
    patch_target: str,
    title: str,
    expected_kwargs: dict[str, str],
) -> None:
    """The four issue-named wrappers delegate to the shared summary helper."""
    results = {1: WorkerResult(issue_number=1, success=True)}

    with patch(patch_target) as summary:
        reviewer_cls._print_summary(object(), results)

    summary.assert_called_once_with(title, results, **expected_kwargs)
