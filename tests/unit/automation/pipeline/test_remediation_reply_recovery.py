"""Regression tests for failed remediation reply recovery."""

from __future__ import annotations

from hephaestus.automation.pipeline.jobs import JobResult
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.stages.implementation import (
    REMEDIATION_FAILURE_DIAGNOSTIC_MAX,
    ImplementationStage,
)
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem


def test_file_change_failure_records_bounded_redacted_recovery_evidence() -> None:
    """A failed tool event preserves only bounded recovery evidence."""
    item = WorkItem(
        repo="Hephaestus",
        kind=ItemKind.ISSUE,
        issue=2973,
        stage=StageName.IMPLEMENTATION,
        state="IMPLEMENT_WAIT",
    )
    item.payload["implementation_remediation"] = True
    error = "codex_tool_or_provider_failure: file_change status=failed " + "x" * 600

    ImplementationStage._on_implement_done(item, JobResult(ok=False, error=error))

    assert item.attempts["implement"] == 1
    assert item.attempts["remediation_reply"] == 0
    assert item.payload["implement_error"] is True
    assert item.payload["remediation_reply_inspection_required"] is True
    assert (
        item.payload["remediation_failure_diagnostic"] == error[:REMEDIATION_FAILURE_DIAGNOSTIC_MAX]
    )
