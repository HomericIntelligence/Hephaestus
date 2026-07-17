"""Behavioral regression tests for merge-queue workflow readiness."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
POLICY_DOC = REPO_ROOT / "docs" / "ci" / "required-checks.md"

REQUIRED_WORKFLOWS = ("_required.yml", "test.yml")
DIRECT_REQUIRED_JOBS = {
    "lint",
    "unit-tests",
    "integration-tests",
    "security/dependency-scan",
    "security/secrets-scan",
    "build",
    "schema-validation",
    "deps/version-sync",
    "pr-policy",
}
PULL_REQUEST_TYPES = [
    "opened",
    "synchronize",
    "reopened",
    "ready_for_review",
    "labeled",
    "unlabeled",
    "auto_merge_enabled",
    "auto_merge_disabled",
]


def _load_workflow(name: str) -> dict[Any, Any]:
    """Load a workflow while tolerating PyYAML's YAML 1.1 ``on`` coercion."""
    workflow = yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def _events(workflow: dict[Any, Any]) -> dict[str, Any]:
    """Return the Actions event mapping from a parsed workflow."""
    events = workflow.get("on", workflow.get(True))
    assert isinstance(events, dict)
    return events


@pytest.mark.parametrize("workflow_name", REQUIRED_WORKFLOWS)
def test_required_workflows_run_for_merge_group_checks_requested(workflow_name: str) -> None:
    """Every workflow supplying a required context must run for queue checks."""
    events = _events(_load_workflow(workflow_name))

    assert events.get("merge_group") == {"types": ["checks_requested"]}


def test_required_workflows_preserve_pull_request_and_push_behavior() -> None:
    """Queue readiness must be additive to the existing PR and main-push events."""
    required_events = _events(_load_workflow("_required.yml"))
    test_events = _events(_load_workflow("test.yml"))

    assert required_events["pull_request"] == {"types": PULL_REQUEST_TYPES}
    assert required_events["push"] == {"branches": ["main"]}
    assert test_events["pull_request"] is None
    assert test_events["push"] == {"branches": ["main"]}


def test_required_context_names_remain_stable() -> None:
    """The queue event must reuse the exact contexts already enforced live."""
    required_jobs = _load_workflow("_required.yml")["jobs"]
    direct_contexts = {
        job["name"]
        for job in required_jobs.values()
        if isinstance(job, dict) and job.get("name") in DIRECT_REQUIRED_JOBS
    }

    assert direct_contexts == DIRECT_REQUIRED_JOBS
    assert required_jobs["required-checks-gate"]["name"] == "required-checks-gate"

    test_job = _load_workflow("test.yml")["jobs"]["test"]
    matrix = test_job["strategy"]["matrix"]
    assert matrix["os"] == ["ubuntu-latest"]
    assert matrix["python-version"] == ["3.10", "3.11", "3.12", "3.13"]
    assert matrix["test-type"] == ["unit", "integration"]


def test_pr_policy_posts_queue_context_without_reading_pr_payload() -> None:
    """The direct pr-policy context must succeed when merge_group has no PR payload."""
    pr_policy = _load_workflow("_required.yml")["jobs"]["pr-policy"]

    assert pr_policy["if"] == (
        "github.event_name == 'pull_request' || github.event_name == 'merge_group'"
    )

    steps = pr_policy["steps"]
    queue_steps = [step for step in steps if step.get("if") == "github.event_name == 'merge_group'"]
    assert len(queue_steps) == 1
    assert "pull_request" not in str(queue_steps[0])

    pr_steps = [step for step in steps if step not in queue_steps]
    assert pr_steps
    for step in pr_steps:
        condition = str(step.get("if", ""))
        assert "github.event_name == 'pull_request'" in condition, step.get(
            "name", step.get("uses")
        )


def test_policy_records_staged_activation_boundary_and_exact_parameters() -> None:
    """Repository docs must keep live activation external and policy-exact."""
    policy = POLICY_DOC.read_text(encoding="utf-8")

    for marker in (
        "`merge_group: checks_requested`",
        "Odysseus is the sole activation authority",
        "SQUASH",
        "ALLGREEN",
        "10 queue builds",
        "5 merged entries",
        "minimum 1 entry",
        "5-minute minimum wait",
        "60-minute check timeout",
        "human workflow review",
        "queued smoke",
    ):
        assert marker in policy, f"required-checks policy is missing {marker!r}"
