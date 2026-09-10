"""Test current plan identity and bounded queue retries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.learning_journal import LearningJournalStore
from hephaestus.automation.pipeline.jobs import JobResult
from hephaestus.automation.pipeline.routing import Disposition, StageOutcome
from hephaestus.automation.pipeline.stage_results import Continue
from hephaestus.automation.pipeline.stages.plan_review import PlanReviewStage
from hephaestus.automation.pipeline.stages.planning import PlanningStage
from hephaestus.automation.plan_review_session import PlanReviewSessionStore
from hephaestus.automation.review_journal import (
    PlanDiscoveryResult,
    render_current_plan,
    render_pending_review,
)
from hephaestus.automation.review_types import ReviewVerdict
from hephaestus.automation.state_labels import STATE_NEEDS_PLAN, STATE_PLAN_GO
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _seed_plan(github: FakeStageGitHub, issue: int, plan: str, revision: int = 1) -> None:
    """Store a current plan and its pending review."""
    github.comments[issue] = [
        render_current_plan(plan, revision=revision),
        render_pending_review(revision=revision),
    ]


def test_published_plan_must_still_exist_at_handoff(
    make_ctx: Any, make_work_item: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deleted publication cannot advance to plan review."""
    github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
    lookups = iter([PlanDiscoveryResult.absent(), PlanDiscoveryResult.absent()])
    monkeypatch.setattr(github, "discover_plan", lambda _issue: next(lookups))
    item = make_work_item(issue=501, state="VERIFY", payload={"plan_text": "Candidate plan"})

    outcome = PlanningStage().step(item, make_ctx(github=github))

    assert outcome == StageOutcome(Disposition.RETRY, "plan disappeared before verification")


