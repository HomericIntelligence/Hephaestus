"""Tests for SHA-conditional normal merging in ``merge_wait`` (#2419)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.stages.base import ConditionalMergeResult
from hephaestus.automation.pipeline.stages.merge_wait import MERGE, MergeWaitStage
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

_HEAD_A = "a" * 40
_HEAD_B = "b" * 40


def _open_state(
    head: str = _HEAD_A,
    *,
    arm: object | None = None,
    base: str = "main",
) -> dict[str, object]:
    """Return a complete open PR lifecycle snapshot."""
    return {
        "state": "OPEN",
        "headRefOid": head,
        "baseRefName": base,
        "autoMergeRequest": arm,
    }


class _StateQueueGitHub(FakeStageGitHub):
    """Stage fake with ordered lifecycle reads and one typed merge outcome."""

    def __init__(
        self,
        states: Iterable[dict[str, object] | None],
        *,
        result: ConditionalMergeResult,
        labels: tuple[bool, bool] = (True, False),
        readiness: dict[str, object] | None = None,
    ) -> None:
        queued = list(states)
        super().__init__(pr_impl_state=labels, pr_state=queued[-1] if queued else None)
        self._states = queued
        self._result = result
        self._readiness = readiness

    def gh_pr_state(self, pr_number: int) -> dict[str, object] | None:
        del pr_number
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0] if self._states else None

    def merge_pr_if_head(self, pr_number: int, reviewed_sha: str) -> ConditionalMergeResult:
        self._log("merge_pr_if_head", pr_number, reviewed_sha)
        return self._result

    def gh_pr_merge_readiness(self, pr_number: int) -> dict[str, object] | None:
        del pr_number
        return self._readiness


def _item(make_work_item: Any) -> Any:
    return make_work_item(
        stage=StageName.MERGE_WAIT,
        pr=12,
        state=MERGE,
        payload={"reviewed_pr_head_sha": _HEAD_A},
    )


def test_conditional_merge_success_requires_github_to_report_merged(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Only 200 plus ``merged: true`` followed by a merged lifecycle succeeds."""
    github = _StateQueueGitHub(
        [_open_state(), {"state": "MERGED"}],
        result=ConditionalMergeResult(status=200, body={"merged": True}),
    )
    ctx = make_ctx(github=github)
    ctx.config.enable_learn = False

    result = MergeWaitStage().step(_item(make_work_item), ctx)

    assert result == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert github.mutation_log == [("merge_pr_if_head", (12, _HEAD_A))]


def test_external_auto_merge_blocks_without_label_or_merge_write(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A pre-existing external arm has priority over every merge action."""
    github = _StateQueueGitHub(
        [_open_state(arm={"enabledAt": "elsewhere"})],
        result=ConditionalMergeResult(status=200, body={"merged": True}),
    )

    result = MergeWaitStage().step(_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
    assert github.mutation_log == []


def test_missing_reviewed_head_revokes_exclusive_go_only_when_unarmed(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A restart cannot use a label without its process-local reviewed SHA."""
    github = _StateQueueGitHub(
        [_open_state(), _open_state()],
        result=ConditionalMergeResult(status=200, body={"merged": True}),
    )
    item = _item(make_work_item)
    item.payload.clear()

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "reviewed_head_missing")
    assert github.mutation_log == [("mark_pr_implementation_no_go", (12,))]


