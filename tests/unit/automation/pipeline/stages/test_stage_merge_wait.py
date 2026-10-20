"""Tests for the merge-wait stage.

The contract is: an open pull request carrying the exclusive implementation-GO
label is admitted, GitHub native auto-merge is enabled once, and the stage waits
for GitHub to report the merge. The stage does not read the head, does not gate
on required status evidence, and does not rebase first.
"""

from __future__ import annotations

from typing import Any

from hephaestus.automation.pipeline.github_jobs import (
    GitHubJob,
    MergeWaitCycleCompleted,
    RunMergeWaitCycleRequest,
)
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import (
    AutoMergeEnableResult,
    StageOutcome,
)
from hephaestus.automation.pipeline.stages.merge_wait import MergeWaitStage
from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

MERGE = "MERGE"
MERGE_APPLY = "MERGE_APPLY"


def _open_pr(
    head: str = "a" * 40,
    *,
    state: str = "OPEN",
    auto_merge_request: object | None = None,
) -> dict[str, object]:
    """Return one complete open lifecycle record."""
    return {
        "id": "PR_node",
        "state": state,
        "headRefOid": head,
        "baseRefOid": "b" * 40,
        "baseRefName": "main",
        "autoMergeRequest": auto_merge_request,
    }


def _merged_pr(head: str = "a" * 40) -> dict[str, object]:
    """Return one complete merged lifecycle record with a merge commit."""
    return {
        "id": "PR_node",
        "state": "MERGED",
        "mergedAt": "2026-01-01T00:00:00Z",
        "headRefOid": head,
        "baseRefOid": "b" * 40,
        "baseRefName": "main",
        "autoMergeRequest": {"enabledAt": "2026-01-01T00:00:00Z"},
        "mergeCommit": {"oid": "c" * 40},
    }


class _AutoMergeGitHub(FakeStageGitHub):
    """Stage fake that records auto-merge enablement instead of a merge PUT."""

    def __init__(
        self,
        *,
        labels: tuple[bool, bool] = (True, False),
        states: list[dict[str, object] | None] | None = None,
        result: AutoMergeEnableResult | None = None,
        raises: bool = False,
    ) -> None:
        super().__init__(
            pr_impl_state=labels,
            pr_state=(states or [_open_pr()])[0],
        )
        self._states = list(states or [_open_pr()])
        self._result = result if result is not None else AutoMergeEnableResult(enabled=True)
        self._raises = raises
        self.enable_calls: list[int] = []
        self.merge_calls: list[int] = []

    def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
        """Return each scripted state once, then repeat the last one."""
        if self._states:
            return self._states.pop(0)
        return None

    def enable_auto_merge(self, pr_number: int, **kwargs: Any) -> AutoMergeEnableResult:
        """Record one enablement request."""
        self.enable_calls.append(pr_number)
        if self._raises:
            raise RuntimeError("transport exploded")
        return self._result

    def merge_pr_if_head(self, pr_number: int, reviewed_sha: str, **kwargs: Any) -> Any:
        """Record that the removed conditional merge was never called."""
        self.merge_calls.append(pr_number)
        raise AssertionError("merge_pr_if_head must not be called")


def _run_cycle(github: _AutoMergeGitHub, job: GitHubJob) -> Any:
    """Run one merge-wait GitHub cycle against the fake accessor."""
    request = job.request
    assert isinstance(request, RunMergeWaitCycleRequest)
    return PipelineGitHubJobRunner._run_merge_wait_cycle(request, github)


def _job(repo_root: Any) -> GitHubJob:
    """Build one merge-wait GitHub job."""
    return GitHubJob(
        repo="test-repo",
        repo_root=repo_root,
        request=RunMergeWaitCycleRequest(
            issue_number=1,
            pr_number=7,
            reviewed_head_sha="a" * 40,
            deadline_s=60.0,
            cancellation=__import__("threading").Event(),
        ),
        descr="merge_wait_admission_cycle",
    )


def test_open_go_labelled_pr_enables_auto_merge(tmp_path: Any) -> None:
    """The normal case: one enablement request and no conditional merge."""
    github = _AutoMergeGitHub()
    receipt = _run_cycle(github, _job(tmp_path))

    assert github.enable_calls == [7]
    assert github.merge_calls == []
    assert receipt.outcome == "auto_merge_enabled"
    assert receipt.attempted is True


def test_auto_merge_is_enabled_even_when_required_checks_are_not_green(
    tmp_path: Any,
) -> None:
    """Required status evidence is GitHub's gate, not this stage's."""
    github = _AutoMergeGitHub(result=AutoMergeEnableResult(enabled=True))
    github.required_checks_pass_for_head = lambda *a, **k: False  # type: ignore[method-assign]

    receipt = _run_cycle(github, _job(tmp_path))

    assert receipt.outcome == "auto_merge_enabled"
    assert github.enable_calls == [7]


def test_a_conflicting_pull_request_still_enables_auto_merge(tmp_path: Any) -> None:
    """A conflict is not a failure. GitHub reports it and rebases on demand."""
    github = _AutoMergeGitHub(states=[_open_pr()])
    receipt = _run_cycle(github, _job(tmp_path))

    assert receipt.outcome == "auto_merge_enabled"


def test_missing_go_label_is_not_admitted(tmp_path: Any) -> None:
    """Without the approval label nothing is mutated."""
    github = _AutoMergeGitHub(labels=(False, False))
    receipt = _run_cycle(github, _job(tmp_path))

    assert receipt.outcome == "not_implementation_go"
    assert github.enable_calls == []


def test_no_go_label_is_not_admitted(tmp_path: Any) -> None:
    """An exclusive NO-GO blocks the enablement."""
    github = _AutoMergeGitHub(labels=(True, True))
    receipt = _run_cycle(github, _job(tmp_path))

    assert receipt.outcome == "not_implementation_go"
    assert github.enable_calls == []


