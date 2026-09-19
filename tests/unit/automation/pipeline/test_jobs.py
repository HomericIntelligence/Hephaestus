"""Tests for job dataclasses."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

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

    @pytest.mark.parametrize("op", sorted(GIT_OPS - {"prepare_repository_validation"}))
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


def _ci_request_fixture(tmp_path: Path) -> tuple[Any, Any]:
    """Make a closed CI request without claiming provider evidence."""
    import importlib

    from tests.unit.automation.pipeline.test_repository_validation import _api, _plan

    domain = _api()
    plan = _plan(domain, tmp_path)
    invocation = domain.RepositoryValidationInvocation(
        plan, 1, "f" * 32, "ci", tuple(c.check_id for c in plan.checks)
    )
    api = importlib.import_module("hephaestus.automation.pipeline.github_jobs")
    return api, api.ReadRepositoryValidationCIRequest(invocation, "codex/repair", 160.0)


def test_repository_ci_request_keeps_complete_immutable_context(tmp_path: Path) -> None:
    """Keep plan ownership, branch, deadline, and job target in the closed request."""
    api, request = _ci_request_fixture(tmp_path)
    assert (request.repository, request.issue_number, request.pr_number) == (
        "LLM360/comet",
        1623,
        1639,
    )
    job = api.GitHubJob("comet", tmp_path, request, "Read CI validation.")
    assert job.request is request
    assert api.github_request_issue(request) == 1623
    assert api.github_request_pr(request) == 1639
    with pytest.raises(FrozenInstanceError):
        request.head_branch = "other"
    with pytest.raises(ValueError):
        api.GitHubJob("other", tmp_path, request, "Read CI validation.")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("deadline_s", None),
        ("deadline_s", True),
        ("deadline_s", float("inf")),
        ("deadline_s", 0),
        ("head_branch", ""),
        ("head_branch", "bad\nbranch"),
    ],
)
def test_repository_ci_request_rejects_invalid_inputs(
    tmp_path: Path, field: str, value: object
) -> None:
    """Reject a missing deadline and an invalid source branch."""
    from dataclasses import replace

    _, request = _ci_request_fixture(tmp_path)
    with pytest.raises(ValueError):
        replace(request, **{field: value})


def test_repository_ci_result_rejects_unrelated_receipts(tmp_path: Path) -> None:
    """Reject evidence from a different plan or evidence source."""
    from dataclasses import replace

    from tests.unit.automation.pipeline.test_repository_validation import _api, _receipt

    api, request = _ci_request_fixture(tmp_path)
    receipt = _receipt(_api(), request.invocation.plan, 0)
    result = api.RepositoryValidationCIRead(request, (receipt,), ())
    assert result.receipts == (receipt,)
    for changed in (replace(receipt, plan_id="e" * 64), replace(receipt, evidence_kind="local")):
        with pytest.raises(ValueError):
            api.RepositoryValidationCIRead(request, (changed,), ())
    with pytest.raises(ValueError):
        api.RepositoryValidationCIRead(request, (receipt, receipt), ())


@pytest.mark.parametrize(
    ("request_deadline", "caller_deadline", "expected"),
    [(150.0, 140.0, 140.0), (130.0, 140.0, 130.0), (300.0, 250.0, 220.0)],
)
def test_repository_ci_dispatch_uses_the_smallest_deadline(
    tmp_path: Path, monkeypatch, request_deadline: float, caller_deadline: float, expected: float
) -> None:
    """Use the request, caller, and worker deadlines at the real facade boundary."""
    import threading
    from dataclasses import replace
    from types import SimpleNamespace

    from hephaestus.automation import pipeline_github, pipeline_github_jobs
    from hephaestus.automation.pipeline_github_review_validation import CometCICollection

    api, request = _ci_request_fixture(tmp_path)
    request = replace(request, deadline_s=request_deadline)
    stop = threading.Event()
    captured = []
    monkeypatch.setattr("time.monotonic", lambda: 100.0)

    def reader(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return SimpleNamespace(**kwargs)

    def collect(invocation: Any, *, head_branch: str, reader: Any) -> Any:
        assert invocation == request.invocation
        assert head_branch == request.head_branch
        assert reader.shutdown is stop
        return CometCICollection()

    monkeypatch.setattr(pipeline_github, "CometCIReader", reader)
    monkeypatch.setattr(pipeline_github, "collect_comet_ci", collect)
    runner = pipeline_github_jobs.PipelineGitHubJobRunner(org="LLM360", dry_run=False)
    result = runner.run(
        api.GitHubJob("comet", tmp_path, request, "Read CI validation."),
        deadline_s=caller_deadline,
        shutdown=stop,
    )
    assert isinstance(result, api.RepositoryValidationCIRead)
    assert result.request == request
    assert captured == [{"deadline_s": expected, "shutdown": stop}]


@pytest.mark.parametrize("interruption", ["deadline", "cancelled"])
def test_repository_ci_dispatch_returns_typed_early_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    """Keep an early deadline or cancellation failure as a typed CI gap."""
    import threading
    from dataclasses import replace

    from hephaestus.automation import pipeline_github_jobs
    from hephaestus.automation.pipeline.github_jobs import RepositoryValidationCIRead

    api, request = _ci_request_fixture(tmp_path)
    monkeypatch.setattr("time.monotonic", lambda: 100.0)
    stop = threading.Event()
    if interruption == "deadline":
        request = replace(request, deadline_s=90.0)
    else:
        stop.set()
    runner = pipeline_github_jobs.PipelineGitHubJobRunner(org="LLM360", dry_run=False)
    result = runner.run(
        api.GitHubJob("comet", tmp_path, request, "Read CI validation."), shutdown=stop
    )
    assert isinstance(result, RepositoryValidationCIRead)
    assert result.request == request and result.receipts == ()
    assert result.gaps[0].reason == f"ci_request_{interruption}"


def _repository_execution(tmp_path: Path) -> Any:
    """Build execution metadata from the complete current Comet profile."""
    from hephaestus.automation.pipeline import repository_validation as api
    from tests.unit.automation.test_pipeline_github_review_validation import _ci_collection_fixture

    assert hasattr(api, "RepositoryValidationExecution"), "Execution metadata is not available."
    invocation, _ = _ci_collection_fixture(tmp_path)
    plan = invocation.plan
    lock_digest = next(
        digest for path, _, digest in plan.checks[0].source_digests if path == "uv.lock"
    )
    return api.RepositoryValidationExecution(
        plan=plan,
        check_id=plan.checks[0].check_id,
        attempt_generation=1,
        request_nonce="f" * 32,
        runtime_root=tmp_path / "host/build/hephaestus-review-validation/comet" / lock_digest,
        runtime_manifest_sha256="c" * 64,
        runtime_tree_sha256="d" * 64,
    )


def _repository_build_job(execution: Any, **overrides: Any) -> BuildTestJob:
    """Bind the selected command and source to one build job."""
    values = {
        "repo": "comet",
        "cwd": execution.plan.source_workspace.cwd,
        "argv": execution.plan.checks[0].argv,
        "timeout_s": 60,
        "expected_head_sha": execution.plan.reviewed_head,
        "immutable_source": True,
        "repository_validation": execution,
    }
    values.update(overrides)
    return BuildTestJob(**values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("argv", ("echo", "other")),
        ("cwd", Path("/other")),
        ("repo", "other"),
        ("expected_head_sha", "0" * 40),
        ("immutable_source", False),
        ("timeout_s", 121),
        ("verified_runner_source_revision", "a" * 40),
    ],
)
def test_repository_validation_runtime_preparation_rejects_conflicting_job(
    tmp_path: Path, field: str, value: Any
) -> None:
    """Keep runtime admission bound to its one immutable command."""
    from hephaestus.automation.pipeline.repository_validation import RepositoryValidationInvocation
    from hephaestus.automation.pipeline.repository_validation_preparation import (
        RepositoryValidationRuntimeRequest,
    )

    execution = _repository_execution(tmp_path)
    request = RepositoryValidationRuntimeRequest(
        RepositoryValidationInvocation(execution.plan, 1, "f" * 32, "local", (execution.check_id,)),
        123.0,
    )
    with pytest.raises(ValueError):
        _repository_build_job(
            execution,
            repository_validation=None,
            repository_validation_preparation=request,
            **{field: value},
        )
    with pytest.raises(ValueError):
        _repository_build_job(execution, repository_validation_preparation=request)


def test_repository_validation_source_preparation_requires_closed_request(tmp_path: Path) -> None:
    """Reject absent requests and arbitrary source-preparation arguments."""
    from dataclasses import replace

    from hephaestus.automation.pipeline.repository_validation_preparation import (
        RepositoryValidationSourceRequest,
    )

    plan = _repository_execution(tmp_path).plan
    request = RepositoryValidationSourceRequest(
        plan.repository,
        plan.issue_number,
        plan.pr_number,
        plan.source_workspace,
        plan.reviewed_head,
        plan.reviewed_base,
        plan.diff_base_sha,
        plan.changes,
        1,
        "f" * 32,
        123.0,
    )
    job = GitJob(
        "comet",
        "prepare_repository_validation",
        60,
        expected_repository=plan.repository,
        deadline_s=request.deadline_s,
        workspace=plan.source_workspace,
        repository_validation_preparation=request,
    )
    assert job.repository_validation_preparation == request
    for changes in (
        {"kwargs": {"argv": ("echo", "other")}},
        {"op": "fetch_main"},
        {"workspace": None},
        {"repo": "other"},
        {"deadline_s": 124.0},
        {"timeout_s": 121},
        {"repository_validation_preparation": None},
    ):
        with pytest.raises(ValueError):
            replace(job, **changes)


def test_repository_execution_preserves_build_job_compatibility(tmp_path: Path) -> None:
    """Append one frozen execution value without changing legacy positional fields."""
    from dataclasses import fields

    from hephaestus.automation import pipeline

    execution = _repository_execution(tmp_path)
    job = _repository_build_job(execution)
    assert job.repository_validation is execution
    assert pipeline.RepositoryValidationExecution is type(execution)
    assert tuple(field.name for field in fields(execution)) == (
        "plan",
        "check_id",
        "attempt_generation",
        "request_nonce",
        "runtime_root",
        "runtime_manifest_sha256",
        "runtime_tree_sha256",
    )
    assert tuple(field.name for field in fields(BuildTestJob)) == (
        "repo",
        "cwd",
        "argv",
        "timeout_s",
        "expected_head_sha",
        "immutable_source",
        "verified_runner_source_revision",
        "descr",
        "repository_validation",
        "repository_validation_preparation",
        "capability_target",
    )
    with pytest.raises(FrozenInstanceError):
        execution.request_nonce = "e" * 32  # type: ignore[misc]
    legacy = BuildTestJob("legacy", tmp_path, ("pytest",), 30, "", False, None, "Legacy check.")
    assert legacy.repository_validation is None
    assert legacy.repository_validation_preparation is None
    assert legacy.capability_target is None
    assert job.repository_validation_preparation is None
    assert legacy.descr == "Legacy check."


@pytest.mark.parametrize(
    "field,value",
    [
        ("check_id", "comet.unknown"),
        ("attempt_generation", 0),
        ("attempt_generation", True),
        ("request_nonce", "e" * 31),
        ("runtime_root", Path("relative")),
        ("runtime_root", Path("/other/environment")),
        ("runtime_manifest_sha256", "C" * 64),
        ("runtime_tree_sha256", "d" * 63),
    ],
)
def test_repository_execution_rejects_partial_identity(
    tmp_path: Path, field: str, value: Any
) -> None:
    """Reject incomplete request, command, and runtime identities."""
    from dataclasses import replace

    execution = _repository_execution(tmp_path)
    with pytest.raises(ValueError):
        replace(execution, **{field: value})


@pytest.mark.parametrize(
    "fault", ["plan_digest", "nonexecutable", "missing_lock", "wrong_lock_root"]
)
def test_repository_execution_rechecks_plan_and_dependency_binding(
    tmp_path: Path, fault: str
) -> None:
    """Do not accept changed plans or a runtime for a different lock."""
    from dataclasses import replace

    execution = _repository_execution(tmp_path)
    plan = execution.plan
    if fault == "plan_digest":
        object.__setattr__(plan, "plan_id", "0" * 64)
    elif fault == "nonexecutable":
        plan = replace(plan, execution_allowed=False, coverage_reason="profile_unavailable")
    elif fault == "missing_lock":
        checks = tuple(
            replace(
                check,
                source_digests=tuple(
                    source for source in check.source_digests if source[0] != "uv.lock"
                ),
            )
            for check in plan.checks
        )
        plan = replace(plan, checks=checks)
    else:
        with pytest.raises(ValueError):
            replace(execution, runtime_root=execution.runtime_root.parent / ("0" * 64))
        return
    with pytest.raises(ValueError):
        replace(execution, plan=plan)


@pytest.mark.parametrize(
    "field,value",
    [
        ("repo", "other"),
        ("cwd", Path("/other/checkout")),
        ("argv", ("uv", "run", "other")),
        ("expected_head_sha", "0" * 40),
        ("immutable_source", False),
        ("verified_runner_source_revision", "0" * 40),
        ("timeout_s", 0),
        ("repository_validation", {"partial": True}),
    ],
)
def test_repository_build_job_rejects_conflicting_metadata(
    tmp_path: Path, field: str, value: Any
) -> None:
    """Reject job fields that disagree with the complete execution value."""
    execution = _repository_execution(tmp_path)
    with pytest.raises(ValueError):
        _repository_build_job(execution, **{field: value})
