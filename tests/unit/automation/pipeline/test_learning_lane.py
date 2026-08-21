"""Coordinator behavior tests for the independent learning lane."""

from __future__ import annotations

import inspect
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest

import hephaestus.agents.runtime as agent_runtime
from hephaestus.automation.pipeline.athena_skill_jobs import (
    AthenaSkillJob,
    AthenaSkillRequest,
    AthenaSkillResult,
)
from hephaestus.automation.pipeline.auxiliary_worker_pool import AuxiliaryWorkerPool
from hephaestus.automation.pipeline.coordinator import Coordinator, PipelineConfig
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.jobs import GitJob, JobHandle
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.work_item import ItemKind, ItemResult, LearningIntent, WorkItem
from hephaestus.automation.review_journal import plan_fingerprint, render_current_plan
from hephaestus.automation.state_labels import STATE_PLAN_GO
from hephaestus.utils import subprocess_registry
from hephaestus.utils.helpers import run_subprocess
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def test_coordinator_exposes_independent_learning_capacity(tmp_path: Path) -> None:
    """Learning capacity is configured separately from the main work window."""
    assert "auxiliary_pool" in inspect.signature(Coordinator).parameters
    config = PipelineConfig(
        org="org",
        repos=["repo"],
        max_workers=1,
        learning_workers=2,
        learning_queue_capacity=3,
        projects_dir=tmp_path,
    )
    assert config.learning_workers == 2
    assert config.learning_queue_capacity == 3


def test_single_main_worker_progresses_while_learning_is_queued(tmp_path: Path) -> None:
    """A learning handoff releases the only main permit for unrelated work."""
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            max_workers=1,
            learning_workers=1,
            learning_queue_capacity=1,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(labels=[STATE_PLAN_GO]),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    learning = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, stage=StageName.PLAN_REVIEW)
    unrelated = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=2, stage=StageName.PLANNING)
    assert coordinator._push_item(learning, StageName.PLAN_REVIEW, enter=True)
    assert coordinator._claim_item(StageName.PLAN_REVIEW) is learning

    assert coordinator._handoff_item(learning, StageName.LEARNING, enter=True)

    assert coordinator.live_work_count == 0
    assert coordinator.learning_work_count == 1
    assert coordinator._push_item(unrelated, StageName.PLANNING, enter=True)


def test_learning_cleanup_does_not_wait_for_main_capacity(tmp_path: Path) -> None:
    """The learning-to-finished handoff keeps its auxiliary permit."""
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            max_workers=1,
            learning_workers=1,
            learning_queue_capacity=1,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        auxiliary_pool=FakeWorkerPool(),
        install_signals=False,
    )
    learning = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, stage=StageName.LEARNING)
    unrelated = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=2, stage=StageName.PLANNING)
    assert coordinator._push_item(learning, StageName.LEARNING, enter=True)
    assert coordinator._push_item(unrelated, StageName.PLANNING, enter=True)
    assert coordinator._claim_item(StageName.LEARNING) is learning

    assert coordinator._handoff_item(learning, StageName.FINISHED, enter=True)

    main_handle = JobHandle(
        job=GitJob(repo="repo", op="push", timeout_s=1),
        on_done_state="DONE",
    )
    coordinator.in_flight[main_handle] = unrelated
    coordinator.inflight_per_repo["repo"] = 1
    assert coordinator.live_work_count == 1
    assert coordinator.learning_work_count == 1
    assert len(coordinator.queues[StageName.FINISHED]) == 1
    assert coordinator._admit(learning)


def test_opposite_lane_handoffs_do_not_deadlock_at_capacity_one(tmp_path: Path) -> None:
    """An auxiliary return lets a main item enter the full auxiliary lane."""
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            max_workers=1,
            learning_workers=1,
            learning_queue_capacity=1,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        auxiliary_pool=FakeWorkerPool(),
        install_signals=False,
    )
    returning = WorkItem(
        repo="repo",
        kind=ItemKind.ISSUE,
        issue=1,
        stage=StageName.LEARNING,
    )
    entering = WorkItem(
        repo="repo",
        kind=ItemKind.ISSUE,
        issue=2,
        stage=StageName.PLAN_REVIEW,
    )
    assert coordinator._push_item(returning, StageName.LEARNING, enter=True)
    assert coordinator._push_item(entering, StageName.PLAN_REVIEW, enter=True)
    assert coordinator._claim_item(StageName.LEARNING) is returning
    assert coordinator._claim_item(StageName.PLAN_REVIEW) is entering

    assert not coordinator._handoff_item(returning, StageName.IMPLEMENTATION, enter=True)
    assert coordinator.learning_work_count == 1
    assert not coordinator._handoff_item(entering, StageName.LEARNING, enter=True)
    assert coordinator.live_work_count == 1

    coordinator._drain_pending_handoffs()

    assert returning.stage is StageName.IMPLEMENTATION
    assert entering.stage is StageName.LEARNING
    assert id(returning) not in coordinator._pending_handoffs
    assert id(entering) not in coordinator._pending_handoffs
    assert coordinator.live_work_count == 1
    assert coordinator.learning_work_count == 1


