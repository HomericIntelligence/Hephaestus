"""PR review: GitHub review threads decide approval."""

from __future__ import annotations

import json
from typing import Any

from hephaestus.automation.pipeline.github_jobs import GitHubJob, ReconcilePrReviewRequest
from hephaestus.automation.pipeline.jobs import GitJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.pr_review import PrReviewStage
from hephaestus.automation.pipeline.stages.pr_review_threads import _parse_review_response
from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner
from hephaestus.automation.state_labels import STATE_PLAN_GO
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

_DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n x = 1\n+y = 2\n z = 3\n"


_CRASH = "crash"


def _review(*comments: dict[str, Any], summary: str = "ok") -> str:
    """Return a reviewer response that ends in one JSON block."""
    body = json.dumps({"verdict": "GO", "summary": summary, "comments": list(comments)})
    return f"Looked at the change.\n\n```json\n{body}\n```"


def _drive(stage: PrReviewStage, item: Any, ctx: Any, review: str | None) -> Any:
    """Run the stage to its outcome, answering each job the way the worker would."""
    entry = stage.on_enter(item, ctx)
    if entry is not None:
        return entry
    for _ in range(40):
        result = stage.step(item, ctx)
        if isinstance(result, Continue):
            item.state = result.next_state
            continue
        if not isinstance(result, JobRequest):
            return result
        job = result.job
        if isinstance(job, GitJob) and job.op == "create_worktree":
            value: Any = {"path": "/tmp/detached-review", "dirty": False}
        elif isinstance(job, GitJob) and job.op == "verify_pr_review_checkout":
            value = {"ready": True, "diff": _DIFF, "changed_paths": ["a.py"]}
        elif isinstance(job, GitJob):
            value = True
        elif isinstance(job, GitHubJob) and isinstance(job.request, ReconcilePrReviewRequest):
            value = PipelineGitHubJobRunner._reconcile_pr_review(job.request, ctx.github)
        elif review == _CRASH:
            stage.on_job_done(item, JobResult(ok=False, error="reviewer crashed"), ctx)
            item.state = result.on_done_state
            continue
        else:
            assert review is not None, "unexpected agent job"
            value = _parse_review_response(review)
        stage.on_job_done(item, JobResult(ok=True, value=value), ctx)
        item.state = result.on_done_state
    raise AssertionError("stage did not terminate")


def _setup(make_ctx: Any, make_work_item: Any, threads: list[tuple[int, int, int]]) -> Any:
    github = FakeStageGitHub(
        labels=[STATE_PLAN_GO],
        open_pr=1001,
        pr_head_branch="1-branch",
        by_severity=threads,
        pr_review_context={
            "pr_head_sha": "a" * 40,
            "pr_base_sha": "b" * 40,
            "pr_base_branch": "main",
            "pr_title": "t",
            "pr_description": "d",
        },
    )
    ctx = make_ctx(github=github)
    item = make_work_item(issue=1, pr=1001, stage=StageName.PR_REVIEW, state="ENTER")
    item.branch = "1-branch"
    return github, ctx, item


def _names(github: FakeStageGitHub) -> list[str]:
    return [name for name, *_ in github.mutation_log]


def test_review_without_comments_and_no_open_thread_approves(
    make_ctx: Any, make_work_item: Any
) -> None:
    """No unresolved thread means GO, whatever the reviewer wrote."""
    github, ctx, item = _setup(make_ctx, make_work_item, [(0, 0, 0)])

    outcome = _drive(PrReviewStage(), item, ctx, _review())

    assert outcome.disposition is Disposition.ADVANCE
    assert "mark_pr_implementation_go" in _names(github)
    assert "mark_pr_implementation_no_go" not in _names(github)


def test_grade_and_verdict_are_shown_in_the_review_body(make_ctx: Any, make_work_item: Any) -> None:
    """The grade is informational: it reaches the posted review and gates nothing."""
    github, ctx, item = _setup(make_ctx, make_work_item, [(0, 0, 0)])
    response = '```json\n{"verdict": "GO", "grade": "B", "summary": "Fine.", "comments": []}\n```'

    outcome = _drive(PrReviewStage(), item, ctx, response)

    assert outcome.disposition is Disposition.ADVANCE
    assert "Grade: B" in github.review_summaries[item.pr]


def test_free_text_review_is_read_not_rejected(make_ctx: Any, make_work_item: Any) -> None:
    """A review with no JSON block is not a failure."""
    github, ctx, item = _setup(make_ctx, make_work_item, [(0, 0, 0)])

    outcome = _drive(PrReviewStage(), item, ctx, "The change looks fine to me.")

    assert outcome.disposition is Disposition.ADVANCE
    assert "mark_pr_implementation_go" in _names(github)


def test_comment_without_severity_or_side_is_posted_and_blocks(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A posted comment becomes an open thread, and an open thread is NO-GO."""
    github, ctx, item = _setup(
        make_ctx, make_work_item, [(0, 0, 0), (0, 0, 0), (1, 0, 0), (1, 0, 0)]
    )
    review = _review({"path": "a.py", "line": 2, "body": "y is unused"})

    outcome = _drive(PrReviewStage(), item, ctx, review)

    assert outcome == StageOutcome(Disposition.FAIL_BACK, "implementation_remediation")
    assert "post_review" in _names(github)
    assert "mark_pr_implementation_no_go" in _names(github)
    assert "mark_pr_implementation_go" not in _names(github)
    assert len(item.payload["remediation_threads"]) == 1


def test_comment_the_diff_cannot_place_stays_in_the_review_body_and_blocks(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A finding that is not on the diff is posted, kept, and handed to the agent."""
    github, ctx, item = _setup(make_ctx, make_work_item, [(0, 0, 0)])
    review = _review({"path": "elsewhere.py", "line": 40, "body": "this module is wrong"})

    outcome = _drive(PrReviewStage(), item, ctx, review)

    assert outcome == StageOutcome(Disposition.FAIL_BACK, "implementation_remediation")
    assert github.body_only_findings[1001][0]["body"] == "this module is wrong"
    assert item.payload["unaddressed_findings"][0]["path"] == "elsewhere.py"
    assert "mark_pr_implementation_go" not in _names(github)


def test_unanswered_open_thread_goes_to_implementation_without_a_new_review(
    make_ctx: Any, make_work_item: Any
) -> None:
    """An open thread is the agent's work: no review job runs first."""
    github, ctx, item = _setup(make_ctx, make_work_item, [(1, 0, 0)])

    outcome = _drive(PrReviewStage(), item, ctx, None)

    assert outcome == StageOutcome(Disposition.FAIL_BACK, "implementation_remediation")
    assert "mark_pr_implementation_no_go" in _names(github)
    assert "post_review" not in _names(github)


def test_unreadable_review_retries_then_fails_back_without_a_label(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A failed reviewer neither approves nor rejects the pull request."""
    github, ctx, item = _setup(make_ctx, make_work_item, [(0, 0, 0)])
    stage = PrReviewStage()

    outcomes = []
    for _ in range(4):
        item.state = "ENTER"
        outcomes.append(_drive(stage, item, ctx, _CRASH))

    assert [o.disposition for o in outcomes[:2]] == [Disposition.RETRY, Disposition.RETRY]
    assert outcomes[-1] == StageOutcome(Disposition.FAIL_BACK, "agent_error")
    assert not {"mark_pr_implementation_go", "mark_pr_implementation_no_go"} & set(_names(github))
