"""FinishedStage tests: ledger record, cleanup branches (#1817).

Doc section "8. finished": record ItemResult [M]; worktree cleanup [W:G] —
remove on pass, preserve on fail (preserved list feeds the summary footer).
Terminal: no outgoing routes.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from hephaestus.automation.pipeline.jobs import GitJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages.base import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.finished import FinishedStage
from hephaestus.automation.pipeline.work_item import ItemKind, ItemResult, WorkItem


@pytest.fixture
def ledger() -> list[ItemResult]:
    """Fresh coordinator-owned run ledger."""
    return []


@pytest.fixture
def preserved() -> list[tuple[int, str]]:
    """Fresh preserved-worktree list."""
    return []


@pytest.fixture
def stage(ledger: list[ItemResult], preserved: list[tuple[int, str]]) -> FinishedStage:
    """FinishedStage bound to the fixture ledger/preserved lists."""
    return FinishedStage(ledger, preserved)


def _item(
    *,
    passed: bool = True,
    reason: str = "ok",
    worktree: str = "",
    state: str = "ENTER",
) -> WorkItem:
    item = WorkItem(
        repo="repo-a",
        kind=ItemKind.ISSUE,
        issue=42,
        stage=StageName.FINISHED,
        state=state,
        worktree=worktree,
    )
    item.result = ItemResult(passed=passed, reason=reason, final_stage=StageName.MERGE_WAIT)
    return item


class TestRecord:
    """Step 1 [M]: record the ItemResult in the run ledger."""

    def test_enter_advances_to_record(self, stage: FinishedStage, make_ctx: Any) -> None:
        result = stage.step(_item(), make_ctx())

        assert isinstance(result, Continue) and result.next_state == "RECORD"

    def test_record_appends_result_once(
        self, stage: FinishedStage, ledger: list[ItemResult], make_ctx: Any
    ) -> None:
        """Re-stepping RECORD never double-records (idempotent sink)."""
        ctx = make_ctx()
        item = _item(state="RECORD")

        first = stage.step(item, ctx)
        second = stage.step(item, ctx)

        assert isinstance(first, Continue) and first.next_state == "CLEANUP"
        assert isinstance(second, Continue)
        assert ledger == [item.result]

    def test_missing_result_records_internal_failure(
        self, stage: FinishedStage, ledger: list[ItemResult], make_ctx: Any
    ) -> None:
        """Defensive: an item without a result is recorded as failed, not lost."""
        item = _item(state="RECORD")
        item.result = None

        stage.step(item, make_ctx())

        assert len(ledger) == 1
        assert not ledger[0].passed
        assert "no result" in ledger[0].reason


class TestCleanup:
    """Step 2 [W:G]: remove on pass, preserve on fail."""

    def test_pass_with_worktree_submits_remove_job(
        self, stage: FinishedStage, make_ctx: Any
    ) -> None:
        item = _item(passed=True, worktree="/wt/issue-42", state="CLEANUP")

        result = stage.step(item, make_ctx())

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, GitJob) and result.job.op == "remove_worktree"
        assert result.job.kwargs == {"issue_number": 42, "force": True}
        assert result.on_done_state == "DONE"

    def test_fail_with_worktree_preserves_for_debugging(
        self,
        stage: FinishedStage,
        preserved: list[tuple[int, str]],
        make_ctx: Any,
    ) -> None:
        """Preserve-on-fail: recorded for the summary, no removal job."""
        item = _item(passed=False, reason="boom", worktree="/wt/issue-42", state="CLEANUP")

        result = stage.step(item, make_ctx())

        assert isinstance(result, Continue) and result.next_state == "DONE"
        assert preserved == [(42, "/wt/issue-42")]

    def test_fail_preserve_is_idempotent(
        self,
        stage: FinishedStage,
        preserved: list[tuple[int, str]],
        make_ctx: Any,
    ) -> None:
        ctx = make_ctx()
        item = _item(passed=False, worktree="/wt/issue-42", state="CLEANUP")

        stage.step(item, ctx)
        item.state = "CLEANUP"
        stage.step(item, ctx)

        assert preserved == [(42, "/wt/issue-42")]

    def test_no_worktree_skips_cleanup(self, stage: FinishedStage, make_ctx: Any) -> None:
        result = stage.step(_item(state="CLEANUP"), make_ctx())

        assert isinstance(result, Continue) and result.next_state == "DONE"

    def test_dry_run_pass_never_submits_removal(self, stage: FinishedStage, make_ctx: Any) -> None:
        item = _item(passed=True, worktree="/wt/issue-42", state="CLEANUP")

        result = stage.step(item, make_ctx(dry_run=True))

        assert isinstance(result, Continue) and result.next_state == "DONE"


class TestTerminal:
    """DONE is terminal; job failures are logged, never fatal."""

    def test_done_emits_terminal_pass(self, stage: FinishedStage, make_ctx: Any) -> None:
        result = stage.step(_item(state="DONE"), make_ctx())

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.FINISH_PASS

    def test_unknown_state_fails(self, stage: FinishedStage, make_ctx: Any) -> None:
        result = stage.step(_item(state="BOGUS"), make_ctx())

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.FINISH_FAIL

    def test_on_enter_never_routes_away(self, stage: FinishedStage, make_ctx: Any) -> None:
        assert stage.on_enter(_item(), make_ctx()) is None

    def test_cleanup_failure_logged_non_fatal(
        self,
        stage: FinishedStage,
        make_ctx: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        item = _item(worktree="/wt/issue-42", state="CLEANUP")

        with caplog.at_level(logging.WARNING):
            stage.on_job_done(item, JobResult(ok=False, error="dirty tree"), make_ctx())

        assert any("cleanup failed" in record.message for record in caplog.records)

    def test_cleanup_success_logs_nothing(
        self,
        stage: FinishedStage,
        make_ctx: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING):
            stage.on_job_done(_item(state="CLEANUP"), JobResult(ok=True), make_ctx())

        assert caplog.records == []