def test_exact_stage_opposite_lane_handoffs_exchange_at_capacity_one(tmp_path: Path) -> None:
    """Full source queues exchange complementary leased items without a spill."""
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            max_workers=1,
            learning_workers=1,
            learning_queue_capacity=1,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        auxiliary_pool=FakeWorkerPool(),
        install_signals=False,
    )
    returning = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, stage=StageName.LEARNING)
    entering = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=2, stage=StageName.PLAN_REVIEW)
    assert coordinator._push_item(returning, StageName.LEARNING, enter=True)
    assert coordinator._push_item(entering, StageName.PLAN_REVIEW, enter=True)
    assert coordinator._claim_item(StageName.LEARNING) is returning
    assert coordinator._claim_item(StageName.PLAN_REVIEW) is entering

    assert not coordinator._handoff_item(returning, StageName.PLAN_REVIEW, enter=True)
    assert not coordinator._handoff_item(entering, StageName.LEARNING, enter=True)

    coordinator._drain_pending_handoffs()

    assert coordinator.queues[StageName.PLAN_REVIEW].snapshot() == [returning]
    assert coordinator.queues[StageName.LEARNING].snapshot() == [entering]
    assert not coordinator._leases
    assert not coordinator._pending_handoffs
    assert coordinator.live_work_count == coordinator.learning_work_count == 1


def test_repeated_opposite_lane_recovery_stays_bounded_by_active_leases(tmp_path: Path) -> None:
    """Repeated capacity-one exchanges never create permitless pending work."""
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            max_workers=1,
            learning_workers=1,
            learning_queue_capacity=1,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        auxiliary_pool=FakeWorkerPool(),
        install_signals=False,
    )
    main = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, stage=StageName.PLANNING)
    auxiliary = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=2, stage=StageName.LEARNING)
    assert coordinator._push_item(main, StageName.PLANNING, enter=True)
    assert coordinator._push_item(auxiliary, StageName.LEARNING, enter=True)

    for cycle in range(3):
        assert coordinator._claim_item(main.stage) is main
        assert coordinator._claim_item(auxiliary.stage) is auxiliary
        auxiliary_target = StageName.IMPLEMENTATION if cycle % 2 == 0 else StageName.PLANNING
        assert not coordinator._handoff_item(auxiliary, auxiliary_target, enter=True)
        assert not coordinator._handoff_item(main, StageName.LEARNING, enter=True)
        assert len(coordinator._pending_handoffs) == len(coordinator._leases) == 2
        assert coordinator.live_work_count == coordinator.learning_work_count == 1

        coordinator._drain_pending_handoffs()

        assert not coordinator._pending_handoffs
        assert not coordinator._leases
        assert coordinator.live_work_count == coordinator.learning_work_count == 1
        main, auxiliary = auxiliary, main


def test_scoped_plan_learning_resumes_at_scoped_sink(tmp_path: Path) -> None:
    """A planning-only run does not escape its selected stage scope."""
    from hephaestus.automation.pipeline.routing import PipelineScope

    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            projects_dir=tmp_path,
            scope=PipelineScope(frozenset({StageName.PLANNING, StageName.PLAN_REVIEW})),
        ),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, stage=StageName.PLAN_REVIEW)
    item.learning_intents.append(
        LearningIntent.approved_plan(
            repo="repo",
            issue=1,
            plan_revision=1,
            plan_fingerprint="abc",
        )
    )

    coordinator._route(item, StageOutcome(Disposition.ADVANCE, "plan approved"))

    assert item.stage is StageName.LEARNING
    assert item.learning_resume_stage is StageName.FINISHED

    coordinator._route(item, StageOutcome(Disposition.ADVANCE, "learning terminal"))

    assert item.stage is StageName.FINISHED
    assert item.result is not None
    assert item.result.passed


