"""Tests for merge-wait when auto-merge is enabled but GitHub has not merged yet.

Required status evidence is GitHub's gate. The stage's only remaining job after
enabling auto-merge is to wait, and to fail closed when that wait expires.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from hephaestus.automation.pipeline.github_jobs import (
    MergeWaitCycleCompleted,
    RunMergeWaitCycleRequest,
)
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import StageOutcome
from hephaestus.automation.pipeline.stages.merge_wait import MergeWaitStage

MERGE_APPLY = "MERGE_APPLY"


def _request() -> RunMergeWaitCycleRequest:
    """Build one merge-wait cycle request."""
    return RunMergeWaitCycleRequest(
        issue_number=1,
        pr_number=7,
        reviewed_head_sha="a" * 40,
        deadline_s=60.0,
        cancellation=threading.Event(),
    )


def _item_with_receipt(make_work_item: Any, outcome: str, **kwargs: Any) -> Any:
    """Park an item with one correlated cycle receipt."""
    item = make_work_item(kind="pr", pr=7, issue=1)
    item.payload["reviewed_pr_head_sha"] = "a" * 40
    item.state = MERGE_APPLY
    request = _request()
    item.payload["_pending_github_request"] = request
    item.payload["_merge_wait_cycle_receipt"] = MergeWaitCycleCompleted(
        request=request, outcome=outcome, **kwargs
    )
    return item


def test_enabled_auto_merge_parks_and_retries(make_ctx: Any, make_work_item: Any) -> None:
    """A successful enablement waits for the terminal merge."""
    item = _item_with_receipt(make_work_item, "auto_merge_enabled", attempted=True)

    outcome = MergeWaitStage()._merge_apply(item, make_ctx())

    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.RETRY
    assert outcome.note == "merge_readiness_wait"


def test_already_armed_auto_merge_also_parks(make_ctx: Any, make_work_item: Any) -> None:
    """An externally armed auto-merge is also just a wait."""
    item = _item_with_receipt(make_work_item, "auto_merge_already_armed", attempted=True)

    outcome = MergeWaitStage()._merge_apply(item, make_ctx())

    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.RETRY


def test_wait_backoff_grows_and_stays_bounded(make_ctx: Any, make_work_item: Any) -> None:
    """Repeated polls back off exponentially up to the cap."""
    item = _item_with_receipt(make_work_item, "auto_merge_enabled", attempted=True)
    stage = MergeWaitStage()
    ctx = make_ctx()
    delays = []
    for _ in range(6):
        outcome = stage._merge_apply(item, ctx)
        assert isinstance(outcome, StageOutcome)
        delays.append(item.payload["retry_delay_s"])
    assert delays == sorted(delays)
    assert max(delays) <= 60.0


def test_wait_fails_closed_when_its_deadline_expires(make_ctx: Any, make_work_item: Any) -> None:
    """An expired wait is a failure, not an endless retry."""
    item = _item_with_receipt(make_work_item, "auto_merge_enabled", attempted=True)
    stage = MergeWaitStage()

    # The first poll establishes the wait. A new reviewed head must not inherit
    # a prior proof's deadline, so the deadline is set on the first poll. The
    # next cycle re-enters MERGE, which is where expiry is enforced.
    first = stage._merge_apply(item, make_ctx(now_fn=lambda: 1_000.0))
    assert isinstance(first, StageOutcome)
    assert first.disposition is Disposition.RETRY
    deadline = item.payload["merge_readiness_deadline_s"]

    item.state = "MERGE"
    outcome = stage._merge(item, make_ctx(now_fn=lambda: deadline + 1.0))

    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.FINISH_FAIL
    assert outcome.note == "merge_readiness_timeout"


def test_retryable_transport_error_retries_the_enablement(
    make_ctx: Any, make_work_item: Any
) -> None:
    """An unknown enablement outcome is retried, not accepted."""
    item = _item_with_receipt(
        make_work_item, "auto_merge_transport_error", attempted=True, retryable=True
    )

    outcome = MergeWaitStage()._merge_apply(item, make_ctx())

    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.RETRY
    assert outcome.note == "merge_not_ready"


def test_exhausted_merge_budget_fails_instead_of_retrying(
    make_ctx: Any, make_work_item: Any
) -> None:
    """The bounded merge budget is honoured."""
    item = _item_with_receipt(
        make_work_item, "auto_merge_transport_error", attempted=True, retryable=True
    )
    ctx = make_ctx(budget_fn=lambda action: 0)

    outcome = MergeWaitStage()._merge_apply(item, ctx)

    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.FINISH_FAIL
    assert outcome.note == "merge_attempts_exhausted"


def test_malformed_enablement_response_is_not_retried(make_ctx: Any, make_work_item: Any) -> None:
    """An uninterpretable response fails the item rather than looping."""
    item = _item_with_receipt(
        make_work_item, "auto_merge_malformed", attempted=True, retryable=False
    )

    outcome = MergeWaitStage()._merge_apply(item, make_ctx())

    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.FINISH_FAIL
    assert outcome.note == "auto_merge_malformed"


def test_lost_approval_label_fails_back_to_review(make_ctx: Any, make_work_item: Any) -> None:
    """If the label disappears the item returns to review."""
    item = _item_with_receipt(make_work_item, "not_implementation_go", attempted=True)

    outcome = MergeWaitStage()._merge_apply(item, make_ctx())

    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.FAIL_BACK


def test_a_receipt_for_a_different_request_is_refused(make_ctx: Any, make_work_item: Any) -> None:
    """A receipt that does not match the pending request is invalid."""
    item = _item_with_receipt(make_work_item, "auto_merge_enabled", attempted=True)
    item.payload["_pending_github_request"] = RunMergeWaitCycleRequest(
        issue_number=2,
        pr_number=8,
        reviewed_head_sha="d" * 40,
        deadline_s=60.0,
        cancellation=threading.Event(),
    )

    outcome = MergeWaitStage()._merge_apply(item, make_ctx())

    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.FINISH_FAIL
    assert outcome.note == "merge_cycle_receipt_invalid"


def test_a_transport_failure_receipt_fails_the_cycle(make_ctx: Any, make_work_item: Any) -> None:
    """A job-level failure is reported, never applied."""
    item = make_work_item(kind="pr", pr=7, issue=1)
    item.state = MERGE_APPLY
    item.payload["_merge_wait_cycle_receipt_error"] = "boom"

    outcome = MergeWaitStage()._merge_apply(item, make_ctx())

    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.FINISH_FAIL
    assert outcome.note == "merge_cycle_failed"


def test_a_pull_request_without_an_issue_fails_on_enter(make_ctx: Any, make_work_item: Any) -> None:
    """A pull request with no requirements context is an operator problem."""
    item = make_work_item(kind="pr", pr=7, issue=None)

    outcome = MergeWaitStage().on_enter(item, make_ctx())

    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.FINISH_FAIL
    assert outcome.note == "merge_wait_orphan"


@pytest.mark.parametrize("state", ["", "MERGE", "MERGE_APPLY", "MW_FINISH"])
def test_known_states_are_handled(state: str, make_ctx: Any, make_work_item: Any) -> None:
    """Every declared state has a defined transition."""
    item = make_work_item(kind="pr", pr=7, issue=1)
    item.payload["reviewed_pr_head_sha"] = "a" * 40
    item.state = state

    assert MergeWaitStage().step(item, make_ctx()) is not None
