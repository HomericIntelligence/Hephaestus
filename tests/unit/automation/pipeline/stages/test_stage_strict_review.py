"""Tests for the in-loop `$athena:pr-review` handoff."""

from __future__ import annotations

from typing import Any

from hephaestus.automation.claude_invoke import ReviewVerdict
from hephaestus.automation.pipeline.jobs import AgentJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.base import StrictReviewEvidence
from hephaestus.automation.pipeline.stages.strict_review import (
    EVAL,
    HEAD_CHECK,
    REVIEW_WAIT,
    StrictReviewStage,
    parse_strict_review_verdict,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

_HEAD = "a" * 40
_EVIDENCE = StrictReviewEvidence(
    head_sha=_HEAD,
    issue_title="Task",
    issue_body="Do the task.",
    diff="diff --git a/a.py b/a.py\n+",
    prior_pr_review_verdict="Grade: A\nVerdict: GO",
)


def test_review_job_uses_athena_pr_review_prompt(make_ctx: Any, make_work_item: Any) -> None:
    """The approval reviewer is explicitly the Athena PR-review skill."""
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=REVIEW_WAIT,
        payload={"strict_review_head": _HEAD},
    )
    github = FakeStageGitHub(strict_evidence=_EVIDENCE)

    result = StrictReviewStage().step(item, make_ctx(github=github))

    assert isinstance(result, JobRequest)
    assert isinstance(result.job, AgentJob)
    prompt = result.job.prompt_builder(**result.job.prompt_kwargs)
    assert "MUST invoke and follow the `$athena:pr-review` skill" in prompt
    assert "Automation-loop handoff: <GO|NOGO>" in prompt
    assert "PR-review-specific graded dimensions" not in prompt
    assert "CI-free" in prompt
    assert "collect_evidence.py" in prompt
    assert result.job.sandbox == "read-only"
    assert result.job.agent == "codex"


def test_skill_handoff_must_be_the_final_line() -> None:
    """Only the explicit post-skill handoff can grant in-loop approval."""
    good = parse_strict_review_verdict("Skill report\nAutomation-loop handoff: GO\n")
    quoted = parse_strict_review_verdict("Automation-loop handoff: GO\ntrailing text")

    assert good.verdict == "GO"
    assert quoted.verdict == "AMBIGUOUS"


def test_go_labels_current_head_for_merge_wait(make_ctx: Any, make_work_item: Any) -> None:
    """A current-head review GO is the loop's sole label producer."""
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=EVAL,
        payload={
            "strict_review_head": _HEAD,
            "strict_review_verdict": "GO",
            "strict_review_text": "Grade: A\nVerdict: GO",
        },
    )
    github = FakeStageGitHub(pr_state={"state": "OPEN", "headRefOid": _HEAD})

    result = StrictReviewStage().step(item, make_ctx(github=github))

    assert isinstance(result, Continue)
    assert item.payload["pr_review_skill_head"] == _HEAD
    assert ("mark_pr_implementation_go", (12,)) in github.mutation_log


def test_strict_review_uses_codex_even_when_the_loop_agent_is_claude(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Athena review is never run with an unenforceable Claude shell boundary."""
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=REVIEW_WAIT,
        payload={"strict_review_head": _HEAD},
    )
    github = FakeStageGitHub(strict_evidence=_EVIDENCE)

    result = StrictReviewStage().step(item, make_ctx(github=github))

    assert isinstance(result, JobRequest)
    assert isinstance(result.job, AgentJob)
    assert result.job.agent == "codex"


def test_go_revokes_label_when_a_push_races_the_label_write(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A GO for H1 cannot leave an approval label on a pushed H2."""

    class PushDuringLabelGitHub(FakeStageGitHub):
        def mark_pr_implementation_go(self, pr_number: int) -> None:
            super().mark_pr_implementation_go(pr_number)
            self._pr_state = {"state": "OPEN", "headRefOid": "b" * 40}

    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=EVAL,
        payload={
            "strict_review_head": _HEAD,
            "strict_review_verdict": "GO",
            "strict_review_text": "Grade: A\nVerdict: GO",
        },
    )
    github = PushDuringLabelGitHub(pr_state={"state": "OPEN", "headRefOid": _HEAD})

    result = StrictReviewStage().step(item, make_ctx(github=github))

    assert result == Continue(next_state=HEAD_CHECK)
    assert ("mark_pr_implementation_go", (12,)) in github.mutation_log
    assert ("defer_auto_merge", (12,)) in github.mutation_log
    assert any(action == "gh_issue_remove_labels" for action, _ in github.mutation_log)


def test_job_result_keeps_final_verdict(make_ctx: Any, make_work_item: Any) -> None:
    """A worker result is preserved for the stage's current-head decision."""
    item = make_work_item(stage=StageName.STRICT_REVIEW, pr=12, state=REVIEW_WAIT)
    stage = StrictReviewStage()

    stage.on_job_done(
        item,
        JobResult(
            ok=True,
            value=ReviewVerdict(grade="A", verdict="GO", raw="Grade: A\nVerdict: GO"),
        ),
        make_ctx(),
    )

    assert item.payload["strict_review_verdict"] == "GO"


def test_nogo_disables_auto_merge_and_returns_to_implementation(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A failed in-loop review keeps the head ineligible and gives actionable feedback."""
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=EVAL,
        payload={
            "strict_review_head": _HEAD,
            "strict_review_verdict": "NOGO",
            "strict_review_text": "Missing a regression test.\nGrade: C\nVerdict: NOGO",
        },
    )
    github = FakeStageGitHub(pr_state={"state": "OPEN", "headRefOid": _HEAD})

    result = StrictReviewStage().step(item, make_ctx(github=github))

    assert isinstance(result, StageOutcome)
    assert result.disposition is Disposition.FAIL_BACK
    assert result.note == "nogo"
    assert ("defer_auto_merge", (12,)) in github.mutation_log
    assert ("mark_pr_implementation_no_go", (12,)) in github.mutation_log
    assert any("Missing a regression test." in body for body in github.comments[12])


def test_head_drift_restarts_review_without_handing_off_old_verdict(
    make_ctx: Any, make_work_item: Any
) -> None:
    """The loop never reuses a PR-review verdict for a different head."""
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=EVAL,
        payload={
            "strict_review_head": _HEAD,
            "strict_review_verdict": "GO",
            "strict_review_text": "Grade: A\nVerdict: GO",
        },
    )
    github = FakeStageGitHub(pr_state={"state": "OPEN", "headRefOid": "b" * 40})

    result = StrictReviewStage().step(item, make_ctx(github=github))

    assert result == Continue(next_state=HEAD_CHECK)
    assert "pr_review_skill_head" not in item.payload
    assert ("defer_auto_merge", (12,)) in github.mutation_log


def test_missing_context_is_a_nogo(make_ctx: Any, make_work_item: Any) -> None:
    """Incomplete review context cannot create an approval handoff."""
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=REVIEW_WAIT,
        payload={"strict_review_head": _HEAD},
    )
    github = FakeStageGitHub(pr_state={"state": "OPEN", "headRefOid": _HEAD})

    result = StrictReviewStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "nogo")
    assert "pr_review_skill_head" not in item.payload