def test_restart_reconstructs_pending_intent_before_primary_stage(tmp_path: Path) -> None:
    """A new coordinator routes a durable pending claim back to learning."""
    repo_root = tmp_path / "repo"
    github = FakeStageGitHub(pr_state={"state": "MERGED"})
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            projects_dir=tmp_path,
            repo_roots={"repo": repo_root},
        ),
        github=github,
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    approved_plan = "Use the approved plan."
    github.comments[1] = [render_current_plan(approved_plan, revision=1)]
    intent = LearningIntent.post_merge(repo="repo", issue=2705, pr=12)
    journal = coordinator._ctx_for_repo("repo").learning_journal
    journal.ensure_pending(intent.key, kind=intent.kind.value, identity=intent.journal_identity())
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=2705, stage=StageName.FINISHED)

    coordinator._restore_learning_intents(item, StageName.FINISHED, "already merged")

    assert item.stage is StageName.LEARNING
    assert item.learning_intents == [intent]
    assert item.learning_resume_stage is StageName.FINISHED


def test_restart_restores_post_merge_cleanup_obligation(tmp_path: Path) -> None:
    """A new item recovers the branch, worktree, and confirmed merge result."""
    repo_root = tmp_path / "repo"
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            projects_dir=tmp_path,
            repo_roots={"repo": repo_root},
        ),
        github=FakeStageGitHub(pr_state={"state": "MERGED"}),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    original = WorkItem(
        repo="repo",
        kind=ItemKind.ISSUE,
        issue=2705,
        pr=12,
        stage=StageName.MERGE_WAIT,
        worktree="build/.worktrees/issue-2705",
        branch="2705-fix",
    )
    original.payload["_direct_scope_local_branch_cleanup"] = True
    intent = LearningIntent.post_merge(repo="repo", issue=2705, pr=12)
    original.learning_intents.append(intent)
    original.compact_for_post_processing(
        ItemResult(passed=True, reason="merged", final_stage=StageName.MERGE_WAIT)
    )
    journal = coordinator._ctx_for_repo("repo").learning_journal
    journal.ensure_pending(
        intent.key,
        kind=intent.kind.value,
        identity=original.learning_journal_identity(intent),
    )
    recovered = WorkItem(
        repo="repo",
        kind=ItemKind.ISSUE,
        issue=2705,
        pr=12,
        stage=StageName.FINISHED,
    )

    coordinator._restore_learning_intents(recovered, StageName.FINISHED, "already merged")

    assert recovered.worktree == original.worktree
    assert recovered.branch == original.branch
    assert recovered.post_processing is not None
    assert recovered.post_processing.result.passed
    assert recovered.payload["_direct_scope_local_branch_cleanup"] is True


def test_post_merge_recovery_rejects_non_boolean_result() -> None:
    """Recovery rejects text that could invert a stored merge result."""
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=2705)
    record = {
        "post_processing": {
            "worktree": "",
            "branch": "",
            "resume_stage": StageName.FINISHED.value,
            "cleanup_payload": {},
            "result": {
                "passed": "false",
                "reason": "failed",
                "final_stage": StageName.MERGE_WAIT.value,
            },
        }
    }

    with pytest.raises(ValueError, match="invalid result fields"):
        item.restore_post_processing(record)


def test_malformed_recovery_record_does_not_poison_source_item(tmp_path: Path) -> None:
    """A journal object without a key is reported and left untouched."""
    repo_root = tmp_path / "repo"
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            projects_dir=tmp_path,
            repo_roots={"repo": repo_root},
        ),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    journal = coordinator._ctx_for_repo("repo").learning_journal
    malformed = journal.path("unknown")
    malformed.parent.mkdir(parents=True, exist_ok=True)
    malformed.write_text(
        json.dumps(
            {
                "repo": "repo",
                "issue": 2705,
                "status": "pending",
                "kind": "post_merge",
            }
        ),
        encoding="utf-8",
    )
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=2705, stage=StageName.PLANNING)

    coordinator._restore_learning_intents(item, StageName.PLANNING, "primary")

    assert item.stage is StageName.PLANNING
    assert malformed.exists()


def test_merged_legacy_item_reconstructs_missing_post_merge_intent(tmp_path: Path) -> None:
    """A merged item without a new journal record enters learning once."""
    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo"], projects_dir=tmp_path),
        github=FakeStageGitHub(pr_state={"state": "MERGED"}),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    item = WorkItem(
        repo="repo",
        kind=ItemKind.ISSUE,
        issue=2705,
        pr=12,
        stage=StageName.FINISHED,
    )

    coordinator._restore_learning_intents(item, StageName.FINISHED, "already merged")

    intent = LearningIntent.post_merge(repo="repo", issue=2705, pr=12)
    assert item.stage is StageName.LEARNING
    assert item.learning_intents == [intent]
    record = coordinator._ctx_for_repo("repo").learning_journal.load(intent.key)
    assert record is not None and record["status"] == "pending"


