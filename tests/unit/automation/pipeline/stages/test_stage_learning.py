"""Behavior tests for the auxiliary learning stage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hephaestus.automation.arming_state import LearningJournalStore
from hephaestus.automation.pipeline.athena_skill_jobs import (
    AthenaSkillJob,
    AthenaSkillResult,
)
from hephaestus.automation.pipeline.jobs import JobResult
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.stages import (
    Continue,
    JobRequest,
    LearningStage,
    StageOutcome,
)
from hephaestus.automation.pipeline.stages.base import Disposition
from hephaestus.automation.pipeline.work_item import ItemResult, LearningIntent
from hephaestus.automation.review_journal import plan_fingerprint, render_current_plan
from hephaestus.automation.state_labels import STATE_PLAN_GO
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

_APPROVED_PLAN = "Use the approved plan."
_APPROVED_FINGERPRINT = plan_fingerprint(_APPROVED_PLAN)


def _approved_github(issue: int = 2705, revision: int = 8) -> FakeStageGitHub:
    github = FakeStageGitHub(labels=[STATE_PLAN_GO])
    github.comments[issue] = [render_current_plan(_APPROVED_PLAN, revision=revision)]
    return github


def test_learning_stage_owns_claim_and_submits_only_host_job(
    tmp_path: Path, make_ctx: Any, make_work_item: Any
) -> None:
    """The auxiliary stage claims durable work and emits only AthenaSkillJob."""
    assert LearningStage is not None
    journal = LearningJournalStore(lambda: tmp_path)
    ctx = make_ctx(
        learning_journal=journal,
        github=_approved_github(),
    )
    item = make_work_item(issue=2705, state="ENTER")
    item.learning_intents.append(
        LearningIntent.approved_plan(
            repo=item.repo,
            issue=2705,
            plan_revision=8,
            plan_fingerprint=_APPROVED_FINGERPRINT,
        )
    )
    item.learning_resume_stage = StageName.IMPLEMENTATION

    stage = LearningStage()
    assert stage.on_enter(item, ctx) is None
    entered = stage.step(item, ctx)
    assert entered == Continue(next_state="CLAIM")
    item.state = entered.next_state

    request = stage.step(item, ctx)

    assert isinstance(request, JobRequest)
    assert isinstance(request.job, AthenaSkillJob)
    assert request.job.request.kind == "learn"
    assert request.job.request.payload == {
        "issue_number": 2705,
        "learning_intent": item.learning_intents[0].to_payload(),
    }
    assert "learn_delivery" not in request.job.request.payload
    record = journal.load(item.learning_intents[0].key)
    assert record is not None and record["status"] == "claimed"


def test_restored_direct_scope_learning_uses_captured_bootstrap_revision(
    tmp_path: Path, make_ctx: Any, make_work_item: Any
) -> None:
    """A restored direct item retains enough revision evidence for learning."""
    revision = "a" * 40
    prepared: list[str] = []

    class SourceWorkspaces:
        def prepare(
            self,
            _item_number: int,
            _lane: Any,
            target: str,
            *,
            branch: str | None = None,
        ) -> Any:
            del branch
            prepared.append(target)
            return SimpleNamespace(cwd=tmp_path, revision=target)

    journal = LearningJournalStore(lambda: tmp_path)
    paths = SimpleNamespace(
        repo_root=tmp_path,
        worktree=tmp_path,
        source_workspaces=SourceWorkspaces(),
    )
    ctx = make_ctx(
        learning_journal=journal,
        github=_approved_github(),
        paths=paths,
    )
    original = make_work_item(issue=2705, state="ENTER")
    original.branch = "2705-auto-impl"
    original.payload["_direct_scope_base_sha"] = revision
    intent = LearningIntent.approved_plan(
        repo=original.repo,
        issue=2705,
        plan_revision=8,
        plan_fingerprint=_APPROVED_FINGERPRINT,
    )
    original.learning_intents.append(intent)
    original.learning_resume_stage = StageName.IMPLEMENTATION
    original.compact_for_post_processing(
        ItemResult(
            passed=False,
            reason="restore learning",
            final_stage=StageName.IMPLEMENTATION,
        )
    )
    record = original.learning_journal_identity(intent)
    item = make_work_item(issue=2705, state="ENTER")
    item.branch = original.branch
    item.learning_intents.append(intent)
    item.learning_resume_stage = StageName.IMPLEMENTATION
    assert item.restore_post_processing(record)
    stage = LearningStage()
    stage.on_enter(item, ctx)
    item.state = "CLAIM"

    request = stage.step(item, ctx)

    assert isinstance(request, JobRequest)
    assert prepared == [revision]


def _claimed_learning(
    tmp_path: Path, make_ctx: Any, make_work_item: Any, *, budget: int = 2
) -> tuple[Any, Any, Any, LearningJournalStore]:
    journal = LearningJournalStore(lambda: tmp_path)
    ctx = make_ctx(
        learning_journal=journal,
        budget_fn=lambda _name: budget,
        github=_approved_github(),
    )
    item = make_work_item(issue=2705, state="ENTER")
    item.learning_intents.append(
        LearningIntent.approved_plan(
            repo=item.repo,
            issue=2705,
            plan_revision=8,
            plan_fingerprint=_APPROVED_FINGERPRINT,
        )
    )
    item.learning_resume_stage = StageName.IMPLEMENTATION
    stage = LearningStage()
    stage.on_enter(item, ctx)
    item.state = "CLAIM"
    assert isinstance(stage.step(item, ctx), JobRequest)
    return stage, item, ctx, journal


def test_known_failure_retries_within_learning_budget(
    tmp_path: Path, make_ctx: Any, make_work_item: Any
) -> None:
    """A known failed host result returns the intent to pending once."""
    stage, item, ctx, journal = _claimed_learning(tmp_path, make_ctx, make_work_item)

    stage.on_job_done(item, JobResult(ok=False, error="host unavailable"), ctx)

    record = journal.load(item.learning_intents[0].key)
    assert record is not None
    assert record["status"] == "pending"
    assert record["attempts"] == 1
    assert item.payload.get("learning_failures") is None


def test_exhausted_learning_retry_is_ancillary_failure(
    tmp_path: Path, make_ctx: Any, make_work_item: Any
) -> None:
    """An exhausted learning failure does not fail the primary issue."""
    stage, item, ctx, journal = _claimed_learning(tmp_path, make_ctx, make_work_item, budget=1)

    stage.on_job_done(item, JobResult(ok=False, error="host unavailable"), ctx)
    item.state = "RESULT"
    assert stage.step(item, ctx) == Continue(next_state="CLAIM")
    item.state = "CLAIM"
    assert stage.step(item, ctx) == StageOutcome(Disposition.FAIL_BACK, "resume_implementation")
    record = journal.load(item.learning_intents[0].key)
    assert record is not None and record["status"] == "failed"
    assert item.payload["learning_failures"][0]["error"] == "host unavailable"


def test_valid_receipt_terminalizes_learning_success(
    tmp_path: Path, make_ctx: Any, make_work_item: Any
) -> None:
    """A PR readback receipt is the only successful terminal result."""
    stage, item, ctx, journal = _claimed_learning(tmp_path, make_ctx, make_work_item)
    sha = "a" * 40
    result = AthenaSkillResult(
        kind="learn",
        delivery_receipt={
            "pr_url": "https://github.com/HomericIntelligence/Mnemosyne/pull/1",
            "pr_number": 1,
            "commit_sha": sha,
            "readback_head_sha": sha,
        },
    )

    stage.on_job_done(item, JobResult(ok=True, value=result), ctx)

    record = journal.load(item.learning_intents[0].key)
    assert record is not None
    assert record["status"] == "succeeded"
    assert record["receipt_summary"]["pr_number"] == 1


def test_restart_does_not_repeat_inactive_unknown_claim(
    tmp_path: Path, make_ctx: Any, make_work_item: Any
) -> None:
    """A restart marks an ambiguous claim failed instead of repeating it."""
    stage, item, ctx, journal = _claimed_learning(tmp_path, make_ctx, make_work_item)
    journal._release_claim_lock(item.learning_intents[0].key)
    item.state = "CLAIM"

    assert stage.step(item, ctx) == Continue(next_state="CLAIM")
    record = journal.load(item.learning_intents[0].key)
    assert record is not None and record["status"] == "failed"
    assert item.payload["learning_failures"][0]["error"] == "outcome_unknown"


def test_live_claim_is_ejected_without_terminalizing_owner(
    tmp_path: Path, make_ctx: Any, make_work_item: Any
) -> None:
    """A second loop does not change a claim held by a live owner."""
    owner = LearningJournalStore(lambda: tmp_path)
    observer = LearningJournalStore(lambda: tmp_path)
    ctx = make_ctx(
        learning_journal=observer,
        github=_approved_github(),
    )
    item = make_work_item(issue=2705, state="CLAIM")
    intent = LearningIntent.approved_plan(
        repo=item.repo,
        issue=2705,
        plan_revision=8,
        plan_fingerprint="abc",
    )
    item.learning_intents.append(intent)
    item.learning_resume_stage = StageName.IMPLEMENTATION
    owner.ensure_pending(intent.key, kind=intent.kind.value, identity=intent.journal_identity())
    assert owner.claim(intent.key)

    stage = LearningStage()
    assert stage.step(item, ctx) == StageOutcome(
        Disposition.EJECT,
        "learning_claim_owned_elsewhere",
    )

    record = observer.load(intent.key)
    assert record is not None and record["status"] == "claimed"
    owner.finish(intent.key, succeeded=True)


def test_completion_is_bound_to_the_locally_submitted_intent(
    tmp_path: Path, make_ctx: Any, make_work_item: Any
) -> None:
    """A local completion cannot terminalize another process's live claim."""
    owner = LearningJournalStore(lambda: tmp_path)
    observer = LearningJournalStore(lambda: tmp_path)
    ctx = make_ctx(learning_journal=observer, budget_fn=lambda _name: 1)
    item = make_work_item(issue=2705, state="CLAIM")
    external = LearningIntent.post_merge(repo=item.repo, issue=2705, pr=1)
    local = LearningIntent.post_merge(repo=item.repo, issue=2705, pr=2)
    item.learning_intents.extend([external, local])
    item.learning_resume_stage = StageName.FINISHED
    for intent in item.learning_intents:
        observer.ensure_pending(
            intent.key,
            kind=intent.kind.value,
            identity=intent.journal_identity(),
        )
    assert owner.claim(external.key)
    item.payload["learning_external_claims"] = [external.key]

    stage = LearningStage()
    request = stage.step(item, ctx)
    assert isinstance(request, JobRequest)
    assert request.job.request.payload["learning_intent"]["intent_key"] == local.key
    sha = "a" * 40
    stage.on_job_done(
        item,
        JobResult(
            ok=True,
            value=AthenaSkillResult(
                kind="learn",
                delivery_receipt={
                    "pr_url": "https://github.com/HomericIntelligence/Mnemosyne/pull/1",
                    "pr_number": 1,
                    "commit_sha": sha,
                    "readback_head_sha": sha,
                },
            ),
        ),
        ctx,
    )

    external_record = observer.load(external.key)
    local_record = observer.load(local.key)
    assert external_record is not None and external_record["status"] == "claimed"
    assert local_record is not None and local_record["status"] == "succeeded"
    owner.finish(external.key, succeeded=True)


