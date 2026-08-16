"""Tests for durable coordinator runtime event classification."""

from hephaestus.automation.pipeline.coordinator_runtime import CoordinatorRuntime
from hephaestus.automation.pipeline.jobs import JobResult


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