def test_merged_discovery_skips_terminal_learning_intent(tmp_path: Path) -> None:
    """Repeated discovery does not rebuild a completed auxiliary detour."""
    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo"], projects_dir=tmp_path),
        github=FakeStageGitHub(pr_state={"state": "MERGED"}),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    intent = LearningIntent.post_merge(repo="repo", issue=2705, pr=12)
    journal = coordinator._ctx_for_repo("repo").learning_journal
    journal.ensure_pending(intent.key, kind=intent.kind.value, identity=intent.journal_identity())
    assert journal.claim(intent.key)
    journal.finish(intent.key, succeeded=True)
    item = WorkItem(
        repo="repo",
        kind=ItemKind.ISSUE,
        issue=2705,
        pr=12,
        stage=StageName.FINISHED,
    )

    coordinator._restore_learning_intents(item, StageName.FINISHED, "already merged")

    assert item.stage is StageName.FINISHED
    assert item.learning_intents == []


def test_restart_restores_cleanup_after_learning_became_terminal(tmp_path: Path) -> None:
    """Restart retains cleanup after learn succeeds but before Finished completes."""
    repo_root = tmp_path / "repo"
    worktree = repo_root / "build" / ".worktrees" / "issue-2705"
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            projects_dir=tmp_path,
            repo_roots={"repo": repo_root},
        ),
        github=FakeStageGitHub(pr_state={"state": "MERGED"}),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    original = WorkItem(
        repo="repo",
        kind=ItemKind.ISSUE,
        issue=2705,
        pr=12,
        stage=StageName.MERGE_WAIT,
        worktree=str(worktree),
        branch="2705-fix",
    )
    intent = LearningIntent.post_merge(repo="repo", issue=2705, pr=12)
    original.learning_intents.append(intent)
    original.compact_for_post_processing(
        ItemResult(passed=True, reason="merged", final_stage=StageName.MERGE_WAIT)
    )
    journal = coordinator._ctx_for_repo("repo").learning_journal
    journal.ensure_pending(
        intent.key,
        kind=intent.kind.value,
        identity=original.learning_journal_identity(intent),
    )
    assert journal.claim(intent.key)
    journal.finish(intent.key, succeeded=True)
    recovered = WorkItem(
        repo="repo",
        kind=ItemKind.ISSUE,
        issue=2705,
        pr=12,
        stage=StageName.FINISHED,
    )

    coordinator._restore_learning_intents(recovered, StageName.FINISHED, "already merged")

    assert recovered.stage is StageName.LEARNING
    assert recovered.worktree == str(worktree)
    assert recovered.branch == "2705-fix"
    assert recovered.post_processing is not None
    assert recovered.post_processing.result.passed