def test_second_plan_lookup_has_the_same_failure_bound(
    make_ctx: Any, make_work_item: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated errors on the final lookup stop after the plan budget."""
    github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
    lookups = iter(
        [
            PlanDiscoveryResult.found(render_current_plan("Current plan")),
            PlanDiscoveryResult.read_error("read unavailable"),
        ]
        * 2
    )
    monkeypatch.setattr(github, "discover_plan", lambda _issue: next(lookups))
    item = make_work_item(issue=502, state="VERIFY")
    ctx = make_ctx(github=github, budget_fn=lambda _name: 2)
    stage = PlanningStage()

    first = stage.step(item, ctx)
    item.state = "VERIFY"
    second = stage.step(item, ctx)

    assert isinstance(first, StageOutcome)
    assert isinstance(second, StageOutcome)
    assert first.disposition is Disposition.RETRY
    assert second.disposition is Disposition.FINISH_FAIL
    assert item.attempts["plan"] == 2


def test_verdict_publication_retry_counts_one_review(make_ctx: Any, make_work_item: Any) -> None:
    """A repeated label write does not consume another review round."""

    class DelayedLabels(FakeStageGitHub):
        dropped = False

        def edit_labels(self, issue_number: int, *, add: list[str], remove: list[str]) -> None:
            if not self.dropped:
                self.dropped = True
                return
            super().edit_labels(issue_number, add=add, remove=remove)

    github = DelayedLabels(labels=[STATE_NEEDS_PLAN])
    _seed_plan(github, 503, "Current plan")
    ctx = make_ctx(github=github, config_overrides={"enable_learn": False})
    item = make_work_item(issue=503, state="ENTER")
    stage = PlanReviewStage()
    assert stage.on_enter(item, ctx) is None
    item.state = "REVIEW_WAIT"
    stage.on_job_done(
        item,
        JobResult(ok=True, value=ReviewVerdict(None, "NOGO", "Add tests\n\nstate:plan-no-go")),
        ctx,
    )
    item.state = "EVAL"

    first = stage.step(item, ctx)
    second = stage.step(item, ctx)

    assert isinstance(first, StageOutcome)
    assert first.disposition is Disposition.RETRY
    assert second == Continue(next_state="AMEND_WAIT")
    assert item.payload["review_round"] == 1
    assert item.attempts["plan_review_iter"] == 1
    assert (
        len([entry for entry in github.mutation_log if entry[0] == "gh_issue_upsert_comment"]) == 1
    )


@pytest.mark.parametrize("revision", [1, 2], ids=["content-change", "new-revision"])
def test_changed_plan_cannot_receive_an_old_verdict(
    make_ctx: Any, make_work_item: Any, revision: int
) -> None:
    """Only the plan that the reviewer examined can receive GO."""
    github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
    _seed_plan(github, 504, "Reviewed plan")
    ctx = make_ctx(github=github, config_overrides={"enable_learn": False})
    item = make_work_item(issue=504, state="ENTER")
    stage = PlanReviewStage()
    assert stage.on_enter(item, ctx) is None
    item.state = "REVIEW_WAIT"
    stage.on_job_done(
        item,
        JobResult(ok=True, value=ReviewVerdict(None, "GO", "Ready\n\nstate:plan-go")),
        ctx,
    )
    _seed_plan(github, 504, "Changed plan", revision=revision)
    item.state = "EVAL"

    outcome = stage.step(item, ctx)

    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.FAIL_BACK
    assert STATE_PLAN_GO not in github.labels[504]
    assert github.mutation_log == []


@pytest.mark.parametrize("change_at", ["audit", "label"])
@pytest.mark.parametrize("enable_learn", [False, True])
def test_plan_change_during_publication_cannot_advance(
    tmp_path: Path,
    make_ctx: Any,
    make_work_item: Any,
    change_at: str,
    enable_learn: bool,
) -> None:
    """A changed plan cannot advance or create a learning intent."""

    class ConcurrentPlan(FakeStageGitHub):
        changed = False

        def upsert_issue_comment(
            self,
            issue_number: int,
            marker: str,
            body: str,
            *,
            legacy_marker: str | None = None,
        ) -> None:
            super().upsert_issue_comment(issue_number, marker, body, legacy_marker=legacy_marker)
            if change_at == "audit" and not self.changed:
                self.changed = True
                _seed_plan(self, issue_number, "Changed plan", revision=2)

        def edit_labels(self, issue_number: int, *, add: list[str], remove: list[str]) -> None:
            super().edit_labels(issue_number, add=add, remove=remove)
            if change_at == "label" and STATE_PLAN_GO in add and not self.changed:
                self.changed = True
                _seed_plan(self, issue_number, "Changed plan", revision=2)

    github = ConcurrentPlan(labels=[STATE_NEEDS_PLAN])
    _seed_plan(github, 508, "Reviewed plan")
    journal = LearningJournalStore(lambda: tmp_path)
    ctx = make_ctx(
        github=github,
        config_overrides={"enable_learn": enable_learn},
        learning_journal=journal,
    )
    item = make_work_item(issue=508, state="ENTER")
    stage = PlanReviewStage()
    assert stage.on_enter(item, ctx) is None
    item.state = "REVIEW_WAIT"
    stage.on_job_done(
        item,
        JobResult(ok=True, value=ReviewVerdict(None, "GO", "Ready\n\nstate:plan-go")),
        ctx,
    )
    item.state = "EVAL"

    outcome = stage.step(item, ctx)

    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.FAIL_BACK
    assert github.labels[508] == {STATE_NEEDS_PLAN}
    assert item.learning_intents == []
    assert list(tmp_path.glob("learning-intent-*.json")) == []


def test_session_replacement_preserves_completed_rounds(
    make_ctx: Any, make_work_item: Any, tmp_path: Path
) -> None:
    """A replacement conversation retains the logical review budget."""
    github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
    _seed_plan(github, 505, "Current plan")
    store = PlanReviewSessionStore(lambda: tmp_path)
    ctx = make_ctx(github=github, plan_review_sessions=store)
    item = make_work_item(issue=505, state="ENTER")
    stage = PlanReviewStage()
    assert stage.on_enter(item, ctx) is None
    item.payload["review_round"] = 2
    item.state = "REVIEW_WAIT"
    stage.on_job_done(item, JobResult(ok=False, session_lost=True), ctx)

    outcome = stage.step(item, ctx)

    assert outcome == Continue(next_state="REVIEW_WAIT")
    assert item.payload["review_round"] == 2


def test_third_consecutive_session_failure_stops_replacement(
    make_ctx: Any, make_work_item: Any, tmp_path: Path
) -> None:
    """Two replacement attempts use the existing reviewer failure cap."""
    github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
    _seed_plan(github, 506, "Current plan")
    store = PlanReviewSessionStore(lambda: tmp_path)
    ctx = make_ctx(github=github, plan_review_sessions=store)
    item = make_work_item(issue=506, state="ENTER")
    stage = PlanReviewStage()
    assert stage.on_enter(item, ctx) is None
    outcomes = []

    for _attempt in range(3):
        item.state = "REVIEW_WAIT"
        stage.on_job_done(item, JobResult(ok=False, session_lost=True), ctx)
        outcomes.append(stage.step(item, ctx))

    assert outcomes[:2] == [Continue(next_state="REVIEW_WAIT")] * 2
    assert isinstance(outcomes[2], StageOutcome)
    assert outcomes[2].disposition is Disposition.FINISH_FAIL
    assert item.payload["review_error_retries"] == 3
    assert github.mutation_log == []


def test_missing_verdict_retry_submits_another_review(make_ctx: Any, make_work_item: Any) -> None:
    """A reviewer failure retries the job instead of the empty verdict."""
    github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
    _seed_plan(github, 507, "Current plan")
    ctx = make_ctx(github=github)
    item = make_work_item(issue=507, state="ENTER")
    stage = PlanReviewStage()
    assert stage.on_enter(item, ctx) is None
    item.state = "REVIEW_WAIT"
    stage.on_job_done(item, JobResult(ok=True), ctx)
    item.state = "EVAL"

    outcome = stage.step(item, ctx)

    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.RETRY
    assert item.state == "REVIEW_WAIT"
    assert item.payload["review_error_retries"] == 1