def test_cancellation_before_host_start_returns_claim_to_pending(
    tmp_path: Path, make_ctx: Any, make_work_item: Any
) -> None:
    """A proven pre-start cancellation remains safe to retry after restart."""
    stage, item, ctx, journal = _claimed_learning(tmp_path, make_ctx, make_work_item)

    stage.on_cancelled_before_start(item, ctx)

    record = journal.load(item.learning_intents[0].key)
    assert record is not None
    assert record["status"] == "pending"
    assert record["error"] == "interrupted_before_start"


def test_cleanup_barrier_waits_for_every_intent(
    tmp_path: Path, make_ctx: Any, make_work_item: Any
) -> None:
    """The stage does not advance until every associated intent is terminal."""
    journal = LearningJournalStore(lambda: tmp_path)
    ctx = make_ctx(
        learning_journal=journal,
        budget_fn=lambda _name: 1,
        github=_approved_github(),
    )
    item = make_work_item(issue=2705, state="ENTER")
    item.learning_intents.extend(
        [
            LearningIntent.approved_plan(
                repo=item.repo,
                issue=2705,
                plan_revision=8,
                plan_fingerprint=_APPROVED_FINGERPRINT,
            ),
            LearningIntent.post_merge(repo=item.repo, issue=2705, pr=99),
        ]
    )
    item.learning_resume_stage = StageName.FINISHED
    stage = LearningStage()
    stage.on_enter(item, ctx)
    item.state = "CLAIM"
    first = stage.step(item, ctx)
    assert isinstance(first, JobRequest)
    stage.on_job_done(item, JobResult(ok=False, error="first failed"), ctx)

    item.state = "CLAIM"
    second = stage.step(item, ctx)
    assert isinstance(second, JobRequest)
    assert isinstance(second.job, AthenaSkillJob)
    assert second.job.request.payload["learning_intent"]["kind"] == "post_merge"
    stage.on_job_done(item, JobResult(ok=False, error="second failed"), ctx)

    item.state = "CLAIM"
    assert stage.step(item, ctx) == StageOutcome(Disposition.ADVANCE, "learning terminal")