def test_no_learn_and_no_advise_keep_cleanup_on_separate_pool(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Disabling host skills does not collapse cleanup into the main pool."""
    import hephaestus.automation.mnemosyne_skill_host as host_module

    monkeypatch.setattr(
        host_module,
        "MnemosyneSkillHost",
        lambda: (_ for _ in ()).throw(AssertionError("host must not be constructed")),
    )
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            projects_dir=tmp_path,
            enable_learn=False,
            no_advise=True,
        ),
        github=FakeStageGitHub(),
        install_signals=False,
    )

    assert coordinator.auxiliary_pool is not coordinator.pool
    assert coordinator._auxiliary_pool_separate
    coordinator._shutdown_pool()


def test_no_learn_disables_recovered_intent_and_keeps_primary_route(tmp_path: Path) -> None:
    """The no-learn switch records a terminal skip without a host request."""
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            projects_dir=tmp_path,
            enable_learn=False,
        ),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    intent = LearningIntent.post_merge(repo="repo", issue=2705, pr=12)
    journal = coordinator._ctx_for_repo("repo").learning_journal
    journal.ensure_pending(intent.key, kind=intent.kind.value, identity=intent.journal_identity())
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=2705, stage=StageName.FINISHED)

    coordinator._restore_learning_intents(item, StageName.FINISHED, "already merged")

    assert item.stage is StageName.FINISHED
    assert item.learning_intents == []
    record = journal.load(intent.key)
    assert record is not None
    assert record["status"] == "failed"
    assert record["error"] == "learning_disabled"


def test_no_learn_restores_terminal_cleanup_before_disabling(tmp_path: Path) -> None:
    """The no-learn switch preserves worktree cleanup from the journal."""
    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo"], projects_dir=tmp_path, enable_learn=False),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    original = WorkItem(
        repo="repo",
        kind=ItemKind.ISSUE,
        issue=2705,
        pr=12,
        stage=StageName.MERGE_WAIT,
        worktree="build/.worktrees/issue-2705",
        branch="2705-fix",
    )
    intent = LearningIntent.post_merge(repo="repo", issue=2705, pr=12)
    original.learning_intents.append(intent)
    original.compact_for_post_processing(
        ItemResult(passed=True, reason="merged", final_stage=StageName.MERGE_WAIT)
    )
    journal = coordinator._ctx_for_repo("repo").learning_journal
    journal.ensure_pending(
        intent.key, kind=intent.kind.value, identity=original.learning_journal_identity(intent)
    )
    recovered = WorkItem(
        repo="repo", kind=ItemKind.ISSUE, issue=2705, pr=12, stage=StageName.FINISHED
    )

    coordinator._restore_learning_intents(recovered, StageName.FINISHED, "already merged")

    assert recovered.post_processing is not None
    assert recovered.post_processing.intent_keys == (intent.key,)
    assert recovered.worktree == original.worktree
    assert recovered.branch == original.branch

    recovered.state = "DONE"
    finished = coordinator.stages[StageName.FINISHED]
    assert finished.step(recovered, coordinator._ctx_for_repo("repo")) == StageOutcome(
        Disposition.FINISH_PASS,
        "done",
    )
    terminal = journal.load(intent.key)
    assert terminal is not None
    assert terminal["cleanup_status"] == "succeeded"


def test_pending_learning_ejection_keeps_cleanup_recoverable(tmp_path: Path) -> None:
    """Finished leaves the cleanup receipt pending until learning is terminal."""
    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo"], projects_dir=tmp_path),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    item = WorkItem(
        repo="repo",
        kind=ItemKind.ISSUE,
        issue=2705,
        pr=12,
        stage=StageName.FINISHED,
        state="CLEANUP",
        worktree="build/.worktrees/issue-2705",
        branch="2705-fix",
    )
    intent = LearningIntent.post_merge(repo="repo", issue=2705, pr=12)
    item.learning_intents.append(intent)
    item.compact_for_post_processing(
        ItemResult(passed=True, reason="merged", final_stage=StageName.MERGE_WAIT)
    )
    journal = coordinator._ctx_for_repo("repo").learning_journal
    journal.ensure_pending(
        intent.key, kind=intent.kind.value, identity=item.learning_journal_identity(intent)
    )

    finished = coordinator.stages[StageName.FINISHED]
    outcome = finished.step(item, coordinator._ctx_for_repo("repo"))

    assert outcome == StageOutcome(Disposition.EJECT, "learning_cleanup_pending")
    coordinator._route(item, outcome)
    record = journal.load(intent.key)
    assert record is not None
    assert record["status"] == "pending"
    assert record["cleanup_status"] == "pending"
    assert item.worktree == "build/.worktrees/issue-2705"


def test_malformed_post_processing_quarantines_only_one_issue(tmp_path: Path) -> None:
    """One damaged cleanup receipt does not stop later repository discovery."""
    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo"], projects_dir=tmp_path),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    intent = LearningIntent.post_merge(repo="repo", issue=2705, pr=12)
    journal = coordinator._ctx_for_repo("repo").learning_journal
    journal.ensure_pending(
        intent.key,
        kind=intent.kind.value,
        identity={
            **intent.journal_identity(),
            "post_processing": {
                "worktree": "",
                "branch": "",
                "resume_stage": StageName.FINISHED.value,
                "cleanup_payload": {},
                "result": {
                    "passed": "false",
                    "reason": "bad",
                    "final_stage": StageName.MERGE_WAIT.value,
                },
            },
        },
    )
    damaged = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=2705)
    healthy = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=2706)

    coordinator._restore_learning_intents(damaged, StageName.FINISHED, "merged")
    coordinator._restore_learning_intents(healthy, StageName.PLANNING, "open")

    assert damaged.stage is StageName.FINISHED
    assert damaged.result is not None
    assert not damaged.result.passed
    assert damaged.result.reason == "invalid durable learning recovery state"
    assert healthy.stage is StageName.REPO


def test_post_merge_persistence_failure_keeps_confirmed_result(tmp_path: Path) -> None:
    """Late auxiliary persistence cannot poison a confirmed merge result."""
    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo"], projects_dir=tmp_path),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        auxiliary_pool=FakeWorkerPool(),
        install_signals=False,
    )
    item = WorkItem(
        repo="repo",
        kind=ItemKind.ISSUE,
        issue=2705,
        pr=12,
        stage=StageName.MERGE_WAIT,
    )
    item.learning_intents.append(LearningIntent.post_merge(repo="repo", issue=2705, pr=12))
    journal = coordinator._ctx_for_repo("repo").learning_journal

    def fail_persistence(*_args: object, **_kwargs: object) -> None:
        raise OSError("journal unavailable")

    cast(Any, journal).ensure_pending = fail_persistence

    coordinator._route(item, StageOutcome(Disposition.FINISH_PASS, "merged"))

    assert item.stage is StageName.FINISHED
    assert item.result == ItemResult(
        passed=True,
        reason="merged",
        final_stage=StageName.MERGE_WAIT,
    )
    assert item.learning_intents == []
    assert item.payload["learning_failures"][0]["key"] == "post_merge"


class _BlockingLearningHost:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def execute(self, request: object) -> AthenaSkillResult:
        self.started.set()
        assert self.release.wait(timeout=2)
        sha = "a" * 40
        return AthenaSkillResult(
            kind="learn",
            delivery_receipt={
                "pr_url": "https://github.com/HomericIntelligence/Mnemosyne/pull/1",
                "pr_number": 1,
                "commit_sha": sha,
                "readback_head_sha": sha,
            },
        )


def test_single_main_worker_progresses_while_learning_is_blocked(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A blocked host call does not retain the one main-lane permit."""

    def _harness_forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("learning called an agent harness")

    monkeypatch.setattr(agent_runtime, "run_agent_text", _harness_forbidden)
    monkeypatch.setattr(agent_runtime, "run_agent_session", _harness_forbidden)
    monkeypatch.setattr(agent_runtime, "run_pi_text", _harness_forbidden)
    monkeypatch.setattr(agent_runtime, "run_pi_session", _harness_forbidden)
    monkeypatch.setattr(agent_runtime, "preflight_pi_environment", _harness_forbidden)
    host = _BlockingLearningHost()
    completions: queue.Queue = queue.Queue(maxsize=1)
    auxiliary = AuxiliaryWorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completions,
        athena_skill_executor=host,
    )
    github = FakeStageGitHub(labels=[STATE_PLAN_GO])
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            max_workers=1,
            learning_workers=1,
            learning_queue_capacity=1,
            projects_dir=tmp_path,
        ),
        github=github,
        pool=FakeWorkerPool(),
        auxiliary_pool=auxiliary,
        install_signals=False,
    )
    approved_plan = "Use the approved plan."
    github.comments[1] = [render_current_plan(approved_plan, revision=1)]
    learning = WorkItem(
        repo="repo", kind=ItemKind.ISSUE, issue=1, stage=StageName.LEARNING, state="ENTER"
    )
    learning.learning_intents.append(
        LearningIntent.approved_plan(
            repo="repo",
            issue=1,
            plan_revision=1,
            plan_fingerprint=plan_fingerprint(approved_plan),
        )
    )
    learning.learning_resume_stage = StageName.IMPLEMENTATION
    assert coordinator._push_item(learning, StageName.LEARNING, enter=True)
    claimed = coordinator._claim_item(StageName.LEARNING)
    assert claimed is learning
    coordinator._run_item(learning)
    assert host.started.wait(timeout=2)

    unrelated = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=2, stage=StageName.PLANNING)
    assert coordinator._push_item(unrelated, StageName.PLANNING, enter=True)
    assert coordinator.live_work_count == 1
    assert coordinator.learning_work_count == 1
    assert coordinator._claim_item(StageName.PLANNING) is unrelated
    progressed = threading.Event()

    class ProgressStage:
        def on_enter(self, _item: WorkItem, _ctx: object) -> StageOutcome:
            progressed.set()
            return StageOutcome(Disposition.EJECT, "unrelated completed")

        def step(self, _item: WorkItem, _ctx: object) -> StageOutcome:
            raise AssertionError("on_enter completes the unrelated item")

        def on_job_done(self, _item: WorkItem, _result: JobResult, _ctx: object) -> None:
            raise AssertionError("progress stage submits no job")

    coordinator.stages[StageName.PLANNING] = ProgressStage()
    coordinator._run_item(unrelated)
    assert progressed.is_set()
    assert unrelated.result is not None
    assert unrelated.result.passed
    assert unrelated.result.reason == "ejected: unrelated completed"
    assert not host.release.is_set()

    host.release.set()
    done, result = coordinator.auxiliary_completion_q.get(timeout=2)
    coordinator._handle_completion(done, result, auxiliary=True)
    auxiliary.shutdown(mark_interrupted=False)


