"""Tests for durable coordinator runtime event classification."""

import pytest

from hephaestus.automation.pipeline.coordinator_runtime import CoordinatorRuntime
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.jobs import GitJob, JobResult


def test_pipeline_config_generates_one_bounded_run_identity() -> None:
    """Each pipeline configuration gets a different UUID4 hex identity."""
    first = PipelineConfig(org="org", repos=["repo"])
    second = PipelineConfig(org="org", repos=["repo"])

    assert first.run_identity != second.run_identity
    assert len(first.run_identity) == 32
    assert all(character in "0123456789abcdef" for character in first.run_identity)


def test_github_failure_has_specific_durable_error_class() -> None:
    """A safe GitHub failure class remains available in the durable event."""
    result = JobResult(
        ok=False,
        error="github_rate_limit",
        value={"failure_kind": "github_rate_limit", "retry_delay_s": 45.0},
    )

    fields = CoordinatorRuntime._job_result_event_fields(result)

    assert fields["error"] == "github_rate_limit"


def test_semantic_rebase_failure_has_specific_durable_error_class() -> None:
    """Semantic validation is distinguishable from an undifferentiated error."""
    result = JobResult(
        ok=False,
        error="rebase semantic validation failed: duplicate ADR number 0027",
        value={"failure_kind": "semantic_validation"},
    )

    fields = CoordinatorRuntime._job_result_event_fields(result)

    assert fields["error"] == "semantic_validation"


def test_publish_lease_failure_has_specific_durable_error_class() -> None:
    """Lease failures retain the safe ownership classification after a push error."""
    result = JobResult(
        ok=False,
        error="publish failed: remote head unchanged",
        value={"failure_kind": "publish_remote_head_unchanged"},
    )

    fields = CoordinatorRuntime._job_result_event_fields(result)

    assert fields["error"] == "publish_remote_head_unchanged"


def test_lock_timeout_event_retains_bounded_contention_evidence() -> None:
    """A lock completion keeps its safe holder and attempt diagnostics."""
    fields = CoordinatorRuntime._job_result_event_fields(
        JobResult(
            ok=False,
            error="lock_timeout",
            value={
                "repository": "repo-a",
                "operation": "sync_checkout",
                "lock_layer": "advisory",
                "lock_path": "/tmp/git-repo-a.lock",
                "configured_lock_wait_s": 2.0,
                "attempt_wait_s": 1.25,
                "run_identity": "run-7",
                "holder_metadata_status": "unverified",
                "holder_metadata_advisory": True,
                "holder_metadata": {
                    "pid": 123,
                    "run_identity": "run-6",
                    "repository": "repo-a",
                    "operation": "commit_push",
                    "acquired_at_unix_s": 42.0,
                },
            },
        )
    )

    assert fields["error"] == "lock_timeout"
    assert fields["lock_contention"]["run_identity"] == "run-7"
    assert fields["lock_contention"]["holder_metadata"]["operation"] == "commit_push"


def test_repository_busy_summary_does_not_report_clone_exhaustion() -> None:
    """The durable failure class keeps repository contention distinct."""
    fields = CoordinatorRuntime._job_result_event_fields(
        JobResult(ok=False, error="repository_busy")
    )

    assert fields["error"] == "repository_busy"
    assert "clone exhausted" not in str(fields)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param("circuit_open", "circuit_open", id="open-circuit"),
        pytest.param(
            "agent_error: provider rejected the request",
            "agent_error",
            id="agent-execution",
        ),
        pytest.param(
            "agent_error: codex_tool_or_provider_failure: mcp_tool_call status=failed",
            "codex_tool_or_provider_failure",
            id="codex-tool-or-provider-failure",
        ),
        pytest.param(
            "agent_error: unknown_provider_failure: secret-token-value",
            "agent_error",
            id="unknown-provider-failure",
        ),
        pytest.param(
            "agent_error: codex_tool_or_provider_failure secret-token-value",
            "agent_error",
            id="provider-failure-without-delimiter",
        ),
        pytest.param("parse failed: ValueError", "parse_error", id="agent-output-parser"),
        pytest.param("review-session-lost", "session_lost", id="lost-session"),
        pytest.param(
            "host_verification_failed: sandbox unavailable",
            "host_verification",
            id="host-verification",
        ),
        pytest.param(
            "source_workspace_ownership_unavailable: "
            "implementation writer branch is an unowned existing branch",
            "source_workspace_ownership_unavailable",
            id="source-workspace-ownership-unavailable",
        ),
        pytest.param("rc=75", "process_exit", id="subprocess-exit"),
        pytest.param(
            "mechanical rebase hit conflicts; resolution required",
            "git_operation",
            id="git-operation",
        ),
    ],
)
def test_safe_worker_error_class_remains_available(error: str, expected: str) -> None:
    """A closed mapping keeps safe failure classes without raw details."""
    fields = CoordinatorRuntime._job_result_event_fields(JobResult(ok=False, error=error))

    assert fields["error"] == expected


