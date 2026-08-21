"""Regression contracts for required checks on merge-queue commits."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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


def _merge_group_policy_step() -> dict[str, Any]:
    """Return the merge-group policy step from the required workflow."""
    required = _load_workflow(REQUIRED_WORKFLOW)
    policy = required["jobs"]["pr-policy"]
    steps = policy["steps"]
    merge_steps = [
        step
        for step in steps
        if step.get("if") == "github.event_name == 'merge_group'" and "run" in step
    ]
    assert len(merge_steps) == 1
    return cast(dict[str, Any], merge_steps[0])


def test_merge_group_pr_policy_revalidates_source_metadata_from_trusted_base() -> None:
    """HEADGREEN groups must re-run policy instead of trusting spoofable check names."""
    required = _load_workflow(REQUIRED_WORKFLOW)
    policy = required["jobs"]["pr-policy"]
    assert policy["permissions"]["checks"] == "read"
    run = str(_merge_group_policy_step()["run"])

    assert "mergeQueueEntry" in run
    assert "entries(first:100, after:$endCursor)" in run
    assert "after:$endCursor" in run
    assert "pageInfo { hasNextPage endCursor }" in run
    assert "totalCount" in run
    assert "$target_entry.baseCommit.oid == $queued_base" in run
    assert "$target_entry.headCommit.oid == $group_head" in run
    assert "$members[.].baseCommit.oid == $members[. - 1].headCommit.oid" in run
    assert "$counts[0] == ($entries | length)" in run
    assert "queue-members.tsv" in run
    assert "while IFS=$'\\t' read -r source_pr source_head" in run
    assert "/check-runs" not in run
    assert "policy-base/scripts/check_conventional_commit.py" in run
    assert "policy-base/scripts/check_dco_signoff.py" in run
    assert "source_head" in run
    assert 'queued_base="${BASH_REMATCH[2]}"' in run
    assert "MERGE_GROUP_SHA" in run

    checkout = next(
        step
        for step in policy["steps"]
        if step.get("if") == "github.event_name == 'merge_group'" and "uses" in step
    )
    assert checkout["with"]["ref"] == "${{ github.event.merge_group.base_sha }}"
    assert checkout["with"]["path"] == "policy-base"


def _run_merge_group_policy(
    tmp_path: Path,
    *,
    ref_base: str,
    entry_base: str,
    event_head: str,
    entry_head: str,
) -> subprocess.CompletedProcess[str]:
    """Execute the actual workflow script against deterministic GitHub fixtures."""
    tmp_path.mkdir()
    source_head = "3" * 40
    queue_entry = {
        "position": 1,
        "baseCommit": {"oid": entry_base},
        "headCommit": {"oid": entry_head},
        "pullRequest": {"number": 42, "state": "OPEN", "headRefOid": source_head},
    }
    queue = [
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 42,
                        "state": "OPEN",
                        "headRefOid": source_head,
                        "mergeQueueEntry": {
                            "position": 1,
                            "baseCommit": {"oid": entry_base},
                            "headCommit": {"oid": entry_head},
                            "mergeQueue": {
                                "entries": {
                                    "totalCount": 1,
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": [queue_entry],
                                }
                            },
                        },
                    }
                }
            }
        }
    ]
    pr = {
        "author": {"login": "octocat"},
        "body": "Closes #42\n",
        "headRefOid": source_head,
        "state": "OPEN",
        "title": "fix(ci): validate queue policy",
    }
    commits = {
        "data": {
            "repository": {
                "pullRequest": {
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "message": "fix(ci): validate queue policy\n\n"
                                    "Signed-off-by: Octo Cat <octo@example.com>"
                                }
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }
    }
    for name, value in (
        ("queue-fixture.json", queue),
        ("pr-fixture.json", pr),
        ("commits-fixture.json", commits),
    ):
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [[ "$*" == *"--paginate --slurp"* ]]; then cat "$QUEUE_FIXTURE"; '
        'elif [[ "$1 $2" == "pr view" ]]; then cat "$PR_FIXTURE"; '
        'elif [[ "$1 $2" == "api graphql" ]]; then cat "$COMMITS_FIXTURE"; '
        "else exit 64; fi\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
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
        "COMMITS_FIXTURE": str(tmp_path / "commits-fixture.json"),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }
    return subprocess.run(
        ["bash", "-c", str(_merge_group_policy_step()["run"])],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_merge_group_policy_binds_queue_base_and_synthetic_head(tmp_path: Path) -> None:
    """The live queue-ref shape succeeds and either SHA mismatch fails closed."""
    base = "1" * 40
    head = "2" * 40

    valid = _run_merge_group_policy(
        tmp_path / "valid", ref_base=base, entry_base=base, event_head=head, entry_head=head
    )
    assert valid.returncode == 0, valid.stderr

    wrong_base = _run_merge_group_policy(
        tmp_path / "wrong-base",
        ref_base="4" * 40,
        entry_base=base,
        event_head=head,
        entry_head=head,
    )
    assert wrong_base.returncode != 0

    wrong_head = _run_merge_group_policy(
        tmp_path / "wrong-head",
        ref_base=base,
        entry_base=base,
        event_head="5" * 40,
        entry_head=head,
    )
    assert wrong_head.returncode != 0


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
