"""Regression contracts for required checks on merge-queue commits."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
REQUIRED_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "_required.yml"
TEST_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
CLASSIC_REQUIRED_CONTEXTS = {
    "required-checks-gate",
    "test (ubuntu-latest, 3.13, integration)",
    "test (ubuntu-latest, 3.13, unit)",
}
RULESET_REQUIRED_CONTEXTS = {
    "build",
    "deps/version-sync",
    "integration-tests",
    "lint",
    "pr-policy",
    "schema-validation",
    "security/dependency-scan",
    "security/secrets-scan",
    "unit-tests",
}


def _load_workflow(path: Path) -> dict[str, Any]:
    """Load workflow YAML without YAML 1.1 coercing the ``on`` key to true."""
    return cast(
        dict[str, Any],
        yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader),
    )


def test_every_required_context_workflow_runs_for_merge_groups() -> None:
    """Synthetic queue commits must emit the same required contexts as PR heads."""
    for path in (REQUIRED_WORKFLOW, TEST_WORKFLOW):
        workflow = _load_workflow(path)

        assert workflow["on"]["merge_group"]["types"] == ["checks_requested"], path.name


def test_required_context_names_and_aggregate_membership_are_exact() -> None:
    """The documented required contexts must be emitted and fully aggregated."""
    required = _load_workflow(REQUIRED_WORKFLOW)
    jobs = required["jobs"]
    names_to_ids = {job.get("name", job_id): job_id for job_id, job in jobs.items()}

    assert names_to_ids.keys() >= RULESET_REQUIRED_CONTEXTS
    assert names_to_ids["required-checks-gate"] == "required-checks-gate"
    assert set(jobs["required-checks-gate"]["needs"]) == set(jobs) - {"required-checks-gate"}

    test = _load_workflow(TEST_WORKFLOW)["jobs"]["test"]
    matrix = test["strategy"]["matrix"]
    expanded = {
        f"test ({os_name}, {python}, {test_type})"
        for os_name in matrix["os"]
        for python in matrix["python-version"]
        for test_type in matrix["test-type"]
    }
    assert expanded == CLASSIC_REQUIRED_CONTEXTS - {"required-checks-gate"}


def test_merge_group_pr_policy_revalidates_the_source_pr_check() -> None:
    """HEADGREEN groups must inherit every source PR's exact policy result."""
    required = _load_workflow(REQUIRED_WORKFLOW)
    policy = required["jobs"]["pr-policy"]
    assert policy["permissions"]["checks"] == "read"
    steps = policy["steps"]
    merge_steps = [step for step in steps if step.get("if") == "github.event_name == 'merge_group'"]

    assert len(merge_steps) == 1
    run = merge_steps[0]["run"]
    assert "mergeQueueEntry" in run
    assert "entries(first:100, after:$endCursor)" in run
    assert "after:$endCursor" in run
    assert "pageInfo { hasNextPage endCursor }" in run
    assert "totalCount" in run
    assert "$target.headRefOid == $target_head" in run
    assert "$target_entry.headCommit.oid == $group_head" in run
    assert "$members[.].baseCommit.oid == $members[. - 1].headCommit.oid" in run
    assert "$counts[0] == ($entries | length)" in run
    assert "queue-members.tsv" in run
    assert "while IFS=$'\\t' read -r source_pr source_head" in run
    assert "commits/${source_head}/check-runs" in run
    assert run.count("--paginate --slurp") == 2
    assert "total_count" in run
    assert 'select(.name == "pr-policy")' in run
    assert "select(.app.id == 15368)" in run
    assert '.status == "completed"' in run
    assert '.conclusion == "success"' in run
    assert "($policy | length) > 0" in run
    assert "all($policy[];" in run
    assert "source_head" in run
    assert 'queued_head="${BASH_REMATCH[2]}"' in run
    assert "MERGE_GROUP_SHA" in run
    assert 'echo "PR policy applies' not in run


def test_shell_jobs_use_the_same_versioned_ci_image_as_local_checks() -> None:
    """Bats and ShellCheck must not drift between Ubuntu and the local image."""
    required = _load_workflow(REQUIRED_WORKFLOW)
    shellcheck = str(required["jobs"]["shellcheck"]["steps"])
    shell_tests = str(required["jobs"]["shell-tests"]["steps"])

    assert "apt-get" not in shellcheck
    assert "apt-get" not in shell_tests
    assert "hephaestus-ci:local" in shellcheck
    assert "hephaestus-ci:local" in shell_tests
    assert "scripts/run_ci_local.sh shellcheck" in shellcheck
    assert "scripts/run_ci_local.sh shell-tests" in shell_tests
    assert "CONTAINER_ENGINE" in shellcheck
    assert "CONTAINER_ENGINE" in shell_tests
