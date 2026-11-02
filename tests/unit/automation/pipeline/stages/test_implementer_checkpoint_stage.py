"""Stage-level tests for the implementer checkpoint wiring (issue #3435).

These cover only the ordinary implement dispatch touchpoints in
``stages/implementation.py``: the injected advisory resume context and the
turn stamp. The checkpoint module itself is covered by
``tests/unit/automation/pipeline/test_implementer_checkpoint.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.agent_config import AGENT_IMPLEMENTER
from hephaestus.automation.pipeline import implementer_checkpoint
from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.implementer_checkpoint import LaneSnapshot
from hephaestus.automation.pipeline.jobs import AgentJob, JobHandle, JobResult
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.stages import JobRequest
from hephaestus.automation.pipeline.stages.implementation import (
    ImplementationStage,
    build_implementation_prompt,
)
from tests.unit.automation.pipeline.conftest import (
    FakeWorkerPool,
    claim_test_item,
    fake_worker_factories,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub
from tests.unit.automation.pipeline.stages.test_stage_implementation import (
    _prepared_writer,
)

HEAD = "a" * 40
INTERVAL_S = 300


def _fake_run_git(*, git_dir: Path, head: str = HEAD) -> Any:
    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        subcommand = args[2:]
        if subcommand == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(args, 0, f"{git_dir}\n", "")
        if subcommand == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, f"{head}\n", "")
        if subcommand == ["status", "--porcelain=v1"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected git invocation: {args!r}")

    return _run


def _prepared_lane(
    item: Any,
    tmp_path: Path,
    *,
    step: str = "turn in progress",
    head: str = HEAD,
) -> Path:
    """Attach a prepared writer under ``tmp_path`` with one checkpoint record."""
    worktree = tmp_path / "lane"
    git_dir = worktree / ".git"
    git_dir.mkdir(parents=True)
    item.worktree = str(worktree)
    item.branch = "1-auto-impl"
    _prepared_writer(item)
    lane = LaneSnapshot(git_dir=git_dir, head_sha=head, changed_files=("a.py", "b.py"))
    implementer_checkpoint.write_checkpoint(
        lane,
        issue=item.issue or 0,
        branch=item.branch,
        worktree=item.worktree,
        elapsed_s=42.0,
        attempt=1,
        step=step,
    )
    return worktree


class TestImplementWaitCheckpointWiring:
    """Ordinary implement dispatch wiring of the checkpoint feature."""

    def test_injects_resume_context_and_stamps_turn(
        self, make_ctx: Any, make_work_item: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A timeout retry carries the advisory checkpoint block into the prompt."""
        stage = ImplementationStage()
        ctx = make_ctx(config_overrides={"implementer_checkpoint_interval": INTERVAL_S})
        item = make_work_item(issue=1, state="IMPLEMENT_WAIT")
        _prepared_lane(item, tmp_path)
        monkeypatch.setattr(
            implementer_checkpoint,
            "run_git",
            _fake_run_git(git_dir=tmp_path / "lane" / ".git"),
        )

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.descr == "implement"
        resume_context = result.job.prompt_kwargs["resume_context"]
        assert "## Mid-Run Checkpoint From the Previous Turn (Advisory)" in resume_context
        assert "a.py" in resume_context
        assert result.job.prompt_builder is build_implementation_prompt
        prompt = result.job.prompt_builder(**result.job.prompt_kwargs)
        assert resume_context in prompt
        stamp = item.payload["_impl_checkpoint_turn_start"]
        assert isinstance(stamp, float) and stamp > 0

    def test_superseded_head_gives_a_fresh_prompt(
        self, make_ctx: Any, make_work_item: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A checkpoint against an older lane head is discarded, not injected."""
        stage = ImplementationStage()
        ctx = make_ctx(config_overrides={"implementer_checkpoint_interval": INTERVAL_S})
        item = make_work_item(issue=1, state="IMPLEMENT_WAIT")
        worktree = _prepared_lane(item, tmp_path, head=HEAD)
        monkeypatch.setattr(
            implementer_checkpoint,
            "run_git",
            _fake_run_git(git_dir=worktree / ".git", head="c" * 40),
        )

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.prompt_kwargs["resume_context"] == ""
        prompt = result.job.prompt_builder(**result.job.prompt_kwargs)
        assert "Mid-Run Checkpoint" not in prompt
        # The stale checkpoint was discarded and the new turn still stamped.
        assert not (worktree / ".git" / implementer_checkpoint.CHECKPOINT_FILENAME).exists()
        assert "_impl_checkpoint_turn_start" in item.payload

    def test_enabled_without_checkpoint_still_stamps(
        self, make_ctx: Any, make_work_item: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh first turn records no resume context and still stamps."""
        stage = ImplementationStage()
        ctx = make_ctx(config_overrides={"implementer_checkpoint_interval": INTERVAL_S})
        item = make_work_item(issue=1, state="IMPLEMENT_WAIT")
        item.worktree = str(tmp_path / "lane")
        item.branch = "1-auto-impl"
        _prepared_writer(item)
        git_dir = tmp_path / "lane" / ".git"
        git_dir.mkdir(parents=True)
        monkeypatch.setattr(implementer_checkpoint, "run_git", _fake_run_git(git_dir=git_dir))

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.prompt_kwargs["resume_context"] == ""
        assert "_impl_checkpoint_turn_start" in item.payload

    def test_disabled_leaves_prompt_and_payload_untouched(
        self, make_ctx: Any, make_work_item: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Interval zero keeps dispatch byte-identical to the prior behavior."""
        stage = ImplementationStage()
        ctx = make_ctx(config_overrides={"implementer_checkpoint_interval": 0})
        item = make_work_item(issue=1, state="IMPLEMENT_WAIT")
        item.worktree = str(tmp_path / "lane")
        item.branch = "1-auto-impl"
        _prepared_writer(item)

        def _raising(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise AssertionError(f"disabled path must not call git: {args!r}")

        monkeypatch.setattr(implementer_checkpoint, "run_git", _raising)

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.prompt_kwargs["resume_context"] == ""
        prompt = result.job.prompt_builder(**result.job.prompt_kwargs)
        assert "Mid-Run Checkpoint" not in prompt
        assert "_impl_checkpoint_turn_start" not in item.payload


class _Clock:
    """Controllable monotonic clock for the coordinator tick."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _CompletionOnlyStage:
    """Record completions and route nothing.

    The coordinator's completion path is the subject here. Routing belongs to
    the stage under test elsewhere.
    """

    def __init__(self) -> None:
        self.completions: list[JobResult] = []

    def on_enter(self, item: Any, ctx: Any) -> None:
        """Do nothing on entry."""
        return None

    def on_cancelled_before_start(self, item: Any, ctx: Any) -> None:
        """Do nothing when execution did not start."""
        return None

    def on_job_done(self, item: Any, result: JobResult, ctx: Any) -> None:
        """Keep the completion result for the assertions."""
        self.completions.append(result)

    def step(self, item: Any, ctx: Any) -> Any:
        """Fail: the completion path must not step a stage."""
        raise AssertionError("the completion path must not step the stage")


class TestCoordinatorTimeoutContinuity:
    """The coordinator seam that joins a timeout to the retry turn."""

    def test_timeout_completion_carries_the_session_and_the_checkpoint_into_the_retry(
        self,
        make_ctx: Any,
        make_work_item: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC1: a killed resume turn leaves a checkpoint and the retry resumes."""
        stage = ImplementationStage()
        ctx = make_ctx(config_overrides={"implementer_checkpoint_interval": INTERVAL_S})
        item = make_work_item(issue=1, stage=StageName.IMPLEMENTATION, state="IMPLEMENT_WAIT")
        worktree = tmp_path / "lane"
        git_dir = worktree / ".git"
        git_dir.mkdir(parents=True)
        item.worktree = str(worktree)
        item.branch = "1-auto-impl"
        _prepared_writer(item)
        item.session_ids[AGENT_IMPLEMENTER] = "session-1"
        monkeypatch.setattr(implementer_checkpoint, "run_git", _fake_run_git(git_dir=git_dir))

        dispatched = stage.step(item, ctx)
        assert isinstance(dispatched, JobRequest)
        assert isinstance(dispatched.job, AgentJob)
        assert dispatched.job.resume_session_id == "session-1"

        clock = _Clock()
        coordinator = Coordinator(
            PipelineConfig(
                org="org",
                repos=["repo-a"],
                projects_dir=tmp_path,
                rate_guard_enabled=False,
                implementer_checkpoint_interval=INTERVAL_S,
            ),
            github=FakeStageGitHub(),
            **fake_worker_factories(FakeWorkerPool(), None),
            install_signals=False,
            monotonic=clock,
        )
        routed = _CompletionOnlyStage()
        coordinator.stages[StageName.IMPLEMENTATION] = routed
        claim_test_item(coordinator, item)
        handle = JobHandle(job=dispatched.job, on_done_state=StageName.IMPLEMENTATION)
        coordinator.in_flight[handle] = item
        coordinator.inflight_per_repo[item.repo] += 1

        # The run-loop walk writes the dispatch snapshot, then one interval one.
        coordinator._observe_implementer_progress()
        clock.now += INTERVAL_S + 1
        coordinator._observe_implementer_progress()
        assert [event for event, *_rest in coordinator.event_log].count("implementer_progress") == 1
        snapshot = _validated_checkpoint(git_dir, item)
        assert snapshot is not None
        assert snapshot.last_step == "turn in progress"

        # The provider died at its deadline while it ran the resume.
        clock.now += 10.0
        coordinator._handle_completion(
            handle,
            JobResult(ok=False, error="timeout", duration_s=float(INTERVAL_S)),
        )

        assert routed.completions
        assert item.session_ids[AGENT_IMPLEMENTER] == "session-1"
        timeout_record = _validated_checkpoint(git_dir, item)
        assert timeout_record is not None
        assert timeout_record.session_id == "session-1"
        assert "timeout" in timeout_record.last_step

        # The retry turn renders the advisory block and keeps the resume id.
        item.state = "IMPLEMENT_WAIT"
        retry = stage.step(item, ctx)
        assert isinstance(retry, JobRequest)
        assert isinstance(retry.job, AgentJob)
        assert retry.job.resume_session_id == "session-1"
        assert "Mid-Run Checkpoint" in retry.job.prompt_kwargs["resume_context"]


def _validated_checkpoint(git_dir: Path, item: Any) -> Any:
    """Read the lane checkpoint through the production validation path."""
    raw = implementer_checkpoint.read_checkpoint(implementer_checkpoint.checkpoint_path(git_dir))
    assert raw is not None
    return implementer_checkpoint.validate_checkpoint(
        raw,
        issue=item.issue or 0,
        branch=item.branch,
        worktree=item.worktree,
    )
