"""Regression contracts for required checks on merge-queue commits.

The aggregate ``required-checks-gate`` fans in every job in ``_required.yml``,
including the ``agent-contract`` governance job added for issue #2853. The
provider job is a minimal read-only reusable call: it has no ``needs`` and no
code-event condition, so the conditional-graph invariant must exempt it. The
Athena contract content of the job is asserted in
``tests/unit/docs/test_agent_contract.py``.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
REQUIRED_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "_required.yml"
RULESET_REQUIRED_CONTEXTS = {
    "deps/version-sync",
    "lint",
    "pr-policy",
    "schema-validation",
    "security/dependency-scan",
    "security/secrets-scan",
}
EXPECTED_SKIP_POLICY = {
    "pull_request": [],
    "push": ["pr-policy"],
    "merge_group": [],
}

# Jobs exempt from the code-event conditional graph: the condition source, the
# event-only policy job, and the read-only governance provider job (#2853).
CONDITION_EXEMPT_JOBS = {"changes-gate", "pr-policy", "agent-contract"}


def _load_workflow(path: Path) -> dict[str, Any]:
    """Load workflow YAML without YAML 1.1 coercing the ``on`` key to true."""
    return cast(
        dict[str, Any],
        yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader),
    )


def _green_required_needs() -> dict[str, dict[str, Any]]:
    """Return one successful result for each aggregate dependency."""
    jobs = _load_workflow(REQUIRED_WORKFLOW)["jobs"]
    return {
        job_id: {"result": "success", "outputs": {}}
        for job_id in jobs["required-checks-gate"]["needs"]
    }


def _run_required_gate(
    needs: object,
    *,
    event_name: str,
    allowed_skips: dict[str, list[str]] | None = None,
    serialized_results: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the aggregate's real verdict step with controlled job results."""
    gate = _load_workflow(REQUIRED_WORKFLOW)["jobs"]["required-checks-gate"]
    step = gate["steps"][0]
    if allowed_skips is not None:
        step["env"]["ALLOWED_SKIPS"] = json.dumps(allowed_skips)
    env = os.environ | {
        "GITHUB_EVENT_NAME": event_name,
        "RESULTS": json.dumps(needs) if serialized_results is None else serialized_results,
    }
    env.update(
        {name: str(value) for name, value in step.get("env", {}).items() if "${{" not in str(value)}
    )
    return subprocess.run(
        ["bash", "-c", str(step["run"])],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _assert_required_gate_contract(workflow: dict[str, Any]) -> None:
    """Check that the aggregate result policy matches the workflow job graph."""
    jobs = workflow["jobs"]
    gate = jobs["required-checks-gate"]
    needs = gate["needs"]
    step = gate["steps"][0]
    expected_jobs = json.loads(step["env"]["EXPECTED_JOBS"])
    allowed_skips = json.loads(step["env"]["ALLOWED_SKIPS"])

    assert gate["if"] == "always()"
    assert needs == expected_jobs
    assert len(needs) == len(set(needs))
    assert set(needs) == set(jobs) - {"required-checks-gate"}
    assert allowed_skips == EXPECTED_SKIP_POLICY
    assert set().union(*map(set, allowed_skips.values())) <= set(needs)

    code_event_condition = "needs.changes-gate.outputs.code_event == 'true'"
    changes_gate = jobs["changes-gate"]
    assert changes_gate.get("if") is None
    assert changes_gate["outputs"] == {"code_event": "${{ steps.decide.outputs.code_event }}"}
    assert changes_gate["steps"][0]["id"] == "decide"
    assert 'echo "code_event=true"' in changes_gate["steps"][0]["run"]
    assert "needs" not in jobs["pr-policy"]
    assert jobs["pr-policy"]["if"] == (
        "github.event_name == 'pull_request' || github.event_name == 'merge_group'"
    )
    for job_id in set(needs) - CONDITION_EXEMPT_JOBS:
        job = jobs[job_id]
        job_needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
        assert "changes-gate" in job_needs
        assert job["if"] == code_event_condition


def test_every_required_context_workflow_runs_for_merge_groups() -> None:
    """Synthetic queue commits must emit the same required contexts as PR heads."""
    workflow = _load_workflow(REQUIRED_WORKFLOW)

    assert workflow["on"]["merge_group"]["types"] == ["checks_requested"]


def test_required_context_names_and_aggregate_membership_are_exact() -> None:
    """The documented required contexts must be emitted and fully aggregated."""
    required = _load_workflow(REQUIRED_WORKFLOW)
    jobs = required["jobs"]
    names_to_ids = {job.get("name", job_id): job_id for job_id, job in jobs.items()}

    assert names_to_ids.keys() >= RULESET_REQUIRED_CONTEXTS
    assert names_to_ids["required-checks-gate"] == "required-checks-gate"
    assert set(jobs["required-checks-gate"]["needs"]) == set(jobs) - {"required-checks-gate"}


def test_required_gate_policy_matches_the_complete_job_graph() -> None:
    """The runtime census, skip policy, and conditional graph must stay aligned."""
    _assert_required_gate_contract(_load_workflow(REQUIRED_WORKFLOW))


@pytest.mark.parametrize("event_name", ["pull_request", "push", "merge_group"])
def test_required_gate_accepts_all_success_for_supported_events(event_name: str) -> None:
    """Every supported event must accept the complete successful result set."""
    result = _run_required_gate(_green_required_needs(), event_name=event_name)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("event_name", ["pull_request", "push", "merge_group"])
def test_required_gate_rejects_unallowlisted_skipped_dependency(event_name: str) -> None:
    """A dependency-induced skip cannot satisfy the required aggregate."""
    needs = _green_required_needs()
    needs["lint"]["result"] = "skipped"

    result = _run_required_gate(needs, event_name=event_name)

    assert result.returncode != 0
    assert "lint" in result.stdout


@pytest.mark.parametrize("result_name", ["failure", "cancelled", "neutral", "timed_out"])
def test_required_gate_rejects_every_non_success_result(result_name: str) -> None:
    """Failure, cancellation, and unknown outcomes must fail closed."""
    needs = _green_required_needs()
    needs["lint"]["result"] = result_name

    result = _run_required_gate(needs, event_name="pull_request")

    assert result.returncode != 0
    assert result_name in result.stdout


@pytest.mark.parametrize("event_name", ["pull_request", "merge_group"])
def test_required_gate_rejects_pr_policy_skip_outside_push(event_name: str) -> None:
    """The one allowed skip must remain event-specific."""
    needs = _green_required_needs()
    needs["pr-policy"]["result"] = "skipped"

    result = _run_required_gate(needs, event_name=event_name)

    assert result.returncode != 0
    assert "pr-policy" in result.stdout


def test_required_gate_allows_pr_policy_skip_on_push() -> None:
    """A main push has no pull request and can skip only ``pr-policy``."""
    needs = _green_required_needs()
    needs["pr-policy"]["result"] = "skipped"

    result = _run_required_gate(needs, event_name="push")

    assert result.returncode == 0, result.stdout + result.stderr


def test_required_gate_rejects_missing_dependency_result() -> None:
    """A missing dependency cannot disappear from the aggregate census."""
    needs = _green_required_needs()
    del needs["lint"]

    result = _run_required_gate(needs, event_name="pull_request")

    assert result.returncode != 0
    assert "missing" in result.stdout
    assert "lint" in result.stdout


def test_required_gate_rejects_unknown_dependency() -> None:
    """An unexpected result cannot expand the aggregate census."""
    needs = _green_required_needs()
    needs["unknown-job"] = {"result": "success", "outputs": {}}

    result = _run_required_gate(needs, event_name="pull_request")

    assert result.returncode != 0
    assert "unexpected" in result.stdout
    assert "unknown-job" in result.stdout


def test_required_gate_rejects_stale_skip_allowlist_at_runtime() -> None:
    """The real gate must reject a skip policy for an unknown job."""
    allowed_skips = {event: list(job_names) for event, job_names in EXPECTED_SKIP_POLICY.items()}
    allowed_skips["push"].append("unknown-job")

    result = _run_required_gate(
        _green_required_needs(),
        event_name="pull_request",
        allowed_skips=allowed_skips,
    )

    assert result.returncode != 0
    assert "skip policy references unknown jobs" in result.stdout
    assert "unknown-job" in result.stdout


def test_required_gate_rejects_missing_result_field() -> None:
    """A malformed dependency object cannot become implicit success."""
    needs = _green_required_needs()
    needs["lint"] = {"outputs": {}}

    result = _run_required_gate(needs, event_name="pull_request")

    assert result.returncode != 0
    assert "missing-or-invalid" in result.stdout


@pytest.mark.parametrize(
    ("dependency", "expected_message"),
    [
        ([], "missing-or-invalid"),
        ({"result": None}, "missing-or-invalid"),
        ({"result": 1}, "missing-or-invalid"),
    ],
)
def test_required_gate_rejects_malformed_dependency_record(
    dependency: object,
    expected_message: str,
) -> None:
    """A malformed dependency record or result type must fail closed."""
    needs: dict[str, Any] = _green_required_needs()
    needs["lint"] = dependency

    result = _run_required_gate(needs, event_name="pull_request")

    assert result.returncode != 0
    assert expected_message in result.stdout


@pytest.mark.parametrize(
    ("serialized_results", "expected_message"),
    [
        ("{not-json", "invalid gate input"),
        ("[]", "needs must be an object"),
        ("null", "needs must be an object"),
    ],
)
def test_required_gate_rejects_malformed_results_input(
    serialized_results: str,
    expected_message: str,
) -> None:
    """Malformed JSON and non-object result values must fail closed."""
    result = _run_required_gate(
        _green_required_needs(),
        event_name="pull_request",
        serialized_results=serialized_results,
    )

    assert result.returncode != 0
    assert expected_message in result.stdout


def test_required_gate_rejects_dependency_skip_cascade() -> None:
    """A failed condition source cannot make dependent skips acceptable."""
    needs = _green_required_needs()
    needs["changes-gate"]["result"] = "failure"
    for job_id in needs.keys() - {"changes-gate", "pr-policy"}:
        needs[job_id]["result"] = "skipped"

    result = _run_required_gate(needs, event_name="pull_request")

    assert result.returncode != 0
    assert "changes-gate" in result.stdout
    assert "lint" in result.stdout


def test_required_gate_rejects_unsupported_event() -> None:
    """A new trigger must define its skip policy before the gate accepts it."""
    result = _run_required_gate(_green_required_needs(), event_name="workflow_dispatch")

    assert result.returncode != 0
    assert "unsupported event" in result.stdout


def test_required_gate_guard_detects_aggregate_needs_drift() -> None:
    """The structural guard must detect a dependency removed from ``needs``."""
    workflow = copy.deepcopy(_load_workflow(REQUIRED_WORKFLOW))
    workflow["jobs"]["required-checks-gate"]["needs"].remove("lint")

    with pytest.raises(AssertionError):
        _assert_required_gate_contract(workflow)


def test_required_gate_guard_detects_stale_skip_allowlist() -> None:
    """The structural guard must reject a skip entry for a removed job."""
    workflow = copy.deepcopy(_load_workflow(REQUIRED_WORKFLOW))
    gate = workflow["jobs"]["required-checks-gate"]
    gate["steps"][0]["env"]["ALLOWED_SKIPS"] = json.dumps(
        EXPECTED_SKIP_POLICY | {"push": ["pr-policy", "removed-job"]}
    )

    with pytest.raises(AssertionError):
        _assert_required_gate_contract(workflow)


def test_required_gate_guard_detects_condition_bypass() -> None:
    """A new conditional bypass must fail the structural contract."""
    workflow = copy.deepcopy(_load_workflow(REQUIRED_WORKFLOW))
    workflow["jobs"]["lint"]["if"] = "false"

    with pytest.raises(AssertionError):
        _assert_required_gate_contract(workflow)


def test_required_gate_guard_detects_missing_condition_dependency() -> None:
    """A heavy job must declare the dependency that supplies its condition."""
    workflow = copy.deepcopy(_load_workflow(REQUIRED_WORKFLOW))
    workflow["jobs"]["lint"]["needs"] = []

    with pytest.raises(AssertionError):
        _assert_required_gate_contract(workflow)


def test_required_gate_guard_detects_pr_policy_dependency() -> None:
    """The explicit event-only policy job must remain independent."""
    workflow = copy.deepcopy(_load_workflow(REQUIRED_WORKFLOW))
    workflow["jobs"]["pr-policy"]["needs"] = "changes-gate"

    with pytest.raises(AssertionError):
        _assert_required_gate_contract(workflow)


def test_required_gate_guard_detects_code_event_output_drift() -> None:
    """The condition source must keep its exact output binding."""
    workflow = copy.deepcopy(_load_workflow(REQUIRED_WORKFLOW))
    workflow["jobs"]["changes-gate"]["outputs"]["code_event"] = "false"

    with pytest.raises(AssertionError):
        _assert_required_gate_contract(workflow)
