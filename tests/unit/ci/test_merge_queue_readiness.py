"""Behavioral regression tests for merge-queue workflow readiness."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

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


def _changes_gate_code_event(action: str, tmp_path: Path) -> bool:
    """Execute the real changes-gate script and return its code-event verdict."""
    gate = _load_workflow("_required.yml")["jobs"]["changes-gate"]
    decide = next(step for step in gate["steps"] if step.get("id") == "decide")
    output = tmp_path / "github-output"

    subprocess.run(
        ["bash", "-c", decide["run"]],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "ACTION": action, "GITHUB_OUTPUT": str(output)},
    )

    values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    return values["code_event"] == "true"


@pytest.mark.parametrize("workflow_name", REQUIRED_WORKFLOWS)
def test_required_workflows_do_not_run_for_merge_group(workflow_name: str) -> None:
    """Queue checks are served solely by the fast merge-queue smoke workflow.

    Re-running the full required matrix per queue entry serialized on runner
    slots (70-90 min per merge). merge_group is therefore removed here and
    handled ONLY by merge-queue-smoke.yml, whose single `merge-queue-smoke`
    job is the sole queue-side check. PR-side CI is untouched.
    """
    events = _events(_load_workflow(workflow_name))

    assert "merge_group" not in events


def test_merge_queue_smoke_is_the_only_merge_group_workflow() -> None:
    """Exactly one workflow handles merge_group, with one fast smoke job."""
    smoke_events = _events(_load_workflow("merge-queue-smoke.yml"))
    assert smoke_events == {"merge_group": {"types": ["checks_requested"]}}

    smoke_jobs = _load_workflow("merge-queue-smoke.yml")["jobs"]
    assert list(smoke_jobs) == ["merge-queue-smoke"]
    assert smoke_jobs["merge-queue-smoke"]["name"] == "merge-queue-smoke"
    assert int(smoke_jobs["merge-queue-smoke"]["timeout-minutes"]) <= 5

    merge_group_workflows = [
        path.name
        for path in sorted(WORKFLOW_DIR.glob("*.yml"))
        if "merge_group" in _events(_load_workflow(path.name))
    ]
    assert merge_group_workflows == ["merge-queue-smoke.yml"]


def test_required_workflows_preserve_pull_request_and_push_behavior() -> None:
    """Queue readiness must be additive to the existing PR and main-push events."""
    required_events = _events(_load_workflow("_required.yml"))
    test_events = _events(_load_workflow("test.yml"))

    assert required_events["pull_request"] == {"types": PULL_REQUEST_TYPES}
    assert required_events["push"] == {"branches": ["main"]}
    assert test_events["pull_request"] is None
    assert test_events["push"] == {"branches": ["main"]}


@pytest.mark.parametrize(
    "action",
    ["", "opened", "synchronize", "reopened", "ready_for_review", "checks_requested", "edited"],
)
def test_code_and_merge_group_actions_run_heavy_jobs(action: str, tmp_path: Path) -> None:
    """The gate never permits a skipped heavy-job context for a shared head."""
    assert _changes_gate_code_event(action, tmp_path) is True


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