@pytest.mark.skipif(not subprocess_registry.supported(), reason="requires POSIX process groups")
def test_forced_auxiliary_shutdown_stops_active_host_process() -> None:
    """Forced shutdown ends active host work and releases its worker thread."""

    class ProcessHost:
        def execute(self, request: object) -> AthenaSkillResult:
            del request
            run_subprocess(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                env={"PATH": os.defpath},
                check=False,
                timeout=60,
                track_process_group=True,
            )
            return AthenaSkillResult(kind="learn", error="interrupted")

        def cancel(self) -> None:
            subprocess_registry.terminate_all()

    completions: queue.Queue = queue.Queue(maxsize=1)
    pool = AuxiliaryWorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completions,
        athena_skill_executor=ProcessHost(),
    )
    request = AthenaSkillJob(
        request=AthenaSkillRequest(
            kind="learn",
            repo="repo",
            issue=1,
            agent="codex",
            model="default",
            cwd=Path.cwd(),
            timeout_s=60,
        )
    )
    pool.submit(request, "DONE")
    deadline = time.monotonic() + 3
    while subprocess_registry.live_count() == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert subprocess_registry.live_count() == 1

    pool.shutdown()

    _handle, result = completions.get(timeout=3)
    assert result.interrupted
    assert subprocess_registry.live_count() == 0


