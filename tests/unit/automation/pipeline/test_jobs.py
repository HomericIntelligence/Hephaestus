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


class TestAgentJobAllowedTools:
    """Tests for the fail-closed AgentJob.allowed_tools contract (#2162)."""

    def test_default_scope_is_read_only(self) -> None:
        job = AgentJob(
            repo="test/repo",
            issue=123,
            agent="claude",
            model="opus-4-8",
            prompt_builder=lambda: "prompt",
            cwd=Path("/tmp"),
            timeout_s=60,
        )
        assert job.allowed_tools == "Read,Glob,Grep"

    @pytest.mark.parametrize("bad", ["", "   ", "\t"])
    def test_agent_job_rejects_empty_allowed_tools(self, bad: str) -> None:
        with pytest.raises(ValueError, match="allowed_tools"):
            AgentJob(
                repo="test/repo",
                issue=123,
                agent="claude",
                model="opus-4-8",
                prompt_builder=lambda: "prompt",
                cwd=Path("/tmp"),
                timeout_s=60,
                allowed_tools=bad,
            )


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


class TestBuildTestJobArgv:
    """Tests that BuildTestJob.argv is always a tuple."""

    def test_argv_tuple_preserved(self) -> None:
        job = BuildTestJob(repo="test/repo", cwd=Path("/tmp"), argv=("pytest", "-q"), timeout_s=60)
        assert job.argv == ("pytest", "-q")
        assert isinstance(job.argv, tuple)

    def test_argv_list_normalized_to_tuple(self) -> None:
        job = BuildTestJob(
            repo="test/repo",
            cwd=Path("/tmp"),
            argv=["pytest", "-q"],  # type: ignore[arg-type]
            timeout_s=60,
        )
        assert job.argv == ("pytest", "-q")
        assert isinstance(job.argv, tuple)


class TestJobHandleIdentity:
    """Tests for JobHandle's identity-based hashing and equality (eq=False)."""

    def test_identical_specs_produce_distinct_handles(self) -> None:
        """Two handles over the same job spec are neither equal nor colliding."""
        job = GitJob(repo="test/repo", op="rebase", timeout_s=60)
        h1 = JobHandle(job=job, on_done_state=StageName.CI)
        h2 = JobHandle(job=job, on_done_state=StageName.CI)
        assert h1 != h2
        assert len({h1: "a", h2: "b"}) == 2

    def test_handle_hashable_with_unhashable_job_fields(self) -> None:
        """Handles hash by identity even when the job carries dict kwargs."""
        job = GitJob(repo="test/repo", op="rebase", timeout_s=60, kwargs={"cwd": Path("/tmp")})
        handle = JobHandle(job=job, on_done_state=StageName.CI)
        tracked = {handle: "pending"}
        assert tracked[handle] == "pending"


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
