"""Regression contracts for required checks on merge-queue commits."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
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
EXPECTED_SKIP_POLICY = {
    "pull_request": [],
    "push": ["pr-policy"],
    "merge_group": [],
}


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
    for job_id in set(needs) - {"changes-gate", "pr-policy"}:
        job = jobs[job_id]
        job_needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
        assert "changes-gate" in job_needs
        assert job["if"] == code_event_condition


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


def _merge_group_policy_steps() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the queue-resolution and source-validation workflow steps."""
    required = _load_workflow(REQUIRED_WORKFLOW)
    policy = required["jobs"]["pr-policy"]
    merge_steps = {
        step["name"]: step
        for step in policy["steps"]
        if step.get("if") == "github.event_name == 'merge_group'" and "run" in step
    }
    assert set(merge_steps) == {
        "Resolve complete merge-group membership",
        "Revalidate source PR policy",
    }
    return (
        cast(dict[str, Any], merge_steps["Resolve complete merge-group membership"]),
        cast(dict[str, Any], merge_steps["Revalidate source PR policy"]),
    )


def test_merge_group_pr_policy_revalidates_source_metadata_from_trusted_base() -> None:
    """HEADGREEN groups must re-run policy instead of trusting spoofable check names."""
    required = _load_workflow(REQUIRED_WORKFLOW)
    policy = required["jobs"]["pr-policy"]
    assert "checks" not in policy["permissions"]
    resolve, revalidate = _merge_group_policy_steps()
    resolve_run = str(resolve["run"])
    revalidate_run = str(revalidate["run"])

    assert "mergeQueueEntry" in resolve_run
    assert "entries(first:100, after:$endCursor)" in resolve_run
    assert "pageInfo { hasNextPage endCursor }" in resolve_run
    assert "totalCount" in resolve_run
    assert "$target_entry.baseCommit.oid == $queued_base" in resolve_run
    assert "$target_entry.headCommit.oid == $group_head" in resolve_run
    assert "$members[.].baseCommit.oid == $members[. - 1].headCommit.oid" in resolve_run
    assert "$counts[0] == ($entries | length)" in resolve_run
    assert "queue-members.tsv" in resolve_run
    assert "queue_root: $members[0].baseCommit.oid" in resolve_run
    assert 'queued_base="${BASH_REMATCH[2]}"' in resolve_run
    assert "MERGE_GROUP_SHA" in resolve_run

    assert "while IFS=$'\\t' read -r source_pr source_head" in revalidate_run
    assert "/check-runs" not in revalidate_run
    assert "policy-base/scripts/check_conventional_commit.py" in revalidate_run
    assert "policy-base/scripts/check_dco_signoff.py" in revalidate_run
    assert "totalCount" in revalidate_run
    assert "commit { oid message }" in revalidate_run
    assert "headRefOid" in revalidate_run
    assert "cmp -s" in revalidate_run

    checkout = next(
        step
        for step in policy["steps"]
        if step.get("if") == "github.event_name == 'merge_group'" and "uses" in step
    )
    assert checkout["with"]["ref"] == "${{ steps.resolve_queue.outputs.queue_root }}"
    assert checkout["with"]["path"] == "policy-base"


