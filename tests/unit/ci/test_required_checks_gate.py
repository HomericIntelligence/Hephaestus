"""Guard tests for the required-checks-gate aggregator job.

The ``required-checks-gate`` job in ``.github/workflows/_required.yml`` is the
single branch-protection required status check for that workflow (see
``docs/ci/required-checks.md``). Branch protection requires only that one
context, so the gate MUST fan in every gating job — if a new job is added to
``_required.yml`` without being wired into the gate's ``needs:`` list, it would
silently stop blocking merges (exactly the failure mode that let a red ``lint``
job reach ``main``; issue #1315).

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

# Jobs intentionally NOT gated by required-checks-gate:
#   - auto-merge-policy: advisory only (see its comment in _required.yml); must
#     not block merges or it would contradict the state:implementation-go arming
#     contract.
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
            f"{GATE_JOB} job missing from _required.yml — it is the single "
            "branch-protection required check; see docs/ci/required-checks.md"
        )

    def test_gate_needs_every_non_exempt_job(self, workflow: dict[str, Any]) -> None:
        """Every job except the exempt set must be in the gate's needs list.

        This is the core invariant: add a job to _required.yml and you MUST add
        it to required-checks-gate.needs, or it stops gating merges.
        """
        missing = _unwired_jobs(workflow, EXEMPT_JOBS)
        assert not missing, (
            f"These jobs are not gated by {GATE_JOB}.needs and would not block "
            f"merges: {sorted(missing)}. Add them to the gate's needs list in "
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

        Without always(), the gate would skip whenever a needed job skips
        (label/auto-merge events), reporting neither success nor failure and
        deadlocking the required check.
        """
        gate_if = str(jobs[GATE_JOB].get("if", "")).strip()
        assert "always()" in gate_if, (
            f"{GATE_JOB} must set `if: always()` (got {gate_if!r}) so it does "
            "not skip on label/auto-merge events and deadlock the required check."
        )

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
