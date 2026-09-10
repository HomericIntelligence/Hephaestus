"""Keep accepted plan reviews across bounded publication failures."""

from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.jobs import AgentJob, JobResult
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.stages.plan_review import PlanReviewStage
from hephaestus.automation.review_journal import (
    CommentJournalReadError,
    IssueComment,
    render_current_plan,
    render_pending_review,
)
from hephaestus.automation.review_types import ReviewVerdict
from hephaestus.automation.state_labels import STATE_NEEDS_PLAN, STATE_PLAN_GO
from tests.unit.automation.pipeline.conftest import (
    FakeWorkerPool,
    claim_test_item,
    fake_worker_factories,
)
from tests.unit.automation.pipeline.stages.conftest import (
    FakeSourceWorkspaceManager,
    FakeStageGitHub,
)


class PublicationFailure(FakeStageGitHub):
    """Fail at a service boundary after the reviewer has completed."""

    def __init__(self, point: str, failures: int = 1) -> None:
        super().__init__(labels=[STATE_NEEDS_PLAN])
        self.point = point
        self.remaining = failures
        self.armed = False
        self.audit_written = False
        self.label_written = False

    def fail(self, point: str) -> None:
        """Raise a transport failure at the selected boundary."""
        if self.armed and self.point == point and self.remaining:
            self.remaining -= 1
            raise CommentJournalReadError("temporary publication transport failure")

    def upsert_issue_comment(
        self, issue_number: int, marker: str, body: str, *, legacy_marker: str | None = None
    ) -> None:
        self.fail("comment")
        super().upsert_issue_comment(issue_number, marker, body, legacy_marker=legacy_marker)
        self.audit_written = True
        self.fail("unknown-comment-outcome")

    def edit_labels(self, issue_number: int, *, add: list[str], remove: list[str]) -> None:
        self.fail("label")
        super().edit_labels(issue_number, add=add, remove=remove)
        self.label_written = True

    def issue_comments(self, issue_number: int) -> list[IssueComment]:
        if self.audit_written:
            self.fail("identity-readback")
        return super().issue_comments(issue_number)

    def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
        if self.label_written:
            self.fail("label-readback")
        return super().gh_issue_json(issue_number)


@pytest.mark.parametrize(
    "point", ["comment", "unknown-comment-outcome", "label", "identity-readback", "label-readback"]
)
@pytest.mark.parametrize("failures", [1, 3], ids=["recovers", "bounded-failure"])
def test_coordinator_retries_one_accepted_review(
    tmp_path: Path, make_work_item: Any, point: str, failures: int
) -> None:
    """Publication retry keeps one verdict and does not invoke the reviewer again."""
    github = PublicationFailure(point, failures)
    github.comments[503] = [
        render_current_plan("Current plan", revision=1),
        render_pending_review(revision=1),
    ]
    pool = FakeWorkerPool()
    pool.script(JobResult(ok=True, value=ReviewVerdict(None, "GO", "Ready\n\nstate:plan-go")))
    clock = [100.0]
    stage = PlanReviewStage()
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["test-repo"],
            projects_dir=tmp_path,
            rate_guard_enabled=False,
            no_advise=True,
            enable_learn=False,
        ),
        github=github,
        stages={StageName.PLAN_REVIEW: stage},
        **fake_worker_factories(pool),
        install_signals=False,
        monotonic=lambda: clock[0],
    )
    item = make_work_item(issue=503, stage=StageName.PLAN_REVIEW)
    ctx = coordinator._ctx_for(item)
    ctx.paths.source_workspaces = FakeSourceWorkspaceManager(tmp_path, item.repo)
    assert stage.on_enter(item, ctx) is None
    item.state = "REVIEW_WAIT"
    claim_test_item(coordinator, item)
    coordinator._run_item(item)
    assert len(pool.submitted) == 1
    github.armed = True
    coordinator._drain_completions()

    assert item.stage is StageName.PLAN_REVIEW
    assert item.state == "EVAL"
    assert len(coordinator.timers) == 1
    assert item.payload["review_round"] == 1
    for _ in range(failures):
        clock[0] += 100.0
        coordinator._wake_timers()
        if item.stage is StageName.PLAN_REVIEW:
            claim_test_item(coordinator, item)
            coordinator._run_item(item)
        if item.stage is not StageName.PLAN_REVIEW:
            break

    assert len([h for h in pool.submitted if isinstance(h.job, AgentJob)]) == 1
    assert item.payload["review_round"] == 1
    assert item.attempts["plan_review_iter"] == 1
    if failures == 1:
        assert item.stage is StageName.IMPLEMENTATION
        assert STATE_PLAN_GO in github.labels[503]
    else:
        assert item.stage is StageName.FINISHED
        assert item.result is not None and not item.result.passed
        assert "publication" in item.result.reason
        assert not coordinator.timers