def test_stale_plan_authority_skips_host_and_returns_to_review(
    tmp_path: Path, make_ctx: Any, make_work_item: Any
) -> None:
    """A removed plan-GO label prevents stale learning and implementation."""
    journal = LearningJournalStore(lambda: tmp_path)
    ctx = make_ctx(learning_journal=journal, github=FakeStageGitHub())
    item = make_work_item(issue=2705, state="ENTER")
    intent = LearningIntent.approved_plan(
        repo=item.repo, issue=2705, plan_revision=8, plan_fingerprint="abc"
    )
    item.learning_intents.append(intent)
    item.learning_resume_stage = StageName.IMPLEMENTATION
    stage = LearningStage()
    stage.on_enter(item, ctx)

    item.state = "CLAIM"
    assert stage.step(item, ctx) == Continue(next_state="CLAIM")
    record = journal.load(intent.key)
    assert record is not None
    assert record["status"] == "failed"
    assert record["error"] == "plan_state_changed"

    assert stage.step(item, ctx) == StageOutcome(Disposition.FAIL_BACK, "resume_plan_review")


def test_changed_plan_revision_invalidates_old_learning_intent(
    tmp_path: Path, make_ctx: Any, make_work_item: Any
) -> None:
    """A GO label cannot approve learning for a replaced plan revision."""
    current_plan = render_current_plan("Use the current plan.", revision=9)
    github = FakeStageGitHub(labels=[STATE_PLAN_GO])
    github.comments[2705] = [current_plan]
    journal = LearningJournalStore(lambda: tmp_path)
    ctx = make_ctx(learning_journal=journal, github=github)
    item = make_work_item(issue=2705, state="ENTER")
    intent = LearningIntent.approved_plan(
        repo=item.repo,
        issue=2705,
        plan_revision=8,
        plan_fingerprint=plan_fingerprint("Use the old plan."),
    )
    item.learning_intents.append(intent)
    item.learning_resume_stage = StageName.IMPLEMENTATION
    stage = LearningStage()
    stage.on_enter(item, ctx)
    item.state = "CLAIM"

    assert stage.step(item, ctx) == Continue(next_state="CLAIM")
    record = journal.load(intent.key)
    assert record is not None and record["error"] == "plan_state_changed"
    assert item.learning_resume_stage is StageName.PLAN_REVIEW


def test_unavailable_plan_read_is_ancillary_and_does_not_block_implementation(
    tmp_path: Path, make_ctx: Any, make_work_item: Any
) -> None:
    """A failed authority read skips host delivery but keeps the primary route."""

    class UnavailableGitHub(FakeStageGitHub):
        def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
            raise RuntimeError("GitHub unavailable")

    journal = LearningJournalStore(lambda: tmp_path)
    ctx = make_ctx(learning_journal=journal, github=UnavailableGitHub())
    item = make_work_item(issue=2705, state="ENTER")
    intent = LearningIntent.approved_plan(
        repo=item.repo, issue=2705, plan_revision=8, plan_fingerprint="abc"
    )
    item.learning_intents.append(intent)
    item.learning_resume_stage = StageName.IMPLEMENTATION
    stage = LearningStage()
    stage.on_enter(item, ctx)

    item.state = "CLAIM"
    assert stage.step(item, ctx) == Continue(next_state="CLAIM")
    record = journal.load(intent.key)
    assert record is not None
    assert record["status"] == "failed"
    assert record["error"] == "plan_state_unverified"
    assert stage.step(item, ctx) == StageOutcome(Disposition.FAIL_BACK, "resume_implementation")
