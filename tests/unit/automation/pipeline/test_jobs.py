"""Tests for job dataclasses."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from hephaestus.automation.pipeline.jobs import (
    GIT_OPS,
    AgentJob,
    BuildTestJob,
    CompactJob,
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

    @pytest.mark.parametrize("field", ("repository_lock_wait_timeout_s", "deadline_s"))
    @pytest.mark.parametrize("value", (0, -1, float("inf")))
    def test_invalid_checkout_lock_limits_raise(self, field: str, value: float) -> None:
        """Checkout lock limits must be finite positive values."""
        with pytest.raises(ValueError, match=field):
            if field == "repository_lock_wait_timeout_s":
                GitJob(
                    repo="test/repo",
                    op="clone",
                    timeout_s=60,
                    repository_lock_wait_timeout_s=value,
                )
            else:
                GitJob(
                    repo="test/repo",
                    op="clone",
                    timeout_s=60,
                    deadline_s=value,
                )

    def test_repository_lock_wait_rejects_operation_deadline(self) -> None:
        """Checkout admission and ordinary job deadlines cannot be combined."""
        with pytest.raises(ValueError, match="cannot be combined"):
            GitJob(
                repo="test/repo",
                op="clone",
                timeout_s=60,
                deadline_s=10.0,
                repository_lock_wait_timeout_s=5.0,
            )

    def test_repository_lock_wait_rejects_an_ordinary_git_job(self) -> None:
        """Only repository checkout jobs can request separate lock admission."""
        with pytest.raises(ValueError, match="only for repository checkout"):
            GitJob(
                repo="test/repo",
                op="commit_push",
                timeout_s=60,
                repository_lock_wait_timeout_s=5.0,
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

    def test_compact_job_frozen(self) -> None:
        job = CompactJob(
            repo="test/repo",
            issue=123,
            agent="claude",
            session_agent="implementer",
            model="claude-haiku-4-5",
            cwd=Path("/tmp"),
            timeout_s=60,
        )
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
            handle.on_done_state = StageName.PR_REVIEW  # type: ignore[misc]


class TestBuildTestJobArgv:
    """Tests that BuildTestJob.argv is always a tuple."""

    def test_verified_runner_request_keeps_canonical_command_and_revision(self) -> None:
        """A verified-runner job carries data only until a worker executes it."""
        revision = "a" * 40

        job = BuildTestJob(
            repo="test/repo",
            cwd=Path("/tmp"),
            argv=("bash", "scripts/run_ci_local.sh", "all", "--rebuild"),
            timeout_s=60,
            verified_runner_source_revision=revision,
        )

        assert job.argv == ("bash", "scripts/run_ci_local.sh", "all", "--rebuild")
        assert job.verified_runner_source_revision == revision

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
        h1 = JobHandle(job=job, on_done_state=StageName.PR_REVIEW)
        h2 = JobHandle(job=job, on_done_state=StageName.PR_REVIEW)
        assert h1 != h2
        assert len({h1: "a", h2: "b"}) == 2

    def test_handle_hashable_with_unhashable_job_fields(self) -> None:
        """Handles hash by identity even when the job carries dict kwargs."""
        job = GitJob(repo="test/repo", op="rebase", timeout_s=60, kwargs={"cwd": Path("/tmp")})
        handle = JobHandle(job=job, on_done_state=StageName.PR_REVIEW)
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


def test_pretest_result_digest_binds_actual_bounded_json() -> None:
    """Canonical results retain their values without invented reply fields."""
    import hashlib

    from hephaestus.automation.pipeline.jobs import remediation_pretest_result_digest

    assert remediation_pretest_result_digest(None) == hashlib.sha256(b"null").hexdigest()
    assert (
        remediation_pretest_result_digest({"b": 2, "a": 1})
        == hashlib.sha256(b'{"a":1,"b":2}').hexdigest()
    )
    for invalid in (object(), float("nan"), "x" * (1024 * 1024 + 1)):
        with pytest.raises(ValueError):
            remediation_pretest_result_digest(invalid)


def test_recovered_ready_input_does_not_invent_a_predecessor(tmp_path: Path) -> None:
    """Read-only ready pins can omit history that the closed record does not store."""
    import json

    from hephaestus.automation.pipeline.jobs import RemediationPretestInput
    from tests.unit.automation.test_remediation_recovery import _pretest_payload

    payload = _pretest_payload(tmp_path)
    inputs = RemediationPretestInput(
        repository=payload["repository"],
        issue_number=payload["issue_number"],
        pr_number=payload["pr_number"],
        branch=payload["branch"],
        expected_remote_sha=payload["expected_remote_sha"],
        source_receipt_json=json.dumps(
            payload["source_receipt"], sort_keys=True, separators=(",", ":")
        ),
        source_receipt_sha256=payload["source_receipt_sha256"],
        thread_snapshot_json=payload["thread_snapshot_json"],
        batch_nonce=payload["batch_nonce"],
        allowed_paths=("a.py",),
        approved_scope_sha256="f" * 64,
        candidate_sequence=2,
        expected_previous_record_sha256=None,
    )
    assert inputs.expected_previous_record_sha256 is None
