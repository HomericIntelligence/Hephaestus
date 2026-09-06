"""Tests for durable coordinator runtime event classification."""

import pytest

from hephaestus.automation.pipeline.coordinator_runtime import CoordinatorRuntime
from hephaestus.automation.pipeline.jobs import JobResult


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


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param("circuit_open", "circuit_open", id="open-circuit"),
        pytest.param(
            "agent_error: provider rejected the request",
            "agent_error",
            id="agent-execution",
        ),
        pytest.param("parse failed: ValueError", "parse_error", id="agent-output-parser"),
        pytest.param("review-session-lost", "session_lost", id="lost-session"),
        pytest.param(
            "host_verification_failed: sandbox unavailable",
            "host_verification",
            id="host-verification",
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