def test_specific_worker_error_class_precedes_generic_failure_kind() -> None:
    """A specific safe error shape is more useful than a generic runner class."""
    result = JobResult(
        ok=False,
        error="host_verification_failed: sandbox unavailable",
        value={"failure_kind": "runner"},
    )

    fields = CoordinatorRuntime._job_result_event_fields(result)

    assert fields["error"] == "host_verification"


@pytest.mark.parametrize(
    "error",
    [
        "source_workspace_ownership_unavailable secret-token-value",
        "unexpected failure",
        "provider returned secret-token-value",
        "checkout /private/operator/path is dirty",
    ],
)
def test_unknown_worker_error_remains_generic(error: str) -> None:
    """Unknown or detail-bearing errors do not enter the durable event."""
    fields = CoordinatorRuntime._job_result_event_fields(JobResult(ok=False, error=error))

    assert fields["error"] == "error"


def test_failed_validation_event_keeps_bounded_diagnostics() -> None:
    """Failed validation events retain bounded tails while success events stay quiet."""
    result = JobResult(
        ok=False,
        error="rebase structural validation failed",
        value={"failure_kind": "validation"},
        stdout_tail="duplicate ADR number 0027",
        stderr_tail="pytest diagnostics",
    )

    fields = CoordinatorRuntime._job_result_event_fields(result)

    assert fields["error"] == "validation"
    assert fields["diagnostics"] == {
        "stdout_tail": "duplicate ADR number 0027",
        "stderr_tail": "pytest diagnostics",
    }


def test_event_diagnostics_redact_secret_like_tails() -> None:
    """Durable event diagnostics mask secret-like stdout/stderr before JSONL."""
    bearer = "Bearer " + "abcdef1234567890"
    gh_token = "ghp_" + "1234567890abcdefghijklmnopqrstuvwxyzABCDE"
    result = JobResult(
        ok=False,
        error="rebase structural validation failed",
        value={"failure_kind": "validation"},
        stdout_tail=f"pytest output\nAuthorization: {bearer}",
        stderr_tail=f"git clone https://x-access-token:{gh_token}@github.com/o/r.git",
    )

    fields = CoordinatorRuntime._job_result_event_fields(result)

    diagnostics = fields["diagnostics"]
    assert "abcdef1234567890" not in diagnostics["stdout_tail"]
    assert gh_token not in diagnostics["stderr_tail"]
    assert "redacted" in diagnostics["stdout_tail"]
    assert "redacted" in diagnostics["stderr_tail"]


def test_source_workspace_recovery_is_durable_and_redacted() -> None:
    """A source-workspace recovery event keeps bounded safe fields only."""
    token = "ghp_" + "a" * 36
    result = JobResult(
        ok=False,
        error="source workspace is dirty",
        value={
            "failure_kind": "source_workspace_ownership",
            "source_workspace_recovery": {
                "kind": "dirty_worktree",
                "item_number": 2969,
                "path": "/repo/build/.worktrees/auto-2969-impl",
                "receipt_path": "/repo/state/2969-impl.json",
                "manual_action": f"Use token={token} only after preserving the checkout.",
            },
        },
    )

    fields = CoordinatorRuntime._job_result_event_fields(result)

    assert fields["error"] == "source_workspace_ownership"
    recovery = fields["source_workspace_recovery"]
    assert recovery["kind"] == "dirty_worktree"
    assert recovery["item_number"] == 2969
    assert token not in recovery["manual_action"]
    assert recovery["manual_action"] == "Use token=<redacted> only after preserving the checkout."


def test_rebase_conflict_event_keeps_bounded_classification_and_summary() -> None:
    """Conflict validation events retain only bounded, redacted agent detail."""
    secret = "sk" + "_live_12345678901234567890"
    result = JobResult(
        ok=False,
        error="rebase conflict resolution required: agent made no file changes",
        value={
            "conflict_resolution": "no_edit",
            "agent_summary": f"No changes; token={secret}",
        },
    )

    fields = CoordinatorRuntime._job_result_event_fields(
        result,
        job=GitJob(repo="test/repo", op="validate_rebase_conflict", timeout_s=60),
    )

    assert fields["rebase_conflict_resolution"] == "no_edit"
    summary = fields["rebase_conflict_agent_summary"]
    assert secret not in summary
    assert "redacted" in summary
    assert len(summary) <= 500


def test_unrelated_job_cannot_emit_rebase_conflict_audit_fields() -> None:
    """Only a host conflict-validation job owns conflict audit fields."""
    result = JobResult(
        ok=True,
        value={"conflict_resolution": "resolved_content", "agent_summary": "done"},
    )

    fields = CoordinatorRuntime._job_result_event_fields(result, job=object())

    assert "rebase_conflict_resolution" not in fields
    assert "rebase_conflict_agent_summary" not in fields
