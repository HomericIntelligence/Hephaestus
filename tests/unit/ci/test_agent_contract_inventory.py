"""Structural inventory of Athena agent-contract direct callers (issue #2853).

Every protected-event or publish/release/deploy workflow must be a direct
caller of the protected immutable Athena provider. This test pins the
authoritative inventory, checks each in-scope workflow for exactly one minimal
``agent-contract`` reusable-call job, and graph-walks every recorded terminal
aggregate and state-changing writer job to prove the contract job is in its
transitive needs closure. A new workflow file, a removed in-scope workflow, a
drifting dependency edge, or a new write surface fails this test.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

CONTRACT_TAG = "agent-contract-v1.0.0"
CONTRACT_USES = f"HomericIntelligence/Athena/.github/workflows/_agent-contract.yml@{CONTRACT_TAG}"
CONTRACT_JOB_ID = "agent-contract"

# Authoritative direct-caller inventory (issue #2853 author-owned amendment).
# ``closure_targets`` names each workflow's terminal aggregates and
# state-changing writer jobs that must hold the contract job in their
# transitive needs closure.
IN_SCOPE: dict[str, dict[str, Any]] = {
    "_required.yml": {"closure_targets": {"required-checks-gate"}},
    "auto-tag.yml": {"closure_targets": {"auto-tag"}},
    "contract.yml": {"closure_targets": {"github-contract"}},
    "nightly-tests.yml": {"closure_targets": {"notify-failure"}},
    "performance.yml": {"closure_targets": {"notify-failure"}},
    "release.yml": {"closure_targets": {"build-and-publish", "deploy-docs"}},
    "security.yml": {"closure_targets": {"notify-failure"}},
}
# Issue/comment-only workflows: they cannot publish and stay outside the
# contract surface. If one gains a publish or class-changing surface, the
# file-set or trigger checks below fail until the inventory is updated.
EXCLUDED: dict[str, str] = {
    "auto-label-needs-plan.yml": "issue/comment-only label writer",
    "auto-label-severity.yml": "issue/comment-only label writer",
}


def _load_workflow(name: str) -> dict[str, Any]:
    """Load workflow YAML without YAML 1.1 coercing the ``on`` key to true."""
    path = WORKFLOWS_DIR / name
    return cast(
        dict[str, Any],
        yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader),
    )


def _needs_list(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs")
    if needs is None:
        return []
    return list(needs) if isinstance(needs, list) else [needs]


def _transitive_needs(jobs: dict[str, Any], start: str) -> set[str]:
    """Return every job in the transitive needs closure of ``start``."""
    visited: set[str] = set()
    stack = list(_needs_list(jobs[start]))
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        stack.extend(_needs_list(jobs[current]))
    return visited


def _terminal_jobs(jobs: dict[str, Any]) -> set[str]:
    """Return jobs that no other job depends on."""
    needed: set[str] = set()
    for job in jobs.values():
        needed.update(_needs_list(job))
    return set(jobs) - needed


def _write_grant_jobs(workflow: dict[str, Any]) -> set[str]:
    """Return jobs whose effective permissions include any write grant."""
    workflow_permissions = workflow.get("permissions") or {}
    writers: set[str] = set()
    for job_id, job in workflow["jobs"].items():
        if "uses" in job:
            # A reusable-call job has no steps of its own.
            continue
        permissions = job.get("permissions") or workflow_permissions
        levels = {str(level) for level in permissions.values()}
        if levels - {"read", "none"}:
            writers.add(job_id)
    return writers


def _assert_minimal_contract_job(job: dict[str, Any]) -> None:
    """Assert the exact read-only, secretless provider job contract."""
    assert job["uses"] == CONTRACT_USES
    assert job["permissions"] == {"contents": "read"}
    forbidden = {"with", "secrets", "if", "steps", "runs-on", "strategy", "name"}
    assert forbidden.isdisjoint(job)


def test_inventory_file_set_exact() -> None:
    """The workflow set is exactly the recorded in-scope and excluded files."""
    on_disk = {path.name for path in WORKFLOWS_DIR.glob("*.yml")} | {
        path.name for path in WORKFLOWS_DIR.glob("*.yaml")
    }

    assert on_disk == set(IN_SCOPE) | set(EXCLUDED)


def test_inventory_excluded_workflows_carry_no_contract_job() -> None:
    """Excluded issue/comment-only workflows must not gain a contract job."""
    for name in EXCLUDED:
        jobs = _load_workflow(name)["jobs"]

        for job_id, job in jobs.items():
            assert job.get("uses", "") != CONTRACT_USES, (name, job_id)


@pytest.mark.parametrize("name", sorted(IN_SCOPE), ids=sorted(IN_SCOPE))
def test_inventory_each_workflow_has_one_minimal_contract_job(name: str) -> None:
    """Each direct caller owns exactly one minimal provider reusable call."""
    jobs = _load_workflow(name)["jobs"]
    contract_jobs = [
        job_id for job_id, job in jobs.items() if job.get("uses", "").endswith(f"@{CONTRACT_TAG}")
    ]

    assert contract_jobs == [CONTRACT_JOB_ID]
    _assert_minimal_contract_job(jobs[CONTRACT_JOB_ID])


@pytest.mark.parametrize("name", sorted(IN_SCOPE), ids=sorted(IN_SCOPE))
def test_inventory_recorded_closure_targets_hold(name: str) -> None:
    """Each recorded aggregate and writer is gated by the contract job."""
    jobs = _load_workflow(name)["jobs"]

    for target in IN_SCOPE[name]["closure_targets"]:
        assert target in jobs, (name, target)
        closure = _transitive_needs(jobs, target)
        assert CONTRACT_JOB_ID in closure, (name, target, sorted(closure))


@pytest.mark.parametrize("name", sorted(IN_SCOPE), ids=sorted(IN_SCOPE))
def test_inventory_every_writer_and_terminal_job_is_gated(name: str) -> None:
    """Each terminal aggregate and write job holds the contract in its closure."""
    workflow = _load_workflow(name)
    jobs = workflow["jobs"]
    ungated = {
        job_id
        for job_id in _terminal_jobs(jobs) | _write_grant_jobs(workflow)
        if job_id != CONTRACT_JOB_ID and CONTRACT_JOB_ID not in _transitive_needs(jobs, job_id)
    }

    assert ungated == set()


def test_inventory_guard_detects_missing_closure_edge() -> None:
    """The guard fails when a recorded target loses the contract edge."""
    workflow = copy.deepcopy(_load_workflow("performance.yml"))
    workflow["jobs"]["notify-failure"]["needs"] = []

    jobs = workflow["jobs"]
    ungated = {
        job_id
        for job_id in _terminal_jobs(jobs) | _write_grant_jobs(workflow)
        if job_id != CONTRACT_JOB_ID and CONTRACT_JOB_ID not in _transitive_needs(jobs, job_id)
    }

    assert "notify-failure" in ungated


def test_inventory_guard_detects_new_write_surface() -> None:
    """A new unpinned writer job fails the terminal/writer gate."""
    workflow = copy.deepcopy(_load_workflow("performance.yml"))
    workflow["jobs"]["deploy-new"] = {
        "runs-on": "ubuntu-24.04",
        "permissions": {"contents": "write"},
    }

    jobs = workflow["jobs"]
    ungated = {
        job_id
        for job_id in _terminal_jobs(jobs) | _write_grant_jobs(workflow)
        if job_id != CONTRACT_JOB_ID and CONTRACT_JOB_ID not in _transitive_needs(jobs, job_id)
    }

    assert "deploy-new" in ungated