def test_label_loss_before_call_returns_to_review_without_merging(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A live label loss is an admission failure, never a merge attempt."""
    github = _StateQueueGitHub(
        [_open_state()],
        labels=(False, False),
        result=ConditionalMergeResult(status=200, body={"merged": True}),
    )

    result = MergeWaitStage().step(_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")
    assert github.mutation_log == []


def test_non_main_base_is_terminal_without_conditional_merge(
    make_ctx: Any, make_work_item: Any
) -> None:
    """The normal merge path is limited to the repository's main branch."""
    github = _StateQueueGitHub(
        [_open_state(base="release")],
        result=ConditionalMergeResult(status=200, body={"merged": True}),
    )

    result = MergeWaitStage().step(_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "non_main_base")
    assert github.mutation_log == []


def test_409_head_drift_revokes_stale_label_then_re_reviews(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A conditional-merge CAS miss cannot carry an old approval forward."""
    github = _StateQueueGitHub(
        [_open_state(), _open_state(_HEAD_B), _open_state(_HEAD_B)],
        result=ConditionalMergeResult(status=409, body={"message": "Head branch was modified"}),
    )

    result = MergeWaitStage().step(_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "reviewed_head_drift")
    assert github.mutation_log == [
        ("merge_pr_if_head", (12, _HEAD_A)),
        ("mark_pr_implementation_no_go", (12,)),
    ]


def test_409_with_new_external_arm_never_revokes_label(make_ctx: Any, make_work_item: Any) -> None:
    """A post-attempt arm race remains external and receives zero mutation."""
    github = _StateQueueGitHub(
        [_open_state(), _open_state(_HEAD_B, arm={"enabledAt": "external"})],
        result=ConditionalMergeResult(status=409, body={"message": "Head branch was modified"}),
    )

    result = MergeWaitStage().step(_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
    assert github.mutation_log == [("merge_pr_if_head", (12, _HEAD_A))]


def test_405_not_ready_timer_retries_within_merge_budget(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Ordinary protection readiness uses the timer heap rather than sleeping."""
    readiness = {**_open_state(), "mergeStateStatus": "BLOCKED", "mergeable": "MERGEABLE"}
    github = _StateQueueGitHub(
        [_open_state()],
        readiness=readiness,
        result=ConditionalMergeResult(status=405, body={"message": "not ready"}),
    )
    item = _item(make_work_item)

    result = MergeWaitStage().step(item, make_ctx(github=github, budget_fn=lambda _name: 2))

    assert result == StageOutcome(Disposition.RETRY, "merge_not_ready")
    assert item.attempts["merge"] == 1
    assert item.payload["retry_delay_s"] == 1


def test_405_conflict_is_terminal_and_never_retried(make_ctx: Any, make_work_item: Any) -> None:
    """A concrete merge conflict is not readiness and must not spin a timer."""
    readiness = {**_open_state(), "mergeStateStatus": "DIRTY", "mergeable": "CONFLICTING"}
    github = _StateQueueGitHub(
        [_open_state()],
        readiness=readiness,
        result=ConditionalMergeResult(status=405, body={"message": "conflict"}),
    )
    item = _item(make_work_item)

    result = MergeWaitStage().step(item, make_ctx(github=github, budget_fn=lambda _name: 2))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "merge_conflict")
    assert "retry_delay_s" not in item.payload


def test_200_without_merged_true_is_not_a_success_or_retry(
    make_ctx: Any, make_work_item: Any
) -> None:
    """HTTP success alone cannot claim that GitHub actually merged the PR."""
    github = _StateQueueGitHub(
        [_open_state()],
        result=ConditionalMergeResult(status=200, body={"merged": False}),
    )
    item = _item(make_work_item)

    result = MergeWaitStage().step(item, make_ctx(github=github, budget_fn=lambda _name: 2))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "merge_not_merged")
    assert item.attempts["merge"] == 1
    assert "retry_delay_s" not in item.payload


@pytest.mark.parametrize("status", [403, 404, 422])
def test_terminal_http_status_never_duplicates_merge_attempt(
    status: int, make_ctx: Any, make_work_item: Any
) -> None:
    """Authorization, absence, and validation failures require an operator."""
    github = _StateQueueGitHub(
        [_open_state()],
        result=ConditionalMergeResult(status=status, body={"message": "terminal"}),
    )
    item = _item(make_work_item)

    result = MergeWaitStage().step(item, make_ctx(github=github, budget_fn=lambda _name: 5))

    assert result == StageOutcome(Disposition.FINISH_FAIL, f"merge_http_{status}")
    assert item.attempts["merge"] == 1
    assert "retry_delay_s" not in item.payload


def test_ambiguous_transport_re_reads_before_one_bounded_retry(
    make_ctx: Any, make_work_item: Any
) -> None:
    """An uncertain request only retries after a same-head unarmed lifecycle read."""
    github = _StateQueueGitHub(
        [_open_state(), _open_state()],
        result=ConditionalMergeResult(status=None, body=None, transport_error=True),
    )
    item = _item(make_work_item)

    result = MergeWaitStage().step(item, make_ctx(github=github, budget_fn=lambda _name: 2))

    assert result == StageOutcome(Disposition.RETRY, "merge_transport_retry")
    assert item.attempts["merge"] == 1
    assert item.payload["retry_delay_s"] == 1
    assert github.mutation_log == [("merge_pr_if_head", (12, _HEAD_A))]


def test_ambiguous_transport_with_unreadable_lifecycle_is_terminal(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A lost lifecycle read after an uncertain write never permits a replay."""
    github = _StateQueueGitHub(
        [_open_state(), None],
        result=ConditionalMergeResult(status=None, body=None, transport_error=True),
    )
    item = _item(make_work_item)

    result = MergeWaitStage().step(item, make_ctx(github=github, budget_fn=lambda _name: 2))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
    assert "retry_delay_s" not in item.payload
