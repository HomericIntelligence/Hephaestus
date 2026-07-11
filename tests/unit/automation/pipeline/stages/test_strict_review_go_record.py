"""Regression coverage for #2053.

pr_review never arms auto-merge on its own, and merge_wait's ARM step
requires a durable, head-bound independent strict-review GO before arming.

AC1: a clean internal PR-review GO does not arm auto-merge by itself.
AC2: the current PR head must have a durable independent strict-review GO
     before arming (blocked without one; a stale-head record does not
     qualify; a qualifying record advances the item).
AC4: focused regression tests cover both the blocked and eligible arming
     paths through real stage code (not mocks).
"""

from __future__ import annotations

from typing import Any

from hephaestus.automation.claude_invoke import ReviewVerdict
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import Continue, StageOutcome
from hephaestus.automation.pipeline.stages.merge_wait import ARM, POLL, MergeWaitStage
from hephaestus.automation.pipeline.stages.pr_review import PrReviewStage
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _verdict(kind: str) -> ReviewVerdict:
    """Build a ReviewVerdict of the given kind for EVAL tests."""
    return ReviewVerdict(grade=None, verdict=kind, raw=f"review text ({kind})")


class TestPrReviewNeverArms:
    """AC1: pr_review's own clean GO is never sufficient to arm auto-merge."""

    def test_clean_go_marks_but_does_not_arm(self, make_ctx: Any, make_work_item: Any) -> None:
        stage = PrReviewStage()
        github = FakeStageGitHub(unresolved=[(0, 0)])
        ctx = make_ctx(github=github)
        ctx.config.enable_follow_up = False
        item = make_work_item(issue=1, pr=1001, state="EVAL")
        item.payload["review_verdict"] = _verdict("GO")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE
        assert ("mark_pr_implementation_go", (1001,)) in github.mutation_log
        assert ("arm_auto_merge", (1001,)) not in github.mutation_log


class TestMergeWaitStrictReviewGate:
    """AC2/AC4: merge_wait's ARM step is the sole auto-merge authority."""

    def test_arm_stays_blocked_without_a_qualifying_strict_review_record(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """No strict-review record for the current head -> stays blocked."""
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state={"state": "OPEN", "headRefOid": "abc123"})
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state=ARM)

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "strict_gate_unavailable")
        assert ("arm_auto_merge", (1001,)) not in github.mutation_log
        assert ("defer_auto_merge", (1001,)) in github.mutation_log
        assert item.armed is False

    def test_stale_head_strict_review_record_does_not_arm(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A record for a DIFFERENT (stale, pre-push) head must not authorize arming."""
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state={"state": "OPEN", "headRefOid": "newsha456"})
        github.record_strict_review_go(1001, "oldsha123")  # stale head
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state=ARM)

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "strict_gate_unavailable")
        assert ("arm_auto_merge", (1001,)) not in github.mutation_log
        assert item.armed is False

    def test_qualifying_strict_review_go_advances_and_arms(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A durable strict-review GO for the CURRENT head is the eligible path."""
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state={"state": "OPEN", "headRefOid": "abc123"})
        github.record_strict_review_go(1001, "abc123")  # matches current head
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1, pr=1001, state=ARM)

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == POLL
        assert ("arm_auto_merge", (1001,)) in github.mutation_log
        assert item.armed is True
