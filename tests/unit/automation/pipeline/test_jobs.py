"""Tests for job dataclasses."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from hephaestus.automation.pipeline.jobs import (
    GIT_OPS,
    AgentJob,
    BuildTestJob,
    GitJob,
    JobHandle,
    JobResult,
)
from hephaestus.automation.pipeline.routing import StageName


class TestGitJobValidation:
    """Tests for GitJob op validation."""

    @pytest.mark.parametrize("op", sorted(GIT_OPS))
    def test_valid_ops_construct(self, op: str) -> None:
        job = GitJob(repo="test/repo", op=op, timeout_s=60)
        assert job.op == op
        assert job.repo == "test/repo"

    def test_invalid_op_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown git op"):
            GitJob(repo="test/repo", op="bogus", timeout_s=60)

    def test_invalid_op_suggests_valid_ops(self) -> None:
        with pytest.raises(ValueError, match=r"clone.*commit_push.*create_worktree"):
            GitJob(repo="test/repo", op="invalid", timeout_s=60)


class TestJobDataclassesFrozen:
    """Tests that job dataclasses are frozen."""

    def test_agent_job_frozen(self) -> None:
        job = AgentJob(
            repo="test/repo",
            issue=123,
            agent="claude",
            model="opus-4-8",
            prompt_builder=lambda: "prompt",
            cwd=Path("/tmp"),
            timeout_s=60,
        )
        with pytest.raises(FrozenInstanceError):
            job.timeout_s = 120  # type: ignore[misc]

    def test_build_test_job_frozen(self) -> None:
        job = BuildTestJob(
            repo="test/repo",
            cwd=Path("/tmp"),
            argv=("pytest",),
            timeout_s=60,
        )
        with pytest.raises(FrozenInstanceError):
            job.timeout_s = 120  # type: ignore[misc]

    def test_git_job_frozen(self) -> None:
        job = GitJob(repo="test/repo", op="rebase", timeout_s=60)
        with pytest.raises(FrozenInstanceError):
            job.timeout_s = 120  # type: ignore[misc]

    def test_job_result_frozen(self) -> None:
        result = JobResult(ok=True)
        with pytest.raises(FrozenInstanceError):
            result.ok = False  # type: ignore[misc]

    def test_job_handle_frozen(self) -> None:
        job = GitJob(repo="test/repo", op="rebase", timeout_s=60)
        handle = JobHandle(job=job, on_done_state=StageName.IMPLEMENTATION)
        with pytest.raises(FrozenInstanceError):
            handle.on_done_state = StageName.CI  # type: ignore[misc]


class TestDefaultFactories:
    """Tests that default factory fields are independent per instance."""

    def test_agent_job_prompt_kwargs_independent(self) -> None:
        job1 = AgentJob(
            repo="test/repo",
            issue=123,
            agent="claude",
            model="opus-4-8",
            prompt_builder=lambda: "prompt",
            cwd=Path("/tmp"),
            timeout_s=60,
        )
        job2 = AgentJob(
            repo="test/repo",
            issue=124,
            agent="claude",
            model="opus-4-8",
            prompt_builder=lambda: "prompt",
            cwd=Path("/tmp"),
            timeout_s=60,
        )
        # Verify they are separate dicts
        assert job1.prompt_kwargs is not job2.prompt_kwargs

    def test_git_job_kwargs_independent(self) -> None:
        job1 = GitJob(repo="test/repo", op="rebase", timeout_s=60)
        job2 = GitJob(repo="test/repo", op="rebase", timeout_s=60)
        assert job1.kwargs is not job2.kwargs
