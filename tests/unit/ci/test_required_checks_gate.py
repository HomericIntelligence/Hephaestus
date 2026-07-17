"""Guard tests for the required-checks-gate aggregator job.

The ``required-checks-gate`` job in ``.github/workflows/_required.yml`` is an
aggregate workflow signal (see ``docs/ci/required-checks.md``). It does not
replace the repository ruleset's direct required contexts, but it MUST fan in
every gating job so the full workflow remains auditable.

These tests turn that structural invariant into a unit-test failure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from hephaestus.ci.required_checks_gate import GATE_JOB, _unwired_jobs

REPO_ROOT = Path(__file__).resolve().parents[3]
REQUIRED_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "_required.yml"
STRICT_PROOF_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "strict-review-proof.yml"

# Jobs intentionally NOT gated by required-checks-gate:
#   - auto-merge-policy: advisory only (see its comment in _required.yml); must
#     not block the independently reviewed manual bootstrap merge during #2054.
#   - required-checks-gate: the gate cannot depend on itself.
EXEMPT_JOBS = frozenset({"auto-merge-policy", GATE_JOB})


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    """Return the parsed ``_required.yml`` workflow document."""
    with open(REQUIRED_WORKFLOW, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return the ``jobs:`` mapping of the required-checks workflow."""
    return workflow["jobs"]