def _run_merge_group_policy(
    tmp_path: Path,
    *,
    ref_base: str,
    entry_base: str,
    event_head: str,
    entry_head: str,
    invalid_second_commit: bool = False,
    mutate_final_metadata: bool = False,
    duplicate_commit_oid: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Execute the actual workflow script against deterministic GitHub fixtures."""
    tmp_path.mkdir(exist_ok=True)
    source_head = "3" * 40
    root_base = "1" * 40
    first_entry = {
        "position": 1,
        "baseCommit": {"oid": root_base},
        "headCommit": {"oid": entry_base},
        "pullRequest": {"number": 41, "state": "OPEN", "headRefOid": source_head},
    }
    target_entry = {
        "position": 2,
        "baseCommit": {"oid": entry_base},
        "headCommit": {"oid": entry_head},
        "pullRequest": {"number": 42, "state": "OPEN", "headRefOid": source_head},
    }
    target: dict[str, Any] = {
        "number": 42,
        "state": "OPEN",
        "headRefOid": source_head,
        "mergeQueueEntry": {
            "position": 2,
            "baseCommit": {"oid": entry_base},
            "headCommit": {"oid": entry_head},
        },
    }

    def queue_page(
        node: dict[str, Any], *, has_next_page: bool, end_cursor: str | None
    ) -> dict[str, Any]:
        """Build one deterministic page of the target queue connection."""
        target_queue_entry = cast(dict[str, Any], target["mergeQueueEntry"])
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        **target,
                        "mergeQueueEntry": {
                            **target_queue_entry,
                            "mergeQueue": {
                                "entries": {
                                    "totalCount": 2,
                                    "pageInfo": {
                                        "hasNextPage": has_next_page,
                                        "endCursor": end_cursor,
                                    },
                                    "nodes": [node],
                                }
                            },
                        },
                    }
                }
            }
        }

    queue = [
        queue_page(first_entry, has_next_page=True, end_cursor="queue-page-1"),
        queue_page(target_entry, has_next_page=False, end_cursor=None),
    ]
    pr = {
        "author": {"login": "octocat"},
        "body": "Closes #42\n",
        "headRefOid": source_head,
        "state": "OPEN",
        "title": "fix(ci): validate queue policy",
    }
    first_commit_oid = "7" * 40
    commits_first = {
        "data": {
            "repository": {
                "pullRequest": {
                    "number": 42,
                    "state": "OPEN",
                    "headRefOid": source_head,
                    "commits": {
                        "totalCount": 2,
                        "nodes": [
                            {
                                "commit": {
                                    "oid": first_commit_oid,
                                    "message": "fix(ci): validate queue policy\n\n"
                                    "Signed-off-by: Octo Cat <octo@example.com>",
                                }
                            }
                        ],
                        "pageInfo": {"hasNextPage": True, "endCursor": "commit-page-1"},
                    },
                }
            }
        }
    }
    second_message = "fix(ci): validate final queue page"
    if not invalid_second_commit:
        second_message += "\n\nSigned-off-by: Octo Cat <octo@example.com>"
    commits_second = {
        "data": {
            "repository": {
                "pullRequest": {
                    "number": 42,
                    "state": "OPEN",
                    "headRefOid": source_head,
                    "commits": {
                        "totalCount": 2,
                        "nodes": [
                            {
                                "commit": {
                                    "oid": first_commit_oid
                                    if duplicate_commit_oid
                                    else source_head,
                                    "message": second_message,
                                }
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        }
    }
    for name, value in (
        ("queue-fixture.json", queue),
        ("pr-fixture.json", pr),
        ("commits-first-fixture.json", commits_first),
        ("commits-second-fixture.json", commits_second),
    ):
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [[ "$*" == *"--paginate --slurp"* ]]; then cat "$QUEUE_FIXTURE"; '
        'elif [[ "$1 $2" == "pr view" ]]; then '
        'count=$(cat "$PR_VIEW_COUNT"); count=$((count + 1)); '
        'printf "%s" "$count" > "$PR_VIEW_COUNT"; '
        'if [[ "$MUTATE_FINAL_METADATA" == "1" && $((count % 2)) -eq 0 ]]; '
        'then jq \'.title = "fix(ci): changed while validating"\' "$PR_FIXTURE"; '
        'else cat "$PR_FIXTURE"; fi; '
        'elif [[ "$1 $2" == "api graphql" ]]; then '
        'source_pr=42; for argument in "$@"; do case "$argument" in '
        'pr=*) source_pr="${argument#pr=}" ;; esac; done; '
        'if [[ "$*" == *"after=commit-page-1"* ]]; then fixture="$COMMITS_SECOND_FIXTURE"; '
        'else fixture="$COMMITS_FIRST_FIXTURE"; fi; '
        'jq --argjson number "$source_pr" '
        "'.data.repository.pullRequest.number = $number' \"$fixture\"; "
        "else exit 64; fi\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    pr_view_count = tmp_path / "pr-view-count"
    pr_view_count.write_text("0", encoding="utf-8")
    policy_scripts = tmp_path / "policy-base" / "scripts"
    policy_scripts.mkdir(parents=True)
    for name in ("check_conventional_commit.py", "check_dco_signoff.py"):
        shutil.copy2(REPO_ROOT / "scripts" / name, policy_scripts / name)

    env = os.environ | {
        "BASE_REF": "refs/heads/main",
        "HEAD_REF": f"refs/heads/gh-readonly-queue/main/pr-42-{ref_base}",
        "MERGE_GROUP_SHA": event_head,
        "REPOSITORY": "HomericIntelligence/Hephaestus",
        "REPO_NAME": "Hephaestus",
        "REPO_OWNER": "HomericIntelligence",
        "RUNNER_TEMP": str(tmp_path),
        "QUEUE_FIXTURE": str(tmp_path / "queue-fixture.json"),
        "PR_FIXTURE": str(tmp_path / "pr-fixture.json"),
        "COMMITS_FIRST_FIXTURE": str(tmp_path / "commits-first-fixture.json"),
        "COMMITS_SECOND_FIXTURE": str(tmp_path / "commits-second-fixture.json"),
        "MUTATE_FINAL_METADATA": "1" if mutate_final_metadata else "0",
        "PR_VIEW_COUNT": str(pr_view_count),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }
    resolve, revalidate = _merge_group_policy_steps()
    github_output = tmp_path / "github-output"
    env["GITHUB_OUTPUT"] = str(github_output)
    resolved = subprocess.run(
        ["bash", "-c", str(resolve["run"])],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    output = github_output.read_text(encoding="utf-8") if github_output.exists() else ""
    if resolved.returncode != 0:
        return resolved, output
    validated = subprocess.run(
        ["bash", "-c", str(revalidate["run"])],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return validated, output


def test_merge_group_policy_binds_queue_base_and_synthetic_head(tmp_path: Path) -> None:
    """The live queue-ref shape succeeds and either SHA mismatch fails closed."""
    base = "6" * 40
    head = "2" * 40

    valid, output = _run_merge_group_policy(
        tmp_path / "valid", ref_base=base, entry_base=base, event_head=head, entry_head=head
    )
    assert valid.returncode == 0, valid.stderr
    assert f"queue_root={'1' * 40}" in output

    wrong_base, _ = _run_merge_group_policy(
        tmp_path / "wrong-base",
        ref_base="4" * 40,
        entry_base=base,
        event_head=head,
        entry_head=head,
    )
    assert wrong_base.returncode != 0

    wrong_head, _ = _run_merge_group_policy(
        tmp_path / "wrong-head",
        ref_base=base,
        entry_base=base,
        event_head="5" * 40,
        entry_head=head,
    )
    assert wrong_head.returncode != 0


def test_merge_group_policy_rejects_invalid_later_commit_page(tmp_path: Path) -> None:
    """A policy violation on page two must not be hidden by a clean first page."""
    result, _ = _run_merge_group_policy(
        tmp_path,
        ref_base="6" * 40,
        entry_base="6" * 40,
        event_head="2" * 40,
        entry_head="2" * 40,
        invalid_second_commit=True,
    )

    assert result.returncode != 0


def test_merge_group_policy_rejects_metadata_drift(tmp_path: Path) -> None:
    """Mutable title/body/author facts must remain identical through validation."""
    result, _ = _run_merge_group_policy(
        tmp_path,
        ref_base="6" * 40,
        entry_base="6" * 40,
        event_head="2" * 40,
        entry_head="2" * 40,
        mutate_final_metadata=True,
    )

    assert result.returncode != 0


def test_merge_group_policy_rejects_duplicate_commit_identity(tmp_path: Path) -> None:
    """Mixed or overlapping pages cannot satisfy the exact commit-set contract."""
    result, _ = _run_merge_group_policy(
        tmp_path,
        ref_base="6" * 40,
        entry_base="6" * 40,
        event_head="2" * 40,
        entry_head="2" * 40,
        duplicate_commit_oid=True,
    )

    assert result.returncode != 0


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
