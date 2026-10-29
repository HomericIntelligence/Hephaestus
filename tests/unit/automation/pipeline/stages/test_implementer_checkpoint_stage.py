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

from hephaestus.automation.pipeline import implementer_checkpoint
from hephaestus.automation.pipeline.implementer_checkpoint import LaneSnapshot
from hephaestus.automation.pipeline.jobs import AgentJob
from hephaestus.automation.pipeline.stages import JobRequest
from hephaestus.automation.pipeline.stages.implementation import (
    ImplementationStage,
    build_implementation_prompt,
)
from tests.unit.automation.pipeline.stages.test_stage_implementation import (
    _prepared_writer,
)

HEAD = "a" * 40


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
        ctx = make_ctx()
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
        ctx = make_ctx()
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
        ctx = make_ctx()
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