def test_terminal_handoff_compacts_payload_and_preserves_merge_result(tmp_path: Path) -> None:
    """Post-merge learning drops review data and keeps the merge outcome."""
    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo"], projects_dir=tmp_path),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, pr=7, stage=StageName.MERGE_WAIT)
    item.worktree = str(tmp_path / "repo" / "build" / ".worktrees" / "issue-1")
    item.branch = "1-fix"
    item.payload.update({"pr_diff": "large raw diff", "review_audit": {"large": "value"}})
    item.learning_intents.append(LearningIntent.post_merge(repo="repo", issue=1, pr=7))

    coordinator._route(item, StageOutcome(Disposition.FINISH_PASS, "merged"))

    assert item.stage is StageName.LEARNING
    assert item.payload["_learning_primary_reason"] == "merged"
    assert "pr_diff" not in item.payload
    assert "review_audit" not in item.payload
    assert item.post_processing is not None
    assert item.post_processing.result.passed
    assert item.post_processing.result.final_stage is StageName.MERGE_WAIT
    assert item.worktree.endswith("issue-1")
    assert item.branch == "1-fix"

    item.payload["learning_failures"] = [{"key": "intent", "error": "host failed"}]
    coordinator._route(item, StageOutcome(Disposition.ADVANCE, "learning terminal"))

    assert item.stage is StageName.FINISHED
    assert item.result is not None
    assert item.result.passed
    assert item.result.reason == "merged"
    assert item.result.final_stage is StageName.MERGE_WAIT
    assert item.worktree.endswith("issue-1")


def test_learning_completion_exception_parks_before_cleanup(tmp_path: Path) -> None:
    """A journal callback error preserves the claim and defers cleanup."""

    class RaisingStage:
        def on_job_done(self, item: WorkItem, result: object, ctx: object) -> None:
            del item, result, ctx
            raise OSError("journal unavailable")

    coordinator = Coordinator(
        PipelineConfig(org="org", repos=["repo"], projects_dir=tmp_path),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        auxiliary_pool=FakeWorkerPool(),
        install_signals=False,
    )
    item = WorkItem(
        repo="repo",
        kind=ItemKind.ISSUE,
        issue=1,
        pr=7,
        stage=StageName.LEARNING,
    )
    primary = ItemResult(passed=True, reason="merged", final_stage=StageName.MERGE_WAIT)
    item.learning_intents.append(LearningIntent.post_merge(repo="repo", issue=1, pr=7))
    item.compact_for_post_processing(primary)
    assert coordinator._push_item(item, StageName.LEARNING, enter=True)
    assert coordinator._claim_item(StageName.LEARNING) is item
    request = AthenaSkillJob(
        request=AthenaSkillRequest(
            kind="learn",
            repo="repo",
            issue=1,
            agent="codex",
            model="default",
            cwd=tmp_path,
            timeout_s=60,
        )
    )
    handle = JobHandle(job=request, on_done_state="RESULT")
    coordinator.auxiliary_in_flight[handle] = item
    coordinator.stages[StageName.LEARNING] = RaisingStage()  # type: ignore[assignment]

    coordinator._handle_completion(handle, JobResult(ok=False, error="disk"), auxiliary=True)

    assert item.stage is StageName.LEARNING
    assert item.post_processing is not None
    assert item.post_processing.result == primary
    assert item.result is not None and item.result.reason == "resumable at learning"
    assert item.payload["learning_failures"][0]["error"] == "journal_completion_failed"
    assert item in coordinator.items
    assert coordinator._terminal_summary.dispositions["resumable"] == 1


