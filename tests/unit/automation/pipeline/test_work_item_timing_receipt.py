"""Focused tests for bounded timing receipts and sanitized terminal facts."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from hephaestus.automation.pipeline.coordinator_runtime import (
    CoordinatorRuntime,
    _bounded_retry_cause,
)
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.summary import TerminalSummary
from hephaestus.automation.pipeline.work_item import (
    ItemKind,
    ItemResult,
    PhaseActivity,
    RetryCause,
    TimingReceipt,
    WorkItem,
    exact_source_identity,
    sanitized_terminal_summary,
)

_SHA_A = "a" * 40
_SHA_B = "b" * 40


def _window(start: datetime, end: datetime) -> TimingReceipt:
    return TimingReceipt.create(
        phase=StageName.PLANNING.value,
        started=start,
        ended=end,
        activity=PhaseActivity.ACTIVE,
        attempts=3,
        retry_cause=RetryCause.TEST_FAILURE,
    )


class TestRetryCauseSet:
    """The retry cause domain is closed and names the required causes."""

    def test_closed_set(self) -> None:
        assert {cause.value for cause in RetryCause} == {
            "none",
            "source_change",
            "review_finding",
            "test_failure",
            "hosted_check_failure",
            "remote_movement",
            "tool_failure",
            "external_wait",
        }


class TestTimingReceipt:
    """One receipt carries bounded, closed, machine-readable facts."""

    def test_create_and_to_dict_are_bounded(self) -> None:
        start = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
        receipt = _window(start, start + timedelta(seconds=1.25))

        assert receipt == TimingReceipt(
            phase="planning",
            start="2026-11-25T12:00:00.000Z",
            end="2026-11-25T12:00:01.250Z",
            elapsed_s=1.25,
            activity=PhaseActivity.ACTIVE,
            attempts=3,
            retry_cause=RetryCause.TEST_FAILURE,
        )
        assert receipt.to_dict() == {
            "phase": "planning",
            "start": "2026-11-25T12:00:00.000Z",
            "end": "2026-11-25T12:00:01.250Z",
            "elapsed_s": 1.25,
            "activity": "active",
            "attempts": 3,
            "retry_cause": "test_failure",
        }

    @pytest.mark.parametrize(
        ("phase", "started", "ended", "activity", "attempts", "cause"),
        [
            (
                "foreign-stage",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 1, tzinfo=UTC),
                "active",
                0,
                "none",
            ),
            (
                "planning",
                datetime(2026, 1, 1),
                datetime(2026, 1, 1, tzinfo=UTC),
                "active",
                0,
                "none",
            ),
            (
                "planning",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 1, tzinfo=UTC),
                "spinning",
                0,
                "none",
            ),
            (
                "planning",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 1, tzinfo=UTC),
                "active",
                0,
                "raw agent output",
            ),
            (
                "planning",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 1, tzinfo=UTC),
                "active",
                -1,
                "none",
            ),
        ],
    )
    def test_create_rejects_out_of_domain_fields(
        self,
        phase: str,
        started: datetime,
        ended: datetime,
        activity: str,
        attempts: int,
        cause: str,
    ) -> None:
        with pytest.raises(ValueError):
            TimingReceipt.create(
                phase=phase,
                started=started,
                ended=ended,
                activity=activity,
                attempts=attempts,
                retry_cause=cause,
            )

    def test_elapsed_never_goes_negative(self) -> None:
        start = datetime(2026, 1, 2, tzinfo=UTC)
        receipt = _window(start, start - timedelta(seconds=5))
        assert receipt.elapsed_s == 0.0

    def test_receipt_is_frozen(self) -> None:
        receipt = _window(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
        with pytest.raises(AttributeError):
            receipt.phase = "implementation"  # type: ignore[misc]


class TestWorkItemTiming:
    """A work item keeps exactly one timing receipt."""

    def test_record_timing_replaces_the_single_receipt(self) -> None:
        item = WorkItem(repo="org/repo", kind=ItemKind.ISSUE, issue=1)
        item.attempts["plan"] = 2
        item.attempts["clone"] = 1
        start = datetime(2026, 1, 1, tzinfo=UTC)
        first = item.record_timing(
            started=start,
            ended=start + timedelta(seconds=1),
            retry_cause=RetryCause.NONE,
        )
        second = item.record_timing(
            started=start + timedelta(seconds=1),
            ended=start + timedelta(seconds=2),
            activity=PhaseActivity.WAITING,
            retry_cause=RetryCause.EXTERNAL_WAIT,
        )

        assert item.timing_receipt is second
        assert first != second
        assert second.attempts == 3
        assert second.activity is PhaseActivity.WAITING
        assert second.retry_cause is RetryCause.EXTERNAL_WAIT

    def test_new_items_have_no_receipt(self) -> None:
        item = WorkItem(repo="org/repo", kind=ItemKind.REPO)
        assert item.timing_receipt is None

    def test_record_timing_uses_current_stage_by_default(self) -> None:
        item = WorkItem(repo="org/repo", kind=ItemKind.ISSUE, issue=1)
        item.stage = StageName.IMPLEMENTATION
        now = datetime(2026, 1, 1, tzinfo=UTC)
        receipt = item.record_timing(started=now, ended=now)
        assert receipt.phase == "implementation"


class TestSanitizedTerminalSummary:
    """The terminal summary names bounded identities only."""

    def test_names_issue_pr_and_config_source_identity(self) -> None:
        item = WorkItem(repo="org/repo", kind=ItemKind.PR, issue=7, pr=9)
        summary = sanitized_terminal_summary(item, source_revision=_SHA_A)
        assert summary == {"issue": 7, "pr": 9, "source_identity": _SHA_A}

    def test_uses_a_bounded_payload_identity_when_config_is_absent(self) -> None:
        item = WorkItem(repo="org/repo", kind=ItemKind.ISSUE, issue=7)
        item.payload["_impl_source_revision"] = _SHA_B
        item.payload["_private_path"] = "/Users/private/token"
        item.payload["_prompt"] = "secret instructions"
        summary = sanitized_terminal_summary(item)

        assert summary == {"issue": 7, "pr": None, "source_identity": _SHA_B}
        assert all("/Users" not in str(value) for value in summary.values())
        assert "secret" not in str(summary)

    def test_omits_source_identity_when_no_valid_sha_exists(self) -> None:
        item = WorkItem(repo="org/repo", kind=ItemKind.REPO)
        item.payload["_direct_scope_base_sha"] = "abc123"
        assert exact_source_identity(item) is None
        assert sanitized_terminal_summary(item) == {"issue": None, "pr": None}

    def test_config_identity_takes_priority(self) -> None:
        item = WorkItem(repo="org/repo", kind=ItemKind.ISSUE, issue=7)
        item.payload["_worktree_cleanup_head_sha"] = _SHA_B
        assert exact_source_identity(item, source_revision=_SHA_A) == _SHA_A


class TestCoordinatorTimingHooks:
    """Coordinator hooks only record facts; they do not change workflow state."""

    def test_bounded_retry_cause_accepts_closed_signal_only(self) -> None:
        item = WorkItem(repo="org/repo", kind=ItemKind.ISSUE, issue=1)
        assert _bounded_retry_cause(item) is RetryCause.NONE
        item.payload["_timing_retry_cause"] = "hosted_check_failure"
        assert _bounded_retry_cause(item) is RetryCause.HOSTED_CHECK_FAILURE
        item.payload["_timing_retry_cause"] = "free-form raw output"
        assert _bounded_retry_cause(item) is RetryCause.NONE

    def test_observe_timing_tiles_windows_without_new_state(self) -> None:
        clock = [1_000.0]
        host = cast(Any, object.__new__(CoordinatorRuntime))
        host._wall_time = lambda: clock[0]
        item = WorkItem(repo="org/repo", kind=ItemKind.ISSUE, issue=1)
        item.attempts["plan"] = 2
        item.stage = StageName.PLANNING
        item.state = "ENTER"

        observe = cast(
            Callable[..., TimingReceipt],
            CoordinatorRuntime._observe_timing,
        )
        first = observe(host, item)
        clock[0] += 2.5
        second = observe(
            host,
            item,
            activity=PhaseActivity.WAITING,
            retry_cause=RetryCause.EXTERNAL_WAIT,
        )

        assert first.elapsed_s == 0.0
        assert second.start == first.end
        assert second.elapsed_s == 2.5
        assert second.activity is PhaseActivity.WAITING
        assert second.retry_cause is RetryCause.EXTERNAL_WAIT
        assert item.timing_receipt is second
        assert item.stage is StageName.PLANNING
        assert item.state == "ENTER"
        assert item.attempts["plan"] == 2
        assert "_timing_window_start" not in item.payload

    def test_terminal_result_records_one_sanitized_summary_event(self) -> None:
        config = PipelineConfig(
            org="org",
            repos=["repo"],
            projects_dir=Path("/nonexistent"),
            source_revision=_SHA_A,
        )
        item = WorkItem(repo="org/repo", kind=ItemKind.PR, issue=7, pr=9)
        item.stage = StageName.MERGE_WAIT
        item.result = None
        item.payload["_prompt"] = "do not retain this"
        item.payload["_private_path"] = "/private/path"
        host = cast(Any, object.__new__(CoordinatorRuntime))
        host.config = config
        host.event_log = []
        host._event_log_disabled = False
        host._terminal_summary = TerminalSummary()
        host._seen_item_ids = {id(item)}
        host.items = [item]
        host.ledger = []
        host.preserved = []
        host.recovery_preserved = []
        host._wall_time = lambda: 1_700.0

        record = cast(Callable[..., None], CoordinatorRuntime._record_terminal_result)
        result = ItemResult(
            passed=True,
            reason="merged after prompt=/private/path token=ghp_secret",
            final_stage=StageName.MERGE_WAIT,
        )
        item.result = result
        record(host, item)
        record(host, item)  # Idempotent: no second event.

        events = [entry for entry in host.event_log if entry[0] == "terminal_timing_summary"]
        assert len(events) == 1
        facts = events[0][1]
        receipt = events[0][2]
        assert facts == {"issue": 7, "pr": 9, "source_identity": _SHA_A}
        assert receipt["phase"] == "merge_wait"
        assert receipt["attempts"] == 0
        assert receipt["retry_cause"] == "none"
        assert all("private" not in str(value) for value in facts.values())
        assert "ghp_secret" not in str(events[0])
        assert item.timing_receipt is not None
        assert host._terminal_summary.total == 1
        assert id(item) not in host._seen_item_ids
