"""Bind Comet review checks without an exceptional bootstrap grant."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation.pipeline.github_jobs import GitHubJob, ReadRepositoryValidationCIRequest
from hephaestus.automation.pipeline.jobs import BuildTestJob, GitJob
from hephaestus.automation.pipeline.repository_validation import RepositoryValidationPlan
from hephaestus.automation.pipeline.stages import pr_review_jobs, pr_review_repository_validation
from hephaestus.automation.pipeline.stages.base import JobRequest
from hephaestus.automation.pipeline.stages.pr_review import PrReviewStage
from hephaestus.automation.pipeline.stages.pr_review_threads import REVIEW_CHECKOUT_WAIT


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Comet fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout.strip()


def _comet_checkout(root: Path) -> tuple[str, str]:
    fixtures = Path(__file__).resolve().parents[4] / "fixtures/comet-review-validation/current"
    shutil.copytree(fixtures, root)
    package = root / "src/comet"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    payload = package / "example.py"
    payload.write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "init", "--initial-branch=main")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Create the source fixture.")
    base = _git(root, "rev-parse", "HEAD")
    payload.write_text("VALUE = 2\n", encoding="utf-8")
    _git(root, "add", "src/comet/example.py")
    _git(root, "commit", "-m", "Change the reviewed source.")
    head = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "--detach", head)
    return base, head


def test_comet_empty_bootstrap_submits_bound_validation_before_review(
    tmp_path: Path, make_ctx: Any, make_work_item: Any
) -> None:
    """Start validation after source binding without a bootstrap grant."""
    from hephaestus.automation.pipeline.jobs import GitJob, JobResult
    from hephaestus.automation.pipeline.repository_validation_preparation import (
        RepositoryValidationSourceRead,
    )
    from hephaestus.automation.pipeline_github_review_validation import comet_plan_for_workspace

    root = tmp_path / "source"
    base, head = _comet_checkout(root)
    workspace = WorkspaceBinding.source(
        cwd=root,
        reusable_root=tmp_path,
        repository="LLM360/comet",
        ownership_key="comet-review-1200",
        item_number=1200,
        lane=SourceLane.REVIEW,
        revision=head,
        generation=1,
        detached=True,
    )
    item = make_work_item(repo="comet", issue=1200, pr=1200, state=REVIEW_CHECKOUT_WAIT)
    item.worktree = str(root)
    item.payload.update(
        {
            "review_checkout_expected_head": head,
            "review_checkout_ready": True,
            "pr_head_sha": head,
            "pr_base_sha": base,
            "pr_base_branch": "main",
            "pr_head_branch": "codex/comet-fixture",
            "reviewed_pr_base_sha": base,
            "review_target_base_sha": base,
            "review_diff_base_sha": base,
            "review_changed_paths": ["src/comet/example.py"],
            "review_change_records": [("M", "src/comet/example.py")],
            "pr_diff": _git(root, "diff", f"{base}...{head}"),
            "host_verification_bootstrap_json": "",
        }
    )
    ctx = make_ctx(org="LLM360")

    stage = PrReviewStage()
    with patch.object(
        pr_review_repository_validation, "source_workspace_binding", return_value=workspace
    ) as bind:
        result = stage.step(item, ctx)

    assert bind.called, result
    assert bind.call_args_list[0].args == (item, ctx, SourceLane.REVIEW)
    assert bind.call_args_list[0].kwargs == {"revision": head}
    assert item.payload["host_verification_bootstrap_json"] == ""
    assert isinstance(result, JobRequest)
    assert isinstance(result.job, GitJob)
    request = result.job.repository_validation_preparation
    assert request is not None
    plan = comet_plan_for_workspace(
        workspace, issue_number=1200, pr_number=1200, reviewed_base=base, timeout_s=30
    )
    stage.on_job_done(
        item, JobResult(ok=True, value=RepositoryValidationSourceRead(request, plan)), ctx
    )
    assert "repository_validation_attempt" not in item.payload
    item.state = result.on_done_state
    result = stage._repository_validation_source_wait(item, ctx)
    assert isinstance(result, JobRequest)
    assert isinstance(result.job, GitHubJob), (
        "Comet must collect bound validation before it starts source review."
    )
    assert result.on_done_state == "REPOSITORY_VALIDATION_CI_WAIT"
    assert isinstance(result.job.request, ReadRepositoryValidationCIRequest)
    plan = result.job.request.plan
    assert isinstance(plan, RepositoryValidationPlan)
    assert plan.source_workspace == workspace
    assert plan.reviewed_head == head
    assert plan.reviewed_base == base
    assert plan.changes == (("M", "src/comet/example.py"),)
    assert plan.execution_allowed
    assert {check.check_id for check in plan.checks} >= {
        "comet.python.ruff-format",
        "comet.python.ruff-check",
        "comet.python.ty-check",
        "comet.python.pr-tests",
    }
    assert ctx.github.mutation_log == []


def test_checkout_callback_retains_actual_change_records(make_work_item: Any) -> None:
    """Store immutable change records without changing the legacy base meaning."""
    from hephaestus.automation.pipeline.jobs import JobResult

    item = make_work_item(repo="comet", issue=1200, pr=1200, state=REVIEW_CHECKOUT_WAIT)
    item.payload.update(
        {
            "review_checkout_pending": True,
            "pr_base_sha": "b" * 40,
            "review_checkout_expected_head": "a" * 40,
        }
    )
    records = [("A", "src/comet/new.py"), ("D", "src/comet/old.py")]
    result = JobResult(
        ok=True,
        value={
            "ready": True,
            "head": "a" * 40,
            "base": "c" * 40,
            "diff_base_sha": "c" * 40,
            "target_base_sha": "b" * 40,
            "diff": "bound diff",
            "changed_paths": [path for _, path in records],
            "change_records": records,
        },
    )
    assert pr_review_jobs.PrReviewJobs._consume_review_checkout_result(item, result)
    assert item.payload["review_checkout_ready"] is True
    assert item.payload.get("review_change_records") == tuple(records)
    assert item.payload.get("review_diff_base_sha") == "c" * 40
    assert item.payload.get("review_target_base_sha") == "b" * 40
    assert item.payload["reviewed_pr_base_sha"] == "c" * 40
    records.append(("A", "mutated.py"))
    assert len(item.payload["review_change_records"]) == 2


def test_repository_validation_source_preparation_does_not_run_on_coordinator(
    tmp_path: Path, make_work_item: Any, make_ctx: Any
) -> None:
    """Submit source inspection without doing Git work on the coordinator."""
    from hephaestus.automation.pipeline.jobs import GitJob

    item, request, _ = _pending_ci_item(tmp_path, make_work_item)
    plan = request.plan
    item.payload.pop("repository_validation_attempt")
    item.payload.pop("repository_validation_ci_request")
    item.payload.update(
        review_change_records=plan.changes,
        review_changed_paths=[path for _, path in plan.changes],
        review_target_base_sha=plan.reviewed_base,
        review_diff_base_sha=plan.diff_base_sha,
    )
    with patch.object(
        pr_review_repository_validation,
        "comet_plan_for_workspace",
        side_effect=AssertionError("Source inspection ran on the coordinator."),
        create=True,
    ):
        result = PrReviewStage()._start_repository_validation(
            item, make_ctx(org="LLM360"), plan.source_workspace
        )
    assert isinstance(result, JobRequest)
    assert isinstance(result.job, GitJob)
    assert result.job.op == "prepare_repository_validation"
    assert result.job.workspace == plan.source_workspace
    assert item.payload.get("repository_validation_source_request") is not None
    assert "repository_validation_attempt" not in item.payload


def test_repository_validation_runtime_preparation_does_not_run_on_coordinator(
    tmp_path: Path, make_work_item: Any, make_ctx: Any
) -> None:
    """Reserve one local request and let a worker inspect its runtime."""
    from dataclasses import replace

    from hephaestus.automation.pipeline.jobs import BuildTestJob, JobResult

    item, _, receipt = _pending_ci_item(tmp_path, make_work_item)
    stage = PrReviewStage()
    ctx = make_ctx(org="LLM360")
    stage.on_job_done(item, JobResult(ok=True, value=replace(receipt, receipts=())), ctx)
    with patch.object(
        pr_review_repository_validation,
        "admit_runtime",
        side_effect=AssertionError("Runtime inspection ran on the coordinator."),
        create=True,
    ):
        result = stage._repository_validation_ci_wait(item, ctx)
    assert isinstance(result, JobRequest)
    assert isinstance(result.job, BuildTestJob)
    assert result.job.repository_validation is None
    assert item.payload.get("repository_validation_runtime_request") is not None
    attempt = item.payload["repository_validation_attempt"]
    assert attempt.pending is not None
    assert attempt.pending.evidence_kind == "local"
    assert not attempt.receipts


def test_new_round_discards_old_change_records(make_work_item: Any) -> None:
    """Discard source status evidence before another review round."""
    from hephaestus.automation.pipeline.stages.pr_review_round_state import (
        _clear_round_review_state,
    )

    item = make_work_item(repo="comet", issue=1200, pr=1200)
    item.payload.update(
        {
            "review_change_records": (("M", "old.py"),),
            "review_diff_base_sha": "a" * 40,
            "review_target_base_sha": "b" * 40,
            "repository_validation_local_request": object(),
            "repository_validation_source_request": object(),
            "repository_validation_runtime_request": object(),
            "repository_validation_source_result": object(),
            "repository_validation_runtime_result": object(),
        }
    )
    _clear_round_review_state(item)
    assert "repository_validation_local_request" not in item.payload
    assert "repository_validation_source_request" not in item.payload
    assert "repository_validation_runtime_request" not in item.payload
    assert "repository_validation_source_result" not in item.payload
    assert "repository_validation_runtime_result" not in item.payload
    for key in ("review_change_records", "review_diff_base_sha", "review_target_base_sha"):
        assert key not in item.payload


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("change_records", None),
        ("change_records", [("A", "different.py")]),
        ("change_records", [("R100", "selected.py")]),
        ("change_records", [("M", "selected.py"), ("M", "selected.py")]),
        ("diff_base_sha", "d" * 40),
        ("target_base_sha", "d" * 40),
        ("head", None),
        ("head", "d" * 40),
        ("changed_paths", ["bad\udcff.py"]),
    ],
)
def test_checkout_callback_rejects_unbound_change_records(
    make_work_item: Any, field: str, value: object
) -> None:
    """Reject incomplete or conflicting source records before validation."""
    from hephaestus.automation.pipeline.jobs import JobResult

    item = make_work_item(repo="comet", issue=1200, pr=1200, state=REVIEW_CHECKOUT_WAIT)
    item.payload.update(
        {
            "review_checkout_pending": True,
            "pr_base_sha": "b" * 40,
            "review_checkout_expected_head": "a" * 40,
            "review_change_records": (("M", "old.py"),),
            "review_diff_base_sha": "d" * 40,
            "review_target_base_sha": "e" * 40,
        }
    )
    payload = {
        "ready": True,
        "head": "a" * 40,
        "diff": "bound diff",
        "base": "c" * 40,
        "diff_base_sha": "c" * 40,
        "target_base_sha": "b" * 40,
        "changed_paths": ["selected.py"],
        "change_records": [("M", "selected.py")],
    }
    payload[field] = value
    if field == "changed_paths":
        payload["change_records"] = [("M", "bad\udcff.py")]
    assert pr_review_jobs.PrReviewJobs._consume_review_checkout_result(
        item, JobResult(ok=True, value=payload)
    )
    assert item.payload["review_checkout_ready"] is False
    assert "review_checkout_error" in item.payload
    for key in ("review_change_records", "review_diff_base_sha", "review_target_base_sha"):
        assert key not in item.payload


def _pending_ci_item(
    tmp_path: Path,
    make_work_item: Any,
    changes: tuple[tuple[str, str], ...] = (("M", "tests/test_pool.py"),),
) -> tuple[Any, Any, Any]:
    """Make an admitted plan with a pending CI request for stage tests."""
    import time

    from hephaestus.automation.pipeline.github_jobs import (
        ReadRepositoryValidationCIRequest,
        RepositoryValidationCIRead,
    )
    from hephaestus.automation.pipeline.repository_validation import (
        RepositoryValidationAttempt,
        RepositoryValidationReceipt,
        begin_validation_request,
    )
    from tests.unit.automation.test_pipeline_github_review_validation import _ci_collection_fixture

    invocation, _ = _ci_collection_fixture(tmp_path, changes)
    plan = invocation.plan
    attempt, invocation = begin_validation_request(
        RepositoryValidationAttempt(plan, 1, "f" * 32), "ci", invocation.check_ids
    )
    request = ReadRepositoryValidationCIRequest(invocation, "codex/repair", time.monotonic() + 60)
    item = make_work_item(
        repo="comet", issue=plan.issue_number, pr=plan.pr_number, state=REVIEW_CHECKOUT_WAIT
    )
    item.worktree = str(tmp_path)
    item.payload.update(
        {
            "repository_validation_attempt": attempt,
            "repository_validation_ci_request": request,
            "reviewed_pr_head_sha": plan.reviewed_head,
            "pr_head_sha": plan.reviewed_head,
            "reviewed_pr_base_sha": plan.reviewed_base,
            "pr_base_sha": plan.reviewed_base,
            "pr_head_branch": "codex/repair",
            "reviewed_pr_proof_generation": 1,
        }
    )
    receipts = tuple(
        RepositoryValidationReceipt(
            repository=plan.repository,
            pr_number=plan.pr_number,
            plan_id=plan.plan_id,
            check_id=check.check_id,
            reviewed_head=plan.reviewed_head,
            reviewed_base=plan.reviewed_base,
            argv=check.argv,
            source_digests=check.source_digests,
            evidence_kind="ci",
            status="success",
        )
        for check in plan.checks
        if check.check_id in invocation.check_ids
    )
    return item, request, RepositoryValidationCIRead(request, receipts)


def _preparation_callback(
    tmp_path: Path, make_work_item: Any, make_ctx: Any, phase: str
) -> tuple[Any, Any, Any, Any, str]:
    """Submit one preparation request and construct its external worker result."""
    from dataclasses import replace

    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.repository_validation import RepositoryValidationExecution
    from hephaestus.automation.pipeline.repository_validation_preparation import (
        RepositoryValidationRuntimeRead,
        RepositoryValidationSourceRead,
    )

    item, ci_request, ci = _pending_ci_item(tmp_path, make_work_item)
    stage, ctx = PrReviewStage(), make_ctx(org="LLM360")
    plan = ci_request.plan
    value: RepositoryValidationSourceRead | RepositoryValidationRuntimeRead
    if phase == "source":
        item.payload.pop("repository_validation_attempt")
        item.payload.pop("repository_validation_ci_request")
        item.payload.update(
            review_change_records=plan.changes,
            review_changed_paths=[path for _, path in plan.changes],
            review_diff_base_sha=plan.diff_base_sha,
            review_target_base_sha=plan.reviewed_base,
        )
        submitted = stage._start_repository_validation(item, ctx, plan.source_workspace)
        assert isinstance(submitted, JobRequest)
        assert isinstance(submitted.job, GitJob)
        request = submitted.job.repository_validation_preparation
        assert request is not None
        value = RepositoryValidationSourceRead(request, plan)
    else:
        stage.on_job_done(item, JobResult(ok=True, value=replace(ci, receipts=())), ctx)
        submitted = stage._repository_validation_ci_wait(item, ctx)
        assert isinstance(submitted, JobRequest)
        assert isinstance(submitted.job, BuildTestJob)
        runtime_request = submitted.job.repository_validation_preparation
        assert runtime_request is not None
        runtime = _runtime_for_stage_plan(plan)
        invocation = runtime_request.invocation
        execution = RepositoryValidationExecution(
            plan,
            invocation.check_ids[0],
            invocation.generation,
            invocation.request_nonce,
            runtime.root,
            runtime.manifest.manifest_sha256,
            runtime.manifest.tree_sha256,
        )
        value = RepositoryValidationRuntimeRead(runtime_request, execution)
    return item, stage, ctx, value, submitted.on_done_state


@pytest.mark.parametrize("phase", ["source", "runtime"])
@pytest.mark.parametrize(
    "fault",
    [
        "nonce",
        "generation",
        "missing",
        "failed",
        "interrupted",
        "duplicate",
        "unowned",
        "live_head",
        "live_workspace",
    ],
)
def test_repository_validation_preparation_rejects_invalid_callbacks(
    tmp_path: Path, make_work_item: Any, make_ctx: Any, phase: str, fault: str
) -> None:
    """A foreign or failed callback cannot replace current pending ownership."""
    from dataclasses import replace

    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.stages.base import StageOutcome

    item, stage, ctx, value, state = _preparation_callback(
        tmp_path, make_work_item, make_ctx, phase
    )
    key = f"repository_validation_{phase}_request"
    pending = item.payload[key]
    invalid = value
    if fault in {"nonce", "generation"}:
        changes = {"request_nonce": "e" * 32} if fault == "nonce" else {"generation": 2}
        if phase == "source":
            invalid = replace(value, request=replace(pending, **changes))
        else:
            invocation = replace(pending.invocation, **changes)
            execution_changes = (
                {"request_nonce": "e" * 32} if fault == "nonce" else {"attempt_generation": 2}
            )
            invalid = replace(
                value,
                request=replace(pending, invocation=invocation),
                execution=replace(value.execution, **execution_changes),
            )
    elif fault == "duplicate":
        stage.on_job_done(item, JobResult(ok=True, value=value), ctx)
    elif fault == "unowned":
        item.payload.pop(key)
    elif fault == "live_head":
        item.payload["pr_head_sha"] = "e" * 40
    elif fault == "live_workspace":
        item.worktree = str(tmp_path / "other")
    stage.on_job_done(
        item,
        JobResult(
            ok=fault != "failed",
            interrupted=fault == "interrupted",
            value=None if fault == "missing" else invalid,
        ),
        ctx,
    )
    assert item.payload.get("repository_validation_failure")
    if fault not in {"duplicate", "unowned"}:
        assert item.payload[key] == pending
    stage.on_job_done(item, JobResult(ok=True, value=value), ctx)
    item.state = state
    assert isinstance(
        getattr(stage, f"_repository_validation_{phase}_wait")(item, ctx), StageOutcome
    )
    assert ctx.github.mutation_log == []


@pytest.mark.parametrize("phase", ["source", "runtime"])
def test_repository_validation_preparation_rechecks_identity_before_next_job(
    tmp_path: Path, make_work_item: Any, make_ctx: Any, phase: str
) -> None:
    """A later head change must block an already accepted preparation result."""
    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.stages.base import StageOutcome

    item, stage, ctx, value, _ = _preparation_callback(tmp_path, make_work_item, make_ctx, phase)
    stage.on_job_done(item, JobResult(ok=True, value=value), ctx)
    assert "repository_validation_failure" not in item.payload
    item.payload["pr_head_sha"] = "e" * 40
    result = getattr(stage, f"_repository_validation_{phase}_wait")(item, ctx)
    assert isinstance(result, StageOutcome)
    assert item.payload.get("repository_validation_failure")


def test_repository_validation_runtime_failure_consumes_a_terminal_gap(
    tmp_path: Path, make_work_item: Any, make_ctx: Any
) -> None:
    """A failed owned admission must not leave a reusable local invocation."""
    from dataclasses import replace

    from hephaestus.automation.pipeline.jobs import JobResult

    item, stage, ctx, value, _ = _preparation_callback(
        tmp_path, make_work_item, make_ctx, "runtime"
    )
    failed = replace(value, execution=None, failure="runtime_preparation_failed")
    stage.on_job_done(item, JobResult(ok=False, value=failed), ctx)
    attempt = item.payload["repository_validation_attempt"]
    assert attempt.pending is None
    assert any(gap.reason == "local_runtime_unavailable" for gap in attempt.gaps)
    assert "repository_validation_runtime_request" not in item.payload


def test_ci_callback_is_consumed_before_the_coordinator_changes_state(
    tmp_path: Path, make_work_item: Any, make_ctx: Any
) -> None:
    """Consume the typed result while the item remains in its submitting state."""
    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.repository_validation import validation_attempt_coverage

    item, request, receipt = _pending_ci_item(tmp_path, make_work_item)
    stage = PrReviewStage()
    stage.on_job_done(item, JobResult(ok=True, value=receipt), make_ctx(org="LLM360"))
    assert item.state == REVIEW_CHECKOUT_WAIT
    attempt = item.payload["repository_validation_attempt"]
    assert attempt.pending is None
    assert attempt.consumed_request_ids == (request.invocation.request_id,)
    assert (
        validation_attempt_coverage(
            attempt,
            reviewed_head=attempt.plan.reviewed_head,
            reviewed_base=attempt.plan.reviewed_base,
        ).status
        == "complete"
    )
    assert "repository_validation_ci_request" not in item.payload
    assert "review_audit" not in item.payload


def test_complete_ci_routes_to_review_without_a_host_runtime_lookup(
    tmp_path: Path, make_work_item: Any, make_ctx: Any
) -> None:
    """Let complete CI evidence advance without any local runtime lookup."""
    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.stages.base import Continue

    item, _, receipt = _pending_ci_item(tmp_path, make_work_item)
    stage = PrReviewStage()
    ctx = make_ctx(org="LLM360")
    stage.on_job_done(item, JobResult(ok=True, value=receipt), ctx)
    item.state = "REPOSITORY_VALIDATION_CI_WAIT"
    expected = Continue(next_state="VALIDATE_WAIT")
    with (
        patch.object(stage, "_route_threads_before_broad_review", return_value=expected) as route,
        patch.object(
            stage,
            "_submit_repository_validation_local",
            side_effect=AssertionError("Unexpected local runtime lookup"),
        ),
        patch(
            "hephaestus.automation.pipeline.stages.pr_review._reviewed_terminal_pr_outcome",
            return_value=None,
        ),
    ):
        assert stage.step(item, ctx) == expected
    route.assert_called_once_with(item, ctx)
    assert ctx.github.mutation_log == []


@pytest.mark.parametrize(
    "change", ["nonce", "branch", "generation", "failed-job", "missing-result", "duplicate"]
)
def test_ci_stage_rejects_invalid_or_repeated_callbacks(
    tmp_path: Path, make_work_item: Any, make_ctx: Any, change: str
) -> None:
    """A callback gap cannot become complete coverage or start source review."""
    from dataclasses import replace

    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.repository_validation import validation_attempt_coverage
    from hephaestus.automation.pipeline.stages.base import StageOutcome

    item, request, receipt = _pending_ci_item(tmp_path, make_work_item)
    original_receipt = receipt
    stage = PrReviewStage()
    ctx = make_ctx(org="LLM360")
    if change == "nonce":
        receipt = replace(
            receipt,
            request=replace(
                request, invocation=replace(request.invocation, request_nonce="e" * 32)
            ),
        )
    elif change == "branch":
        receipt = replace(receipt, request=replace(request, head_branch="other"))
    elif change == "generation":
        receipt = replace(
            receipt, request=replace(request, invocation=replace(request.invocation, generation=2))
        )
    elif change == "duplicate":
        stage.on_job_done(item, JobResult(ok=True, value=receipt), ctx)
    result = JobResult(
        ok=change != "failed-job", value=None if change == "missing-result" else receipt
    )
    stage.on_job_done(item, result, ctx)
    assert (
        validation_attempt_coverage(
            item.payload["repository_validation_attempt"],
            reviewed_head=request.invocation.plan.reviewed_head,
            reviewed_base=request.invocation.plan.reviewed_base,
        ).status
        == "gap"
    )
    assert item.payload["repository_validation_attempt"].gaps
    stage.on_job_done(item, JobResult(ok=True, value=original_receipt), ctx)
    assert (
        validation_attempt_coverage(
            item.payload["repository_validation_attempt"],
            reviewed_head=request.invocation.plan.reviewed_head,
            reviewed_base=request.invocation.plan.reviewed_base,
        ).status
        == "gap"
    )
    item.state = "REPOSITORY_VALIDATION_CI_WAIT"
    with (
        patch.object(
            stage,
            "_route_threads_before_broad_review",
            side_effect=AssertionError("Invalid callback started review"),
        ),
        patch(
            "hephaestus.automation.pipeline.stages.pr_review._reviewed_terminal_pr_outcome",
            return_value=None,
        ),
    ):
        assert isinstance(stage.step(item, ctx), StageOutcome)


@pytest.mark.parametrize(
    "field", ["reviewed_pr_head_sha", "pr_base_sha", "reviewed_pr_proof_generation"]
)
def test_ci_stage_checks_current_identity_before_advancing(
    tmp_path: Path, make_work_item: Any, make_ctx: Any, field: str
) -> None:
    """Reject complete receipts when the stage identity has changed."""
    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.stages.base import StageOutcome

    item, _, receipt = _pending_ci_item(tmp_path, make_work_item)
    ctx = make_ctx(org="LLM360")
    stage = PrReviewStage()
    stage.on_job_done(item, JobResult(ok=True, value=receipt), ctx)
    item.payload[field] = 2 if field == "reviewed_pr_proof_generation" else "e" * 40
    item.state = "REPOSITORY_VALIDATION_CI_WAIT"
    with (
        patch.object(
            stage,
            "_route_threads_before_broad_review",
            side_effect=AssertionError("Stale evidence started review"),
        ),
        patch(
            "hephaestus.automation.pipeline.stages.pr_review._reviewed_terminal_pr_outcome",
            return_value=None,
        ),
    ):
        assert isinstance(stage.step(item, ctx), StageOutcome)


@pytest.mark.parametrize(
    "entry",
    [
        "_eval",
        "_handle_clean_go",
        "_go_audit_receipt",
        "_go_audit_publish",
        "_write_go",
        "write_go_default",
    ],
)
@pytest.mark.parametrize("validation", ["missing", "pending", "complete"])
def test_comet_go_requires_complete_current_validation(
    tmp_path: Path, make_work_item: Any, make_ctx: Any, entry: str, validation: str
) -> None:
    """Require complete validation before a GO write or audit publication."""
    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.stages.base import Continue, Disposition, StageOutcome
    from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub
    from tests.unit.automation.pipeline.stages.test_stage_pr_review import _valid_audit

    item, _, receipt = _pending_ci_item(tmp_path, make_work_item)
    github = FakeStageGitHub(
        unresolved=[(0, 0)],
        pr_impl_state=(True, False),
        pr_state={
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "baseRefOid": "b" * 40,
            "autoMergeRequest": None,
        },
    )
    ctx = make_ctx(org="LLM360", github=github)
    stage = PrReviewStage()
    if validation == "complete":
        stage.on_job_done(item, JobResult(ok=True, value=receipt), ctx)
    elif validation == "missing":
        item.payload.pop("repository_validation_attempt")
    audit = _valid_audit()
    item.payload.update(
        review_audit=audit,
        pending_implementation_go_audit=audit,
        pending_implementation_go_audit_head="a" * 40,
    )
    result = (
        stage.write_go(item, github)
        if entry == "write_go_default"
        else getattr(stage, entry)(item, ctx)
    )
    if validation == "complete":
        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.ADVANCE
        assert github.mutation_log
    else:
        assert not github.mutation_log
        assert isinstance(result, (Continue, StageOutcome))
        assert isinstance(result, Continue) or result.disposition is not Disposition.ADVANCE
        assert item.attempts.get("pr_review_iter", 0) == 0


@pytest.mark.parametrize("live_base", [None, "c" * 40])
@pytest.mark.parametrize("drift_at", ["before", "after"])
def test_comet_go_checks_live_base_before_and_after_label(
    tmp_path: Path, make_work_item: Any, make_ctx: Any, live_base: str | None, drift_at: str
) -> None:
    """Do not advance if the live target base differs from the validated base."""
    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.stages.base import Disposition, StageOutcome
    from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

    item, _, receipt = _pending_ci_item(tmp_path, make_work_item)
    github = FakeStageGitHub(unresolved=[(0, 0)], pr_impl_state=(True, False))
    ctx = make_ctx(org="LLM360", github=github)
    stage = PrReviewStage()
    stage.on_job_done(item, JobResult(ok=True, value=receipt), ctx)
    valid = {
        "state": "OPEN",
        "headRefOid": "a" * 40,
        "baseRefOid": "b" * 40,
        "autoMergeRequest": None,
    }
    changed = dict(valid, baseRefOid=live_base)
    observations = [changed] if drift_at == "before" else [valid, changed]
    with patch.object(github, "gh_pr_state", side_effect=observations):
        result = stage.write_go(item, github, org="LLM360")
    assert not (isinstance(result, StageOutcome) and result.disposition is Disposition.ADVANCE)
    if drift_at == "before":
        assert not github.mutation_log
    else:
        assert [event[0] for event in github.mutation_log] == ["mark_pr_implementation_go"]


@pytest.mark.parametrize("completed", [False, True])
def test_review_entry_discards_prior_validation_attempt(
    tmp_path: Path, make_work_item: Any, make_ctx: Any, completed: bool
) -> None:
    """A new stage entry cannot retain validation from a previous attempt."""
    from hephaestus.automation.pipeline.jobs import JobResult

    item, _, receipt = _pending_ci_item(tmp_path, make_work_item)
    stage = PrReviewStage()
    ctx = make_ctx(org="LLM360")
    if completed:
        stage.on_job_done(item, JobResult(ok=True, value=receipt), ctx)
    item.payload["repository_validation_failure"] = "previous_failure"
    item.payload["repository_validation_local_request"] = object()
    preparation_keys = [
        f"repository_validation_{phase}_{kind}"
        for phase in ("source", "runtime")
        for kind in ("request", "result")
    ]
    item.payload.update({key: object() for key in preparation_keys})
    stage.on_enter(item, ctx)
    assert "repository_validation_attempt" not in item.payload
    assert "repository_validation_ci_request" not in item.payload
    assert "repository_validation_failure" not in item.payload
    assert "repository_validation_local_request" not in item.payload
    assert all(key not in item.payload for key in preparation_keys)


def _runtime_for_stage_plan(plan: Any) -> Any:
    """Supply admitted metadata without claiming an executable runtime."""
    from hephaestus.automation.repository_validation_runtime import (
        AdmittedRuntime,
        parse_runtime_manifest,
    )
    from tests.unit.automation.pipeline.test_repository_validation_runtime import _encode, _manifest

    sources = {path: digest for path, _, digest in plan.checks[0].source_digests}
    project, lock = sources["pyproject.toml"], sources["uv.lock"]
    manifest = parse_runtime_manifest(
        _encode(_manifest(project=project, lock=lock)),
        pyproject_sha256=project,
        uv_lock_sha256=lock,
    )
    root = Path(pr_review_jobs.__file__).resolve().parents[4]
    return AdmittedRuntime(root / "build/hephaestus-review-validation/comet" / lock, manifest)


def _complete_runtime_preparation(stage: Any, item: Any, ctx: Any, submitted: Any) -> Any:
    """Supply worker metadata without treating admission as check evidence."""
    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.repository_validation import RepositoryValidationExecution
    from hephaestus.automation.pipeline.repository_validation_preparation import (
        RepositoryValidationRuntimeRead,
        RepositoryValidationRuntimeRequest,
    )

    request = submitted.job.repository_validation_preparation
    assert type(request) is RepositoryValidationRuntimeRequest
    assert submitted.job.repository_validation is None
    invocation = request.invocation
    runtime = _runtime_for_stage_plan(invocation.plan)
    execution = RepositoryValidationExecution(
        invocation.plan,
        invocation.check_ids[0],
        invocation.generation,
        invocation.request_nonce,
        runtime.root,
        runtime.manifest.manifest_sha256,
        runtime.manifest.tree_sha256,
    )
    receipts = item.payload["repository_validation_attempt"].receipts
    previous_state = item.state
    stage.on_job_done(
        item, JobResult(ok=True, value=RepositoryValidationRuntimeRead(request, execution)), ctx
    )
    assert item.state == previous_state
    assert item.payload["repository_validation_attempt"].pending == invocation
    assert item.payload["repository_validation_attempt"].receipts == receipts
    item.state = submitted.on_done_state
    return stage._repository_validation_runtime_wait(item, ctx)


@pytest.mark.parametrize("ci_count", [0, 2])
def test_local_stage_completes_only_the_uncovered_checks(
    tmp_path: Path, make_work_item: Any, make_ctx: Any, ci_count: int
) -> None:
    """Complete local or combined coverage before source review starts."""
    import json
    from dataclasses import replace

    from hephaestus.automation.pipeline.jobs import BuildTestJob, JobResult
    from hephaestus.automation.pipeline.repository_validation import RepositoryValidationLocalRead
    from hephaestus.automation.pipeline.stages.base import Continue

    item, _, ci = _pending_ci_item(tmp_path, make_work_item)
    stage, ctx = PrReviewStage(), make_ctx(org="LLM360")
    stage.on_job_done(
        item, JobResult(ok=True, value=replace(ci, receipts=ci.receipts[:ci_count])), ctx
    )
    item.state = "REPOSITORY_VALIDATION_CI_WAIT"
    expected = Continue(next_state="VALIDATE_WAIT")
    selected = []
    with (
        patch.object(stage, "_route_threads_before_broad_review", return_value=expected) as route,
        patch(
            "hephaestus.automation.pipeline.stages.pr_review._reviewed_terminal_pr_outcome",
            return_value=None,
        ),
    ):
        for receipt in ci.receipts[ci_count:]:
            submitted = stage.step(item, ctx)
            assert isinstance(submitted, JobRequest)
            submitted = _complete_runtime_preparation(stage, item, ctx, submitted)
            assert isinstance(submitted.job, BuildTestJob)
            execution = submitted.job.repository_validation
            assert execution is not None
            assert execution.check_id == receipt.check_id
            assert item.payload["repository_validation_local_request"] == execution
            attempt = item.payload["repository_validation_attempt"]
            assert attempt.pending.check_ids == (receipt.check_id,)
            assert attempt.pending.evidence_kind == "local"
            selected.append(execution.check_id)
            route.assert_not_called()
            prior_state = item.state
            stage.on_job_done(
                item,
                JobResult(
                    ok=True,
                    value=RepositoryValidationLocalRead(
                        execution, receipt=replace(receipt, evidence_kind="local")
                    ),
                ),
                ctx,
            )
            assert item.state == prior_state
            assert item.payload["repository_validation_attempt"].pending is None
            assert "repository_validation_local_request" not in item.payload
            item.state = submitted.on_done_state
        assert stage.step(item, ctx) == expected
    assert selected == [receipt.check_id for receipt in ci.receipts[ci_count:]]
    route.assert_called_once_with(item, ctx)
    assert ctx.github.mutation_log == []

    with patch.object(
        pr_review_jobs, "source_workspace_binding", return_value=ci.request.plan.source_workspace
    ):
        review = stage._submit_review_job(item, ctx)
    assert isinstance(review, JobRequest)
    summary = json.loads(review.job.prompt_kwargs["repository_validation_json"])
    assert summary["status"] == "complete"
    assert {row["check_id"]: row["evidence_kind"] for row in summary["receipts"]} == {
        receipt.check_id: ("ci" if index < ci_count else "local")
        for index, receipt in enumerate(ci.receipts)
    }
    assert summary["plan_id"] == ci.request.plan.plan_id


@pytest.mark.parametrize(
    "fault", ["nonce", "generation", "runtime", "missing", "failed", "duplicate"]
)
def test_local_stage_retains_callback_failures(
    tmp_path: Path, make_work_item: Any, make_ctx: Any, fault: str
) -> None:
    """Keep a callback failure after later valid evidence arrives."""
    from dataclasses import replace

    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.repository_validation import RepositoryValidationLocalRead
    from hephaestus.automation.pipeline.stages.base import StageOutcome

    item, _, ci = _pending_ci_item(tmp_path, make_work_item)
    stage, ctx = PrReviewStage(), make_ctx(org="LLM360")
    stage.on_job_done(item, JobResult(ok=True, value=replace(ci, receipts=())), ctx)
    item.state = "REPOSITORY_VALIDATION_CI_WAIT"
    submitted = stage._repository_validation_ci_wait(item, ctx)
    submitted = _complete_runtime_preparation(stage, item, ctx, submitted)
    assert isinstance(submitted, JobRequest)
    execution = submitted.job.repository_validation
    receipt = replace(ci.receipts[0], evidence_kind="local")
    valid = RepositoryValidationLocalRead(execution, receipt=receipt)
    if fault == "duplicate":
        stage.on_job_done(item, JobResult(ok=True, value=valid), ctx)
    changes: dict[str, dict[str, Any]] = {
        "nonce": {"request_nonce": "e" * 32},
        "generation": {"attempt_generation": 2},
        "runtime": {"runtime_manifest_sha256": "e" * 64},
    }
    invalid = (
        replace(valid, execution=replace(execution, **changes[fault]))
        if fault in changes
        else valid
    )
    if fault == "failed":
        invalid = replace(valid, receipt=replace(receipt, status="failed"))
    stage.on_job_done(
        item, JobResult(ok=fault != "failed", value=None if fault == "missing" else invalid), ctx
    )
    stage.on_job_done(item, JobResult(ok=True, value=valid), ctx)
    with patch.object(
        stage,
        "_submit_repository_validation_local",
        side_effect=AssertionError("No retry is allowed."),
    ):
        assert isinstance(stage._repository_validation_ci_wait(item, ctx), StageOutcome)
    assert ctx.github.mutation_log == []


def test_local_stage_records_missing_runtime_as_a_terminal_gap(
    tmp_path: Path, make_work_item: Any, make_ctx: Any
) -> None:
    """Do not retry an unchanged missing runtime or start a review."""
    from dataclasses import replace

    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.repository_validation_preparation import (
        RepositoryValidationRuntimeRead,
    )
    from hephaestus.automation.pipeline.stages.base import StageOutcome

    item, _, ci = _pending_ci_item(tmp_path, make_work_item)
    stage, ctx = PrReviewStage(), make_ctx(org="LLM360")
    stage.on_job_done(item, JobResult(ok=True, value=replace(ci, receipts=())), ctx)
    submitted = stage._repository_validation_ci_wait(item, ctx)
    assert isinstance(submitted, JobRequest)
    assert isinstance(submitted.job, BuildTestJob)
    request = submitted.job.repository_validation_preparation
    assert request is not None
    stage.on_job_done(
        item,
        JobResult(
            ok=False,
            value=RepositoryValidationRuntimeRead(request, failure="runtime_preparation_failed"),
        ),
        ctx,
    )
    assert isinstance(stage._repository_validation_runtime_wait(item, ctx), StageOutcome)
    assert item.payload.get("repository_validation_failure")
    assert isinstance(stage._repository_validation_ci_wait(item, ctx), StageOutcome)


@pytest.mark.parametrize(
    "path,workflow_ci",
    [
        ("src/comet/pool.py", False),
        ("tests/test_ci_workflows.py", False),
        ("tests/test_ci_workflows.py", True),
    ],
)
def test_local_stage_preserves_ci_and_local_eligibility(
    tmp_path: Path, make_work_item: Any, make_ctx: Any, path: str, workflow_ci: bool
) -> None:
    """Retain nightly checks and require CI for the validator that downloads tools."""
    from dataclasses import replace

    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.repository_validation import (
        RepositoryValidationLocalRead,
        RepositoryValidationReceipt,
    )
    from hephaestus.automation.pipeline.stages.base import Continue, StageOutcome

    item, _, ci = _pending_ci_item(tmp_path, make_work_item, (("M", path),))
    plan = ci.request.plan
    workflow = "comet.contract.workflow-contracts"
    receipts = (
        tuple(receipt for receipt in ci.receipts if receipt.check_id == workflow)
        if workflow_ci
        else ci.receipts
        if path == "src/comet/pool.py"
        else ()
    )
    stage, ctx = PrReviewStage(), make_ctx(org="LLM360")
    stage.on_job_done(item, JobResult(ok=True, value=replace(ci, receipts=receipts)), ctx)
    covered = {receipt.check_id for receipt in receipts}
    uncovered = [check for check in plan.checks if check.check_id not in covered]
    expected = Continue(next_state="VALIDATE_WAIT")
    with patch.object(stage, "_route_threads_before_broad_review", return_value=expected) as route:
        if path == "tests/test_ci_workflows.py" and not workflow_ci:
            assert isinstance(stage._repository_validation_ci_wait(item, ctx), StageOutcome)
            assert "repository_validation_runtime_request" not in item.payload
            route.assert_not_called()
            assert item.payload["repository_validation_failure"] == "local_check_ineligible"
            return
        if path == "src/comet/pool.py":
            assert {check.check_id for check in uncovered} >= {
                "comet.contract.control-deployment-contracts",
                "comet.contract.viewer-deployment-contracts",
            }
        for check in uncovered:
            submitted = stage._repository_validation_ci_wait(item, ctx)
            assert isinstance(submitted, JobRequest)
            submitted = _complete_runtime_preparation(stage, item, ctx, submitted)
            execution = submitted.job.repository_validation
            assert execution is not None
            assert execution.check_id == check.check_id != workflow
            receipt = RepositoryValidationReceipt(
                repository=plan.repository,
                pr_number=plan.pr_number,
                plan_id=plan.plan_id,
                check_id=check.check_id,
                reviewed_head=plan.reviewed_head,
                reviewed_base=plan.reviewed_base,
                argv=check.argv,
                source_digests=check.source_digests,
                evidence_kind="local",
                status="success",
            )
            stage.on_job_done(
                item,
                JobResult(ok=True, value=RepositoryValidationLocalRead(execution, receipt=receipt)),
                ctx,
            )
        assert stage._repository_validation_ci_wait(item, ctx) == expected
    route.assert_called_once_with(item, ctx)


def test_terminal_ci_failure_prevents_local_retry(
    tmp_path: Path, make_work_item: Any, make_ctx: Any
) -> None:
    """Do not use local successes to clear a failed admitted CI check."""
    from dataclasses import replace

    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.pipeline.stages.base import StageOutcome

    item, _, ci = _pending_ci_item(tmp_path, make_work_item)
    stage, ctx = PrReviewStage(), make_ctx(org="LLM360")
    failed = replace(ci.receipts[0], status="failed")
    stage.on_job_done(item, JobResult(ok=True, value=replace(ci, receipts=(failed,))), ctx)
    with patch.object(
        stage,
        "_submit_repository_validation_local",
        side_effect=AssertionError("No retry is allowed."),
    ):
        assert isinstance(stage._repository_validation_ci_wait(item, ctx), StageOutcome)


@pytest.mark.parametrize("review_kind", ["analysis", "validation"])
@pytest.mark.parametrize("identity", ["current", "stale", "missing", "legacy"])
def test_reviewer_job_carries_current_repository_validation(
    tmp_path: Path, make_work_item: Any, make_ctx: Any, review_kind: str, identity: str
) -> None:
    """Supply current evidence to both reviewer jobs without a bootstrap grant."""
    import json

    from hephaestus.automation.pipeline.jobs import AgentJob, JobResult

    item, _, receipt = _pending_ci_item(tmp_path, make_work_item)
    stage = PrReviewStage()
    ctx = make_ctx(org="LLM360")
    stage.on_job_done(item, JobResult(ok=True, value=receipt), ctx)
    plan = item.payload["repository_validation_attempt"].plan
    if identity == "stale":
        item.payload["reviewed_pr_proof_generation"] += 1
    elif identity == "missing":
        item.payload.pop("repository_validation_attempt")
    elif identity == "legacy":
        item.repo = "Hephaestus"
    item.payload["review_anchor_correction_complete"] = True
    metadata = {
        "pr_title": "Review the source.",
        "pr_description": "Check the change.",
        "pr_head_sha": plan.reviewed_head,
    }
    with (
        patch.object(
            pr_review_jobs, "source_workspace_binding", return_value=plan.source_workspace
        ),
        patch.object(ctx.github, "pr_review_context", return_value=metadata),
    ):
        result = (
            stage._submit_review_job(item, ctx)
            if review_kind == "analysis"
            else stage._validate_wait(item, ctx)
        )
    assert isinstance(result, JobRequest)
    assert isinstance(result.job, AgentJob)
    encoded = result.job.prompt_kwargs["repository_validation_json"]
    if identity == "legacy":
        assert encoded == ""
        return
    summary = json.loads(encoded)
    if identity != "current":
        assert summary["status"] == "gap"
        assert "receipts" not in summary
        return
    assert summary["status"] == "complete"
    assert summary["plan_id"] == plan.plan_id
    assert summary["reviewed_head"] == plan.reviewed_head
    assert summary["reviewed_base"] == plan.reviewed_base
    assert summary["generation"] == 1
    assert summary["checks"] == [
        {"check_id": check.check_id, "argv": list(check.argv)} for check in plan.checks
    ]
    assert {row["receipt_id"] for row in summary["receipts"]} == {
        row.receipt_id for row in receipt.receipts
    }
    assert all(row["evidence_kind"] == "ci" for row in summary["receipts"])
    assert not item.payload.get("host_verification_bootstrap_json")
    rendered = result.job.prompt_builder(**result.job.prompt_kwargs)
    assert encoded in rendered