class TestRequiredChecksGate:
    """The gate must aggregate every gating job in _required.yml."""

    def test_gate_job_exists(self, jobs: dict[str, Any]) -> None:
        """_required.yml must define the required-checks-gate job."""
        assert GATE_JOB in jobs, (
            f"{GATE_JOB} job missing from _required.yml — aggregate workflow "
            "coverage would be unavailable; see docs/ci/required-checks.md"
        )

    def test_gate_needs_every_non_exempt_job(self, workflow: dict[str, Any]) -> None:
        """Every job except the exempt set must be in the gate's needs list.

        This is the core invariant: add a job to _required.yml and you MUST add
        it to required-checks-gate.needs, or it disappears from aggregate coverage.
        """
        missing = _unwired_jobs(workflow, EXEMPT_JOBS)
        assert not missing, (
            f"These jobs are not included in {GATE_JOB}.needs and lack aggregate "
            f"coverage: {sorted(missing)}. Add them to the gate's needs list in "
            ".github/workflows/_required.yml (see docs/ci/required-checks.md)."
        )

    def test_gate_does_not_need_exempt_jobs(self, jobs: dict[str, Any]) -> None:
        """The gate must not depend on advisory jobs or itself."""
        gate_needs = set(jobs[GATE_JOB]["needs"])
        wrongly_gated = gate_needs & EXEMPT_JOBS
        assert not wrongly_gated, (
            f"{GATE_JOB}.needs must not include {sorted(wrongly_gated)} — "
            "auto-merge-policy is advisory and the gate cannot depend on itself."
        )

    def test_gate_needs_reference_real_jobs(self, jobs: dict[str, Any]) -> None:
        """Every entry in the gate's needs list must be a real job."""
        gate_needs = set(jobs[GATE_JOB]["needs"])
        unknown = gate_needs - set(jobs)
        assert not unknown, f"{GATE_JOB}.needs references jobs that do not exist: {sorted(unknown)}"

    def test_gate_runs_always(self, jobs: dict[str, Any]) -> None:
        """The gate must use if: always() so it never skips into a deadlock.

        Without always(), the gate could skip whenever a needed conditional
        job skips, reporting neither success nor failure and deadlocking the
        required check.
        """
        gate_if = str(jobs[GATE_JOB].get("if", "")).strip()
        assert "always()" in gate_if, (
            f"{GATE_JOB} must set `if: always()` (got {gate_if!r}) so it does "
            "not skip behind a conditional dependency and deadlock the required check."
        )

    def test_license_scan_is_gated(self, jobs: dict[str, Any]) -> None:
        """license-scan must be a job in _required.yml AND wired into the gate.

        Previously license-scan lived only in security.yml and was NOT wired
        into required-checks-gate, so a PR adding a GPL runtime dependency could
        merge despite check_license_compatibility.py exiting 1 on pull_request.
        See issue #1514.
        """
        assert "license-scan" in jobs, (
            "license-scan job missing from _required.yml; it must be a gating "
            "job so the required-checks-gate blocks merges on license violations"
        )
        gate_needs = set(jobs[GATE_JOB]["needs"])
        assert "license-scan" in gate_needs, (
            "license-scan must be in required-checks-gate.needs so it blocks "
            "merges; see issue #1514 and docs/ci/required-checks.md"
        )

    def test_required_workflow_cannot_replace_code_validation_on_label_events(
        self, workflow: dict[object, Any]
    ) -> None:
        """Label updates must not emit skipped heavy-job contexts for a code head."""
        trigger = workflow[True]["pull_request"]
        assert trigger["types"] == ["opened", "synchronize", "reopened", "ready_for_review"]

    def test_strict_review_proof_uses_a_trusted_base_workflow(self, jobs: dict[str, Any]) -> None:
        """A candidate PR must not control the executable merge-authorization verifier."""
        with open(STRICT_PROOF_WORKFLOW, encoding="utf-8") as f:
            proof_workflow = yaml.safe_load(f)
        trigger = proof_workflow[True]["pull_request_target"]
        assert {"labeled", "synchronize"} <= set(trigger["types"])
        assert trigger["branches"] == ["main"]
        proof = proof_workflow["jobs"]["strict-review-proof"]
        assert proof["name"] == "publish-strict-review-proof"
        assert proof_workflow["permissions"] == {
            "contents": "read",
            "pull-requests": "read",
            "statuses": "write",
        }
        assert proof["env"]["EVENT_HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
        assert proof["env"]["AUTOMATION_LOGIN"] == "${{ vars.HEPHAESTUS_AUTOMATION_LOGIN }}"
        steps = proof["steps"]
        pending = steps[0]
        assert pending["name"] == "Invalidate any earlier strict-review proof for this head"
        assert "state=pending" in pending["run"]
        assert "/statuses/$EVENT_HEAD_SHA" in pending["run"]
        rendered_steps = "\n".join(str(step.get("run", "")) for step in steps)
        assert "strict_review_proof" in rendered_steps
        assert "issues/$PR_NUMBER/comments" in rendered_steps
        assert "headRefOid" in rendered_steps
        assert "/statuses/$EVENT_HEAD_SHA" in rendered_steps
        assert 'context="strict-review-proof"' in rendered_steps
        assert 'state="$status_state"' in rendered_steps
        assert "uv sync --no-default-groups --locked" in rendered_steps
        checkout = next(
            step for step in steps if step.get("uses", "").startswith("actions/checkout")
        )
        assert checkout["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
        assert "strict-review-proof" not in jobs[GATE_JOB]["needs"]

    def test_gate_assertion_fires_on_unwired_job(self) -> None:
        """Negative-path: the invariant check must flag a job absent from needs:.

        Drive the SAME ``_unwired_jobs()`` helper the real guard uses with a
        synthetic workflow that introduces a gating job not listed in
        ``required-checks-gate.needs``, and verify the gap is detected. Sharing
        the helper guards against the guard and its test silently diverging
        (issues #1315, #1338).
        """
        synthetic_wf: dict[str, Any] = {
            "jobs": {
                GATE_JOB: {
                    "needs": ["job-a"],
                    "if": "always()",
                    "runs-on": "ubuntu-24.04",
                    "steps": [],
                },
                "job-a": {"runs-on": "ubuntu-24.04", "steps": []},
                "job-b": {"runs-on": "ubuntu-24.04", "steps": []},  # intentionally unwired
            }
        }

        missing = _unwired_jobs(synthetic_wf, EXEMPT_JOBS)

        assert "job-b" in missing, (
            "Expected _unwired_jobs() to detect 'job-b' as unwired from "
            f"{GATE_JOB}.needs, but missing={sorted(missing)}"
        )