def test_closed_pull_request_is_not_admitted(tmp_path: Any) -> None:
    """A closed pull request is terminal, not a merge."""
    github = _AutoMergeGitHub(states=[_open_pr(state="CLOSED")])
    receipt = _run_cycle(github, _job(tmp_path))

    assert receipt.outcome == "closed"
    assert github.enable_calls == []


def test_already_merged_pull_request_is_not_admitted(tmp_path: Any) -> None:
    """An already-merged pull request reports the merge instead."""
    github = _AutoMergeGitHub(states=[_merged_pr()])
    receipt = _run_cycle(github, _job(tmp_path))

    assert receipt.outcome == "merged"
    assert github.enable_calls == []


def test_unreadable_pull_request_state_does_not_enable(tmp_path: Any) -> None:
    """An unverified read is refused rather than guessed."""
    github = _AutoMergeGitHub(states=[None])
    receipt = _run_cycle(github, _job(tmp_path))

    assert receipt.outcome == "pr_state_unavailable"
    assert github.enable_calls == []


def test_state_without_the_auto_merge_field_is_refused(tmp_path: Any) -> None:
    """A missing autoMergeRequest field cannot prove the PR is unarmed."""
    partial = _open_pr()
    del partial["autoMergeRequest"]
    github = _AutoMergeGitHub(states=[partial])
    receipt = _run_cycle(github, _job(tmp_path))

    assert receipt.outcome == "pr_state_unverified"
    assert github.enable_calls == []


def test_transport_error_is_retryable(tmp_path: Any) -> None:
    """An unknown enablement outcome retries rather than failing the item."""
    github = _AutoMergeGitHub(result=AutoMergeEnableResult(enabled=False, transport_error=True))
    receipt = _run_cycle(github, _job(tmp_path))

    assert receipt.outcome == "auto_merge_transport_error"
    assert receipt.retryable is True


def test_raised_transport_failure_is_retryable(tmp_path: Any) -> None:
    """An adapter exception at the boundary becomes a retry, not a crash."""
    github = _AutoMergeGitHub(raises=True)
    receipt = _run_cycle(github, _job(tmp_path))

    assert receipt.outcome == "auto_merge_transport_error"
    assert receipt.retryable is True


def test_malformed_response_is_terminal_for_that_attempt(tmp_path: Any) -> None:
    """An uninterpretable response is reported and not retried blindly."""
    github = _AutoMergeGitHub(result=AutoMergeEnableResult(enabled=False, malformed=True))
    receipt = _run_cycle(github, _job(tmp_path))

    assert receipt.outcome == "auto_merge_malformed"
    assert receipt.retryable is False


def test_dry_run_reports_without_mutating(tmp_path: Any) -> None:
    """A dry run never issues the real mutation."""
    github = _AutoMergeGitHub(result=AutoMergeEnableResult(enabled=False, dry_run=True))
    receipt = _run_cycle(github, _job(tmp_path))

    assert receipt.outcome == "auto_merge_dry_run"


def test_missing_pull_request_id_still_enables_by_number(tmp_path: Any) -> None:
    """A read without a node id still enables by pull request number.

    `gh pr merge --auto` addresses the pull request by number, so a missing node
    id is not a reason to refuse.
    """
    state = _open_pr()
    del state["id"]
    github = _AutoMergeGitHub(states=[state])
    receipt = _run_cycle(github, _job(tmp_path))

    assert receipt.outcome == "auto_merge_enabled"
    assert github.enable_calls == [7]


def test_observed_head_is_not_compared_against_live_state(tmp_path: Any) -> None:
    """A new commit between review and enablement does not block the merge."""
    github = _AutoMergeGitHub(states=[_open_pr(head="f" * 40)])
    receipt = _run_cycle(github, _job(tmp_path))

    assert receipt.outcome == "auto_merge_enabled"
    assert github.enable_calls == [7]


def test_stage_parks_after_a_successful_enablement(make_ctx: Any, make_work_item: Any) -> None:
    """After enabling, the stage waits for GitHub instead of declaring success."""
    import threading

    item = make_work_item(kind="pr", pr=7, issue=1)
    item.payload["reviewed_pr_head_sha"] = "a" * 40
    item.state = MERGE_APPLY
    request = RunMergeWaitCycleRequest(
        issue_number=1,
        pr_number=7,
        reviewed_head_sha="a" * 40,
        deadline_s=60.0,
        cancellation=threading.Event(),
    )
    item.payload["_pending_github_request"] = request
    item.payload["_merge_wait_cycle_receipt"] = MergeWaitCycleCompleted(
        request=request, outcome="auto_merge_enabled", attempted=True
    )
    stage = MergeWaitStage()

    outcome = stage._merge_apply(item, make_ctx())

    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.RETRY


def test_stage_finishes_once_github_reports_merged(make_ctx: Any, make_work_item: Any) -> None:
    """A confirmed merge is a pass, and the learning intent is recorded."""
    import threading

    item = make_work_item(kind="pr", pr=7, issue=1)
    item.payload["reviewed_pr_head_sha"] = "a" * 40
    item.state = MERGE_APPLY
    request = RunMergeWaitCycleRequest(
        issue_number=1,
        pr_number=7,
        reviewed_head_sha="a" * 40,
        deadline_s=60.0,
        cancellation=threading.Event(),
    )
    item.payload["_pending_github_request"] = request
    item.payload["_merge_wait_cycle_receipt"] = MergeWaitCycleCompleted(
        request=request, outcome="merged", attempted=True, merge_sha="c" * 40
    )
    stage = MergeWaitStage()

    outcome = stage._merge_apply(item, make_ctx())

    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.FINISH_PASS
