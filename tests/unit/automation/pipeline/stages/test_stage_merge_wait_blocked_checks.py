"""Required-check evidence for merge wait stays diagnostic.

GitHub enforces the required checks when it merges the current head. The
merge-wait stage records a negative check result for the summary and keeps
waiting, so a check that is not green yet never stops the merge wait.
"""

from __future__ import annotations

import threading
from typing import Any

from hephaestus.automation.pipeline.github_jobs import (
    MergeWaitCycleCompleted,
    RunMergeWaitCycleRequest,
)
from hephaestus.automation.pipeline.merge_wait_admission import (
    RequiredChecksEvidence,
    RequiredChecksGate,
)
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import AutoMergeEnableResult, StageOutcome
from hephaestus.automation.pipeline.stages.merge_wait import MergeWaitStage
from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner

MERGE_APPLY = "MERGE_APPLY"
_REVIEWED_HEAD = "a" * 40
_CURRENT_HEAD = "c" * 40


def _request() -> RunMergeWaitCycleRequest:
    """Build one merge-wait cycle request."""
    return RunMergeWaitCycleRequest(
        issue_number=1,
        pr_number=7,
        reviewed_head_sha=_REVIEWED_HEAD,
        deadline_s=60.0,
        cancellation=threading.Event(),
    )


class _ScriptedGitHub:
    """Scripted merge-wait accessor with a controlled required-check gate."""

    def __init__(
        self,
        *,
        gate: RequiredChecksGate | None = None,
        policy: object | None = None,
    ) -> None:
        self._gate = gate
        self._policy = policy if policy is not None else object()
        self.gate_heads: list[str] = []
        self.state: dict[str, object] = {
            "id": "PR_node",
            "state": "OPEN",
            "headRefOid": _CURRENT_HEAD,
            "baseRefName": "main",
            "autoMergeRequest": None,
        }

    def gh_pr_state(self, pr_number: int) -> dict[str, object]:
        """Return the scripted open pull request."""
        return self.state

    def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
        """Return the scripted approval-label state."""
        return (True, False)

    def enable_auto_merge(self, pr_number: int, **kwargs: Any) -> AutoMergeEnableResult:
        """Confirm the auto-merge request."""
        return AutoMergeEnableResult(enabled=True)

    def effective_merge_policy(
        self,
        pr_number: int,
        base_branch: str,
        *,
        deadline_s: float,
        cancellation: threading.Event,
    ) -> object | None:
        """Return the scripted policy, or None for an unreadable policy."""
        return None if self._policy is None else self._policy

    def required_checks_gate_for_head(
        self,
        head_sha: str,
        policy: object,
        *,
        deadline_s: float,
        cancellation: threading.Event,
    ) -> RequiredChecksGate:
        """Return the scripted gate result."""
        self.gate_heads.append(head_sha)
        assert self._gate is not None
        return self._gate


def test_non_green_required_checks_do_not_stop_the_wait() -> None:
    """A failing check is recorded and the merge wait continues."""
    github = _ScriptedGitHub(
        gate=RequiredChecksGate(
            outcome=False,
            evidence=RequiredChecksEvidence(
                boundary="nonpassing_check_run",
                contexts=("required-ci",),
                detail="conclusion=failure",
            ),
        )
    )

    receipt = PipelineGitHubJobRunner._run_merge_wait_cycle(_request(), github)

    assert receipt.outcome == "auto_merge_enabled"
    assert receipt.attempted is True
    assert receipt.required_checks_evidence is not None
    assert receipt.required_checks_evidence.boundary == "nonpassing_check_run"
    assert receipt.required_checks_evidence.contexts == ("required-ci",)


def test_gate_reads_the_current_head_not_the_reviewed_head() -> None:
    """The gate reads the head that GitHub will merge, not the reviewed head."""
    github = _ScriptedGitHub(gate=RequiredChecksGate(outcome=True))

    receipt = PipelineGitHubJobRunner._run_merge_wait_cycle(_request(), github)

    assert receipt.outcome == "auto_merge_enabled"
    assert github.gate_heads == [_CURRENT_HEAD]
    assert receipt.required_checks_evidence is None


def test_unreadable_merge_policy_records_evidence_and_continues() -> None:
    """A policy read failure is bounded evidence, not a stopped wait."""
    github = _ScriptedGitHub(policy=None)

    receipt = PipelineGitHubJobRunner._run_merge_wait_cycle(_request(), github)

    assert receipt.outcome == "auto_merge_enabled"
    assert receipt.required_checks_evidence is not None
    assert receipt.required_checks_evidence.boundary == "read_unavailable"


def test_stage_records_evidence_and_keeps_waiting(make_ctx: Any, make_work_item: Any) -> None:
    """The stage journals the evidence and parks instead of blocking."""
    request = _request()
    item = make_work_item(kind="pr", pr=7, issue=1)
    item.payload["reviewed_pr_head_sha"] = _REVIEWED_HEAD
    item.state = MERGE_APPLY
    item.payload["_pending_github_request"] = request
    item.payload["_merge_wait_cycle_receipt"] = MergeWaitCycleCompleted(
        request=request,
        outcome="auto_merge_enabled",
        attempted=True,
        required_checks_evidence=RequiredChecksEvidence(
            boundary="pending_check_run",
            contexts=("required-ci",),
            detail="status=in_progress",
        ),
    )

    outcome = MergeWaitStage()._merge_apply(item, make_ctx())

    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.RETRY
    assert outcome.note == "merge_readiness_wait"
    assert item.payload["merge_admission_evidence"] == {
        "outcome": "auto_merge_enabled",
        "boundary": "pending_check_run",
        "detail": "status=in_progress",
        "contexts": ["required-ci"],
    }
