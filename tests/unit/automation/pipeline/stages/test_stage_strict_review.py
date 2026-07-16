"""Safety regressions for #2055's head-bound strict-review stage."""

from __future__ import annotations

import importlib
from typing import Any

from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import (
    Continue,
    JobRequest,
    StageOutcome,
    StrictReviewEvidence,
)
from hephaestus.automation.pipeline.work_item import ItemKind
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

_OLD_HEAD = "a" * 40
_NEW_HEAD = "b" * 40


class _HeadChangedGitHub(FakeStageGitHub):
    """Expose a live remote head that no longer matches the saved review."""

    def __init__(self) -> None:
        super().__init__(labels=["state:implementation-go"])

    def gh_pr_state(self, pr_number: int) -> dict[str, str]:
        assert pr_number == 601
        return {"state": "OPEN", "headRefOid": _NEW_HEAD}


def test_head_change_revokes_stale_go_before_another_review_attempt(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A proof for an old SHA must clear eligibility and restart at ENTER."""
    strict_review = importlib.import_module("hephaestus.automation.pipeline.stages.strict_review")
    stage = strict_review.StrictReviewStage()
    github = _HeadChangedGitHub()
    ctx = make_ctx(github=github)
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=601,
        state="HEAD_CHECK",
        payload={"strict_review_head": _OLD_HEAD, "strict_review_verdict": "GO"},
    )

    assert stage.on_enter(item, ctx) is None
    assert item.state == strict_review.ENTER
    assert "strict_review_head" not in item.payload
    assert "strict_review_verdict" not in item.payload
    assert ("defer_auto_merge", (601,)) in github.mutation_log
    assert ("arm_auto_merge", (601,)) not in github.mutation_log


def test_orphan_pr_enters_strict_review_without_a_linked_issue(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Direct/repo-discovered PRs have no issue but still receive the gate."""
    strict_review = importlib.import_module("hephaestus.automation.pipeline.stages.strict_review")
    stage = strict_review.StrictReviewStage()
    item = make_work_item(
        kind=ItemKind.PR,
        issue=None,
        pr=601,
        stage=StageName.STRICT_REVIEW,
        state=strict_review.ENTER,
    )

    result = stage.step(item, make_ctx(github=FakeStageGitHub()))

    assert result == Continue(next_state=strict_review.HEAD_CHECK)


def test_head_change_fails_closed_when_auto_merge_cannot_be_disarmed(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A moved head must not continue if an existing arm cannot be revoked."""

    class _DeferFailsGitHub(_HeadChangedGitHub):
        def defer_auto_merge(self, pr_number: int) -> None:
            del pr_number
            raise RuntimeError("still armed")

    strict_review = importlib.import_module("hephaestus.automation.pipeline.stages.strict_review")
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=601,
        state="HEAD_CHECK",
        payload={"strict_review_head": _OLD_HEAD},
    )

    result = strict_review.StrictReviewStage().on_enter(item, make_ctx(github=_DeferFailsGitHub()))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "auto_merge_disable_failed")


def test_review_job_receives_fresh_current_head_evidence(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Strict review dispatches only with evidence bound to its captured head."""
    strict_review = importlib.import_module("hephaestus.automation.pipeline.stages.strict_review")
    evidence = StrictReviewEvidence(
        head_sha=_NEW_HEAD,
        diff="diff --git a/file.py b/file.py\n+",
        ci_status="- unit: status=completed, conclusion=success, non-required",
        prior_pr_review_verdict="Grade: A\nVerdict: GO",
    )
    github = FakeStageGitHub(strict_evidence=evidence)
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=601,
        state=strict_review.REVIEW_WAIT,
        payload={"strict_review_head": _NEW_HEAD},
    )

    result = strict_review.StrictReviewStage().step(item, make_ctx(github=github))

    assert isinstance(result, JobRequest)
    assert github.strict_evidence_calls == [(601, _NEW_HEAD)]
    assert result.job.prompt_kwargs["head_sha"] == _NEW_HEAD
    assert result.job.prompt_kwargs["diff"] == evidence.diff
    assert result.job.prompt_kwargs["ci_status"] == evidence.ci_status
    assert result.job.prompt_kwargs["prior_pr_review_verdict"] == evidence.prior_pr_review_verdict


def test_missing_current_head_evidence_fails_closed_to_real_implementation(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A read failure or empty/ambiguous diff becomes a durable strict NOGO."""
    strict_review = importlib.import_module("hephaestus.automation.pipeline.stages.strict_review")
    github = FakeStageGitHub(
        pr_state={"state": "OPEN", "headRefOid": _NEW_HEAD, "autoMergeRequest": None}
    )
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=601,
        state=strict_review.REVIEW_WAIT,
        payload={"strict_review_head": _NEW_HEAD},
    )

    result = strict_review.StrictReviewStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "nogo")
    assert github.strict_evidence_calls == [(601, _NEW_HEAD)]
    assert ("defer_auto_merge", (601,)) in github.mutation_log
    assert ("mark_pr_implementation_no_go", (601,)) in github.mutation_log
    assert any(
        action == "publish_strict_review_artifact" and args[-1] is False
        for action, args in github.mutation_log
    )