class _CompletionRecordingStage:
    """Record completion delivery without starting more work."""

    def __init__(self) -> None:
        self.completed: list[int] = []

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: object) -> None:
        del result, ctx
        self.completed.append(item.issue or 0)


def _queue_dual_lane_completions(
    coordinator: Coordinator, tmp_path: Path
) -> tuple[_CompletionRecordingStage, _CompletionRecordingStage]:
    """Publish one owned result to each coordinator completion channel."""
    main_stage = _CompletionRecordingStage()
    auxiliary_stage = _CompletionRecordingStage()
    coordinator.stages[StageName.PLANNING] = main_stage  # type: ignore[assignment]
    coordinator.stages[StageName.LEARNING] = auxiliary_stage  # type: ignore[assignment]

    main_item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, stage=StageName.PLANNING)
    main_handle = JobHandle(
        job=GitJob(repo="repo", op="push", timeout_s=1),
        on_done_state="DONE",
    )
    coordinator.in_flight[main_handle] = main_item
    coordinator.inflight_per_repo[main_item.repo] = 1

    auxiliary_item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=2, stage=StageName.LEARNING)
    auxiliary_handle = JobHandle(
        job=AthenaSkillJob(
            request=AthenaSkillRequest(
                kind="learn",
                repo="repo",
                issue=2,
                agent="codex",
                model="default",
                cwd=tmp_path,
                timeout_s=60,
            )
        ),
        on_done_state="DONE",
    )
    coordinator.auxiliary_in_flight[auxiliary_handle] = auxiliary_item

    coordinator.completion_q.put((main_handle, JobResult(ok=True, duration_s=0.5)))
    coordinator.auxiliary_completion_q.put(
        (auxiliary_handle, JobResult(ok=False, error="delivery failed", duration_s=0.75))
    )
    return main_stage, auxiliary_stage


def test_dual_completion_channels_drain_all_published_results(tmp_path: Path) -> None:
    """One drain handles ready main and auxiliary results without loss."""
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        auxiliary_pool=FakeWorkerPool(),
        install_signals=False,
    )
    main_stage, auxiliary_stage = _queue_dual_lane_completions(coordinator, tmp_path)
    coordinator.shutdown.set()

    coordinator._drain_completions()

    assert main_stage.completed == [1]
    assert auxiliary_stage.completed == [2]
    assert not coordinator.in_flight
    assert not coordinator.auxiliary_in_flight
    assert coordinator.completion_q.empty()
    assert coordinator.auxiliary_completion_q.empty()
    assert coordinator._auxiliary_job_count == 1
    assert coordinator._auxiliary_job_failure_count == 1


def test_dual_completion_saturation_drains_results_before_fault(tmp_path: Path) -> None:
    """A saturation fault cannot discard results already in either channel."""
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo"],
            learning_workers=3,
            learning_queue_capacity=1,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(),
        pool=FakeWorkerPool(),
        auxiliary_pool=FakeWorkerPool(),
        install_signals=False,
    )
    main_stage, auxiliary_stage = _queue_dual_lane_completions(coordinator, tmp_path)
    coordinator.shutdown.set()
    coordinator._completion_saturation.set()

    with pytest.raises(RuntimeError, match="completion queue saturated"):
        coordinator._drain_completions()

    assert main_stage.completed == [1]
    assert auxiliary_stage.completed == [2]
    assert not coordinator.in_flight
    assert not coordinator.auxiliary_in_flight
    assert coordinator.completion_q.empty()
    assert coordinator.auxiliary_completion_q.empty()
    assert any(event[0] == "completion_saturation" for event in coordinator.event_log)
    assert coordinator.auxiliary_completion_q.maxsize == 3
