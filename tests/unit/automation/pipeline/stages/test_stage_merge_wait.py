"""Tests for the conditional, head-bound merge-wait stage."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.merge_authorization import (
    MERGE_AUTHORIZATION_MARKER,
    MergeAuthorization,
)
from hephaestus.automation.pipeline.github_jobs import GitHubJob, RunMergeWaitCycleRequest
from hephaestus.automation.pipeline.jobs import JobResult
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import (
    ConditionalMergeResult,
    JobRequest,
    StageOutcome,
)
from hephaestus.automation.pipeline.stages.merge_wait import MergeWaitStage
from hephaestus.automation.pipeline.work_item import LearningIntent
from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

MERGE = "MERGE"


def _authorization_review(
    review_id: str = "R1",
    *,
    head: str = "a" * 40,
    author: str = "operator",
    author_type: str = "User",
    state: str = "APPROVED",
    includes_created_edit: bool = False,
) -> dict[str, object]:
    """Build one native review candidate for merge-wait admission tests."""
    return {
        "id": review_id,
        "fullDatabaseId": 1,
        "body": MERGE_AUTHORIZATION_MARKER,
        "state": state,
        "submittedAt": "2026-08-08T00:00:00Z",
        "updatedAt": "2026-08-08T00:00:00Z",
        "includesCreatedEdit": includes_created_edit,
        "lastEditedAt": None,
        "viewerDidAuthor": False,
        "author": {"login": author, "__typename": author_type},
        "commit": {"oid": head},
    }


def _open_pr(
    head: str = "a" * 40,
    *,
    auto_merge_request: object | None = None,
    base: str = "main",
) -> dict[str, object]:
    """Return a complete open lifecycle record for a merge admission test."""
    return {
        "state": "OPEN",
        "headRefOid": head,
        "autoMergeRequest": auto_merge_request,
        "baseRefName": base,
    }


class _ConditionalGitHub(FakeStageGitHub):
    """Stage fake with scripted atomic-merge and lifecycle responses."""

    def __init__(
        self,
        *,
        labels: tuple[bool, bool] = (True, False),
        states: list[dict[str, object] | None] | None = None,
        merge_results: list[ConditionalMergeResult] | None = None,
        readiness: dict[str, object] | list[dict[str, object]] | None = None,
        conversation_resolution: bool = True,
        authorization_snapshots: list[tuple[dict[str, object], ...]] | None = None,
    ) -> None:
        scripted_states = states or [_open_pr()]
        super().__init__(
            pr_impl_state=labels,
            pr_state=scripted_states[0],
            conversation_resolution=conversation_resolution,
        )
        self._authorization_snapshots = list(authorization_snapshots or [])
        self._states = list(scripted_states)
        self._merge_results = list(
            merge_results
            or [
                ConditionalMergeResult(
                    status=200,
                    body={"merged": True},
                    transport_error=False,
                    malformed=False,
                    dry_run=False,
                )
            ]
        )
        default_readiness = {
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "autoMergeRequest": None,
            "baseRefName": "main",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        self._readiness = (
            list(readiness) if isinstance(readiness, list) else [readiness or default_readiness]
        )
        self.merge_attempts: list[tuple[int, str, str]] = []

    def gh_pr_state(self, pr_number: int) -> dict[str, object] | None:
        del pr_number
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0]

    def merge_authorization_reviews(self, pr_number: int) -> tuple[dict[str, object], ...]:
        """Return the next scripted authorization snapshot."""
        del pr_number
        if len(self._authorization_snapshots) > 1:
            return tuple(dict(review) for review in self._authorization_snapshots.pop(0))
        if self._authorization_snapshots:
            return tuple(dict(review) for review in self._authorization_snapshots[0])
        return super().merge_authorization_reviews(0)

    def merge_pr_if_head(
        self,
        pr_number: int,
        reviewed_sha: str,
        authorization: MergeAuthorization,
    ) -> ConditionalMergeResult:
        self.merge_attempts.append((pr_number, reviewed_sha, authorization.review_id))
        return self._merge_results.pop(0)

    def gh_pr_merge_readiness(self, pr_number: int) -> dict[str, object] | None:
        del pr_number
        if len(self._readiness) > 1:
            return dict(self._readiness.pop(0))
        return dict(self._readiness[0])


def _reviewed_item(make_work_item: Any, *, head: str = "a" * 40) -> Any:
    return make_work_item(
        stage=StageName.MERGE_WAIT,
        pr=12,
        state=MERGE,
        payload={"reviewed_pr_head_sha": head},
    )


def _complete_merge_cycle(stage: MergeWaitStage, item: Any, ctx: Any) -> Any:
    """Execute a dispatched merge cycle at the worker boundary, then apply it."""
    request = stage.step(item, ctx)
    if not isinstance(request, JobRequest) or not isinstance(request.job, GitHubJob):
        return request
    assert isinstance(request.job.request, RunMergeWaitCycleRequest)
    try:
        receipt = PipelineGitHubJobRunner._run_merge_wait_cycle(
            request.job.request,
            ctx.github,
        )
        result = JobResult(ok=True, value=receipt)
    except Exception as error:
        result = JobResult(ok=False, error=f"{type(error).__name__}: {error}")
    stage.on_job_done(item, result, ctx)
    item.state = request.on_done_state
    applied = stage.step(item, ctx)
    if isinstance(applied, StageOutcome) and applied.disposition is Disposition.RETRY:
        item.state = MERGE
    return applied


@pytest.mark.parametrize(
    ("reviews", "reason"),
    [
        ((), "merge_authorization_absent"),
        ((_authorization_review(head="b" * 40),), "merge_authorization_stale"),
        (
            (_authorization_review("R1"), _authorization_review("R2")),
            "merge_authorization_ambiguous",
        ),
        (
            (_authorization_review(includes_created_edit=True),),
            "merge_authorization_replayed",
        ),
        (
            (_authorization_review(state="DISMISSED"),),
            "merge_authorization_revoked",
        ),
        (
            (_authorization_review(author="service[bot]", author_type="Bot"),),
            "merge_authorization_untrusted",
        ),
    ],
)
def test_non_authorized_resolution_never_reaches_conditional_put(
    reviews: tuple[dict[str, object], ...],
    reason: str,
    make_ctx: Any,
    make_work_item: Any,
) -> None:
    """Every operator-correctable authorization state blocks the PUT."""
    github = _ConditionalGitHub(authorization_snapshots=[reviews])

    result = _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=github)
    )

    assert result == StageOutcome(Disposition.BLOCKED, reason)
    assert github.merge_attempts == []


def test_implementation_go_without_operator_authorization_is_blocked(
    make_ctx: Any, make_work_item: Any
) -> None:
    """The automated GO label alone cannot authorize a conditional merge."""
    github = _ConditionalGitHub(authorization_snapshots=[()])

    result = _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=github)
    )

    assert result == StageOutcome(Disposition.BLOCKED, "merge_authorization_absent")
    assert github.merge_attempts == []


def test_trusted_operator_authorization_permits_conditional_put(
    make_ctx: Any, make_work_item: Any
) -> None:
    """One trusted unedited current-head approval reaches the conditional PUT."""
    github = _ConditionalGitHub(
        authorization_snapshots=[(_authorization_review("R1"),)],
        states=[_open_pr(), _open_pr(), {"state": "MERGED"}],
    )
    ctx = make_ctx(github=github, config_overrides={"enable_learn": False})

    result = _complete_merge_cycle(MergeWaitStage(), _reviewed_item(make_work_item), ctx)

    assert result == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert github.merge_attempts == [(12, "a" * 40, "R1")]


def test_malformed_trusted_candidate_with_valid_approval_blocks_put(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A replayed candidate vetoes a valid approval before the PUT."""
    malformed = _authorization_review("R2")
    malformed["submittedAt"] = None
    github = _ConditionalGitHub(authorization_snapshots=[(_authorization_review("R1"), malformed)])

    result = _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=github)
    )

    assert result == StageOutcome(Disposition.BLOCKED, "merge_authorization_replayed")
    assert github.merge_attempts == []


def test_authorization_read_failure_is_terminal_without_put(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A review or permission adapter failure is classified as unavailable."""

    class UnavailableGitHub(_ConditionalGitHub):
        def merge_authorization_reviews(self, pr_number: int) -> tuple[dict[str, object], ...]:
            del pr_number
            raise RuntimeError("review service unavailable")

    github = UnavailableGitHub()

    result = _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=github)
    )

    assert result == StageOutcome(Disposition.FINISH_FAIL, "merge_authorization_unavailable")
    assert github.merge_attempts == []


def test_authorization_identity_change_before_put_fails_closed(
    make_ctx: Any, make_work_item: Any
) -> None:
    """The final read must identify the same durable review as the first."""
    github = _ConditionalGitHub(
        authorization_snapshots=[
            (_authorization_review("R1"),),
            (_authorization_review("R2"),),
        ]
    )

    result = _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=github)
    )

    assert result == StageOutcome(Disposition.FINISH_FAIL, "merge_authorization_changed")
    assert github.merge_attempts == []


def test_merge_cycle_dispatches_without_inline_github_calls(
    make_ctx: Any, make_work_item: Any
) -> None:
    """MERGE freezes its exact proof before any admission or mutation call."""

    class InlineGitHubForbidden(FakeStageGitHub):
        def __getattribute__(self, name: str) -> object:
            if name in {
                "gh_pr_state",
                "pr_has_implementation_state_label",
                "list_unresolved_review_threads",
                "base_branch_requires_conversation_resolution",
                "gh_pr_merge_readiness",
                "merge_pr_if_head",
            }:
                raise AttributeError(f"GitHub call ran inline: {name}")
            return super().__getattribute__(name)

    item = _reviewed_item(make_work_item)
    stage = MergeWaitStage()
    ctx = make_ctx(github=InlineGitHubForbidden())

    started = time.monotonic()
    result = stage.step(item, ctx)
    elapsed = time.monotonic() - started

    assert isinstance(result, JobRequest)
    assert isinstance(result.job, GitHubJob)
    assert isinstance(result.job.request, RunMergeWaitCycleRequest)
    assert result.job.request.reviewed_head_sha == "a" * 40
    assert elapsed < 0.25


def test_conditional_merge_succeeds_only_after_lifecycle_confirms_merged(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A 200 response is insufficient until the terminal lifecycle read is merged."""
    github = _ConditionalGitHub(states=[_open_pr(), _open_pr(), {"state": "MERGED"}])
    ctx = make_ctx(github=github, config_overrides={"enable_learn": False})

    result = _complete_merge_cycle(MergeWaitStage(), _reviewed_item(make_work_item), ctx)

    assert result == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert github.merge_attempts == [(12, "a" * 40, "R1")]


@pytest.mark.parametrize("merge_state_status", ["CLEAN", "HAS_HOOKS"])
def test_mergeable_requestable_readiness_merges_successfully(
    make_ctx: Any, make_work_item: Any, merge_state_status: str
) -> None:
    """Initially-ready requestable states reach the PUT without seeding a wait."""
    github = _ConditionalGitHub(
        states=[_open_pr(), _open_pr(), {"state": "MERGED"}],
        readiness={
            **_open_pr(),
            "mergeable": "MERGEABLE",
            "mergeStateStatus": merge_state_status,
        },
    )
    ctx = make_ctx(github=github, config_overrides={"enable_learn": False})
    item = _reviewed_item(make_work_item)

    result = _complete_merge_cycle(MergeWaitStage(), item, ctx)

    assert result == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert github.merge_attempts == [(12, "a" * 40, "R1")]
    assert "merge_readiness_deadline_s" not in item.payload
    assert "merge_readiness_polls" not in item.payload


def test_optional_failure_unstable_readiness_attempts_conditional_merge(
    make_ctx: Any, make_work_item: Any
) -> None:
    """An optional failing status lets server protection classify the first PUT."""
    github = _ConditionalGitHub(
        states=[_open_pr(), _open_pr(), {"state": "MERGED"}],
        readiness={
            **_open_pr(),
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "UNSTABLE",
        },
    )
    ctx = make_ctx(github=github, config_overrides={"enable_learn": False})
    item = _reviewed_item(make_work_item)

    result = _complete_merge_cycle(MergeWaitStage(), item, ctx)

    assert result == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert github.merge_attempts == [(12, "a" * 40, "R1")]
    assert "merge_readiness_deadline_s" not in item.payload
    assert "merge_readiness_polls" not in item.payload


def test_blocked_readiness_waits_before_the_first_conditional_merge(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Pending readiness is polled without burning a conditional PUT."""
    not_ready = {
        **_open_pr(),
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED",
    }
    ready = {
        **_open_pr(),
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
    }
    github = _ConditionalGitHub(
        states=[_open_pr(), _open_pr(), _open_pr(), {"state": "MERGED"}],
        readiness=[not_ready, ready],
    )
    ctx = make_ctx(github=github, config_overrides={"enable_learn": False})
    item = _reviewed_item(make_work_item)

    first = _complete_merge_cycle(MergeWaitStage(), item, ctx)

    assert first == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
    assert item.payload["retry_delay_s"] == 5.0
    assert github.merge_attempts == []

    second = _complete_merge_cycle(MergeWaitStage(), item, ctx)

    assert second == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert github.merge_attempts == [(12, "a" * 40, "R1")]


def test_readiness_wake_rechecks_the_head_before_a_conditional_merge(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A head change during the timer wait returns to fresh review without a PUT."""
    blocked = {**_open_pr(), "mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED"}
    ready = {**_open_pr(), "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}
    github = _ConditionalGitHub(
        states=[_open_pr(), _open_pr(), _open_pr("b" * 40)],
        readiness=[blocked, ready],
    )
    item = _reviewed_item(make_work_item)
    stage = MergeWaitStage()
    ctx = make_ctx(github=github)

    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.RETRY, "merge_readiness_wait"
    )

    result = _complete_merge_cycle(stage, item, ctx)

    assert result == StageOutcome(Disposition.FAIL_BACK, "reviewed_head_drift")
    assert github.merge_attempts == []


def test_readiness_wake_fails_closed_when_a_thread_appears_while_ci_is_pending(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A timer wake rejects a newly unresolved thread before re-parking."""

    class ThreadAppearsDuringReadinessWaitGitHub(_ConditionalGitHub):
        def __init__(self) -> None:
            super().__init__(
                readiness={
                    **_open_pr(),
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "BLOCKED",
                }
            )
            self.thread_reads = 0

        def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, object]]:
            del pr_number
            self.thread_reads += 1
            if self.thread_reads == 1:
                return []
            return [{"id": "late-thread", "automation_owned": True}]

    github = ThreadAppearsDuringReadinessWaitGitHub()
    item = _reviewed_item(make_work_item)
    stage = MergeWaitStage()
    ctx = make_ctx(github=github)

    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.RETRY, "merge_readiness_wait"
    )
    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.FINISH_FAIL,
        "unresolved_review_threads",
    )
    assert github.merge_attempts == []


def test_readiness_wake_fails_closed_when_conversation_protection_is_removed(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A timer wake rejects a branch-policy change before re-parking."""

    class ProtectionRemovedDuringReadinessWaitGitHub(_ConditionalGitHub):
        def __init__(self) -> None:
            super().__init__(
                readiness={
                    **_open_pr(),
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "BLOCKED",
                }
            )
            self.policy_reads = 0

        def base_branch_requires_conversation_resolution(
            self, pr_number: int, base_branch: str
        ) -> bool:
            self.policy_reads += 1
            assert super().base_branch_requires_conversation_resolution(pr_number, base_branch)
            return self.policy_reads == 1

    github = ProtectionRemovedDuringReadinessWaitGitHub()
    item = _reviewed_item(make_work_item)
    stage = MergeWaitStage()
    ctx = make_ctx(github=github)

    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.RETRY, "merge_readiness_wait"
    )
    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.FINISH_FAIL,
        "conversation_resolution_required",
    )
    assert github.merge_attempts == []


def test_minute_scale_readiness_wait_merges_once_when_github_becomes_ready(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Normal CI-duration readiness does not burn conditional merge attempts."""
    now = [0.0]

    class DelayedReadyGitHub(_ConditionalGitHub):
        def __init__(self) -> None:
            super().__init__()
            self.merged = False

        def gh_pr_state(self, pr_number: int) -> dict[str, object] | None:
            del pr_number
            return {"state": "MERGED"} if self.merged else _open_pr()

        def gh_pr_merge_readiness(self, pr_number: int) -> dict[str, object] | None:
            del pr_number
            status = "CLEAN" if now[0] >= 10 * 60 else "BLOCKED"
            return {**_open_pr(), "mergeable": "MERGEABLE", "mergeStateStatus": status}

        def merge_pr_if_head(
            self,
            pr_number: int,
            reviewed_sha: str,
            authorization: MergeAuthorization,
        ) -> ConditionalMergeResult:
            self.merge_attempts.append((pr_number, reviewed_sha, authorization.review_id))
            self.merged = True
            return ConditionalMergeResult(
                status=200,
                body={"merged": True},
                transport_error=False,
                malformed=False,
                dry_run=False,
            )

    github = DelayedReadyGitHub()
    ctx = make_ctx(
        github=github,
        now_fn=lambda: now[0],
        config_overrides={"enable_learn": False},
    )
    item = _reviewed_item(make_work_item)
    stage = MergeWaitStage()

    while now[0] < 10 * 60:
        result = _complete_merge_cycle(stage, item, ctx)
        assert result == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
        assert github.merge_attempts == []
        now[0] += item.payload["retry_delay_s"]

    result = _complete_merge_cycle(stage, item, ctx)

    assert now[0] >= 10 * 60
    assert result == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert github.merge_attempts == [(12, "a" * 40, "R1")]


def test_open_thread_immediately_before_merge_fails_back_without_put(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Merge wait independently rejects a thread that appears after review GO."""

    class LateThreadGitHub(_ConditionalGitHub):
        def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, object]]:
            del pr_number
            return [{"id": "late-thread", "automation_owned": True}]

    github = LateThreadGitHub(states=[_open_pr()])

    item = _reviewed_item(make_work_item)
    result = _complete_merge_cycle(MergeWaitStage(), item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "unresolved_review_threads")
    assert github.merge_attempts == []


@pytest.mark.parametrize("conversation_resolution", [False])
def test_missing_conversation_resolution_policy_blocks_merge_put(
    make_ctx: Any,
    make_work_item: Any,
    conversation_resolution: bool,
) -> None:
    """A branch without server-enforced conversation resolution cannot merge."""
    github = _ConditionalGitHub(conversation_resolution=conversation_resolution)

    result = _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=github)
    )

    assert result == StageOutcome(Disposition.FINISH_FAIL, "conversation_resolution_required")
    assert github.merge_attempts == []
    assert github.conversation_resolution_checks == [(12, "main")]


def test_missing_admin_enforcement_policy_blocks_merge_put(
    make_ctx: Any, make_work_item: Any
) -> None:
    """The combined server-policy admission also rejects an admin bypass risk."""

    class NoAdminEnforcementGitHub(_ConditionalGitHub):
        def base_branch_requires_conversation_resolution(
            self, pr_number: int, base_branch: str
        ) -> bool:
            super().base_branch_requires_conversation_resolution(pr_number, base_branch)
            return False

    github = NoAdminEnforcementGitHub()

    result = _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=github)
    )

    assert result == StageOutcome(Disposition.FINISH_FAIL, "conversation_resolution_required")
    assert github.merge_attempts == []
    assert github.conversation_resolution_checks == [(12, "main")]


def test_unreadable_conversation_resolution_policy_blocks_merge_put(
    make_ctx: Any, make_work_item: Any
) -> None:
    """An unavailable server gate fails closed before the conditional PUT."""

    class UnreadablePolicyGitHub(_ConditionalGitHub):
        def base_branch_requires_conversation_resolution(
            self, pr_number: int, base_branch: str
        ) -> bool:
            del pr_number, base_branch
            raise RuntimeError("protection read failed")

    github = UnreadablePolicyGitHub()

    result = _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=github)
    )

    assert result == StageOutcome(Disposition.FINISH_FAIL, "conversation_resolution_unavailable")
    assert github.merge_attempts == []


def test_server_policy_rejects_thread_that_appears_after_local_thread_read(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Server protection, not the local read, prevents a late-thread merge."""

    class ServerRejectsLateThreadGitHub(_ConditionalGitHub):
        def __init__(self) -> None:
            super().__init__(
                readiness=[
                    {**_open_pr(), "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"},
                    {**_open_pr(), "mergeable": "BLOCKED", "mergeStateStatus": "BLOCKED"},
                ]
            )
            self._thread_appeared_after_local_read = False

        def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, object]]:
            del pr_number
            return []

        def base_branch_requires_conversation_resolution(
            self, pr_number: int, base_branch: str
        ) -> bool:
            assert super().base_branch_requires_conversation_resolution(pr_number, base_branch)
            self._thread_appeared_after_local_read = True
            return True

        def merge_pr_if_head(
            self,
            pr_number: int,
            reviewed_sha: str,
            authorization: MergeAuthorization,
        ) -> ConditionalMergeResult:
            assert self._thread_appeared_after_local_read
            self.merge_attempts.append((pr_number, reviewed_sha, authorization.review_id))
            return ConditionalMergeResult(
                status=405,
                body={"message": "review conversations must be resolved"},
            )

    github = ServerRejectsLateThreadGitHub()
    item = _reviewed_item(make_work_item)

    result = _complete_merge_cycle(MergeWaitStage(), item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
    assert item.payload["retry_delay_s"] == 5.0
    assert github.merge_attempts == [(12, "a" * 40, "R1")]


def test_external_arm_blocks_conditional_merge_without_any_mutation(
    make_ctx: Any, make_work_item: Any
) -> None:
    """An arm observed at final admission belongs to an external actor."""
    github = _ConditionalGitHub(states=[_open_pr(auto_merge_request={"enabledAt": "elsewhere"})])

    result = _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=github)
    )

    assert result == StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
    assert github.merge_attempts == []
    assert github.mutation_log == []


def test_stale_proof_fails_back_without_revoking_a_label(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A stale process owns no later label and must only request fresh review."""
    github = _ConditionalGitHub(states=[_open_pr("b" * 40)])

    result = _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=github)
    )

    assert result == StageOutcome(Disposition.FAIL_BACK, "reviewed_head_drift")
    assert github.merge_attempts == []
    assert github.mutation_log == []


def test_revoke_stale_reviewed_head_never_writes_after_external_interleaving(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A new arm/GO may arrive after the final read, so stale runs never relabel."""

    class InterleavingGitHub(_ConditionalGitHub):
        def mark_pr_implementation_no_go(self, pr_number: int) -> None:
            self._pr_state = _open_pr(
                "c" * 40,
                auto_merge_request={"enabledAt": "external-after-read"},
            )
            self._pr_impl_state = (True, False)
            super().mark_pr_implementation_no_go(pr_number)

    github = InterleavingGitHub(states=[_open_pr("b" * 40)])
    item = _reviewed_item(make_work_item)

    result = MergeWaitStage()._revoke_stale_reviewed_head(item, make_ctx(github=github), "a" * 40)

    assert result == StageOutcome(Disposition.FAIL_BACK, "reviewed_head_drift")
    assert github.mutation_log == []


def test_already_merged_retry_never_attempts_a_second_merge(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A retry that observes terminal merged state is a success without a PUT."""
    github = _ConditionalGitHub(states=[{"state": "MERGED"}])
    ctx = make_ctx(github=github, config_overrides={"enable_learn": False})

    result = _complete_merge_cycle(MergeWaitStage(), _reviewed_item(make_work_item), ctx)

    assert result == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert github.merge_attempts == []


def test_merged_emits_post_merge_intent_without_job(
    tmp_path: Path, make_ctx: Any, make_work_item: Any
) -> None:
    """A confirmed merge records auxiliary work without blocking merge success."""
    from hephaestus.automation.arming_state import LearningJournalStore

    github = _ConditionalGitHub(states=[{"state": "MERGED"}])
    item = _reviewed_item(make_work_item)
    journal = LearningJournalStore(lambda: tmp_path)

    result = _complete_merge_cycle(
        MergeWaitStage(), item, make_ctx(github=github, learning_journal=journal)
    )

    assert result == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert item.learning_intents == [LearningIntent.post_merge(repo=item.repo, issue=1, pr=12)]
    record = journal.load(item.learning_intents[0].key)
    assert record is not None and record["status"] == "pending"
    assert github.merge_attempts == []


def test_post_merge_journal_failure_is_ancillary(make_ctx: Any, make_work_item: Any) -> None:
    """A journal failure after merge confirmation cannot change merge success."""
    from hephaestus.automation.arming_state import LearningJournalStore

    class BrokenJournal(LearningJournalStore):
        def ensure_pending(
            self,
            key: str,
            *,
            kind: str,
            identity: dict[str, object] | None = None,
        ) -> dict[str, Any]:
            raise OSError("journal unavailable")

    github = _ConditionalGitHub(states=[{"state": "MERGED"}])
    item = _reviewed_item(make_work_item)

    result = _complete_merge_cycle(
        MergeWaitStage(),
        item,
        make_ctx(github=github, learning_journal=BrokenJournal(lambda: Path("."))),
    )

    assert result == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert item.payload["learning_failures"][0]["error"] == "learning_intent_persist_failed"


def test_legacy_inflight_learning_is_not_dispatched_again(
    tmp_path: Path, make_ctx: Any, make_work_item: Any
) -> None:
    """A legacy ambiguous claim becomes an ancillary terminal journal result."""
    from hephaestus.automation.arming_state import LearningJournalStore

    github = _ConditionalGitHub(states=[{"state": "MERGED"}])
    github.learn_claims.add(1)
    item = _reviewed_item(make_work_item)
    journal = LearningJournalStore(lambda: tmp_path)

    result = _complete_merge_cycle(
        MergeWaitStage(), item, make_ctx(github=github, learning_journal=journal)
    )

    assert result == StageOutcome(Disposition.FINISH_PASS, "merged")
    intent = LearningIntent.post_merge(repo=item.repo, issue=1, pr=12)
    record = journal.load(intent.key)
    assert record is not None
    assert record["status"] == "failed"
    assert record["error"] == "legacy_outcome_unknown"


def test_ambiguous_transport_reconciles_a_merged_pr_without_duplicate_put(
    make_ctx: Any, make_work_item: Any
) -> None:
    """An unknown transport outcome is reconciled before any retry is considered."""
    github = _ConditionalGitHub(
        states=[_open_pr(), _open_pr(), {"state": "MERGED"}],
        merge_results=[
            ConditionalMergeResult(
                status=None,
                body=None,
                transport_error=True,
                malformed=False,
                dry_run=False,
            )
        ],
    )
    ctx = make_ctx(github=github, config_overrides={"enable_learn": False})

    result = _complete_merge_cycle(MergeWaitStage(), _reviewed_item(make_work_item), ctx)

    assert result == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert github.merge_attempts == [(12, "a" * 40, "R1")]


def test_ambiguous_transport_head_drift_fails_back_without_label_mutation(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A transport retry cannot carry a review proof across a changed head."""
    github = _ConditionalGitHub(
        states=[_open_pr(), _open_pr(), _open_pr("b" * 40)],
        merge_results=[
            ConditionalMergeResult(
                status=None,
                body=None,
                transport_error=True,
                malformed=False,
                dry_run=False,
            )
        ],
    )

    result = _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=github)
    )

    assert result == StageOutcome(Disposition.FAIL_BACK, "reviewed_head_drift")
    assert github.merge_attempts == [(12, "a" * 40, "R1")]
    assert github.mutation_log == []


def test_ambiguous_transport_retries_only_after_delayed_same_head_read(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A same-head ambiguity earns one timer retry, never an in-step re-PUT."""
    github = _ConditionalGitHub(
        states=[_open_pr(), _open_pr(), _open_pr()],
        merge_results=[
            ConditionalMergeResult(
                status=None,
                body=None,
                transport_error=True,
                malformed=False,
                dry_run=False,
            )
        ],
    )
    item = _reviewed_item(make_work_item)

    result = _complete_merge_cycle(
        MergeWaitStage(), item, make_ctx(github=github, budget_fn=lambda _route: 2)
    )

    assert result == StageOutcome(Disposition.RETRY, "merge_not_ready")
    assert item.attempts["merge"] == 1
    assert item.payload["retry_delay_s"] == 1.0
    assert github.merge_attempts == [(12, "a" * 40, "R1")]
    assert github.mutation_log == []


def test_ambiguous_transport_with_unreadable_reconciliation_is_terminal(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A transport ambiguity cannot retry when the confirming read is unavailable."""
    github = _ConditionalGitHub(
        states=[_open_pr(), _open_pr(), None],
        merge_results=[
            ConditionalMergeResult(
                status=None,
                body=None,
                transport_error=True,
                malformed=False,
                dry_run=False,
            )
        ],
    )

    result = _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=github)
    )

    assert result == StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
    assert github.merge_attempts == [(12, "a" * 40, "R1")]
    assert github.mutation_log == []


def test_restarted_merge_without_process_local_proof_fails_back_without_put(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A restarted process never reconstructs authority from durable labels alone."""
    github = _ConditionalGitHub(states=[_open_pr()])
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=MERGE)

    result = _complete_merge_cycle(MergeWaitStage(), item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "reviewed_head_missing")
    assert github.merge_attempts == []
    assert github.mutation_log == []


def test_409_head_drift_fails_back_without_label_mutation(
    make_ctx: Any, make_work_item: Any
) -> None:
    """The conditional SHA conflict cannot revoke an actor-owned later label."""
    github = _ConditionalGitHub(
        states=[_open_pr(), _open_pr(), _open_pr("b" * 40)],
        merge_results=[
            ConditionalMergeResult(
                status=409,
                body={"message": "head changed"},
                transport_error=False,
                malformed=False,
                dry_run=False,
            )
        ],
    )

    result = _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=github)
    )

    assert result == StageOutcome(Disposition.FAIL_BACK, "reviewed_head_drift")
    assert github.merge_attempts == [(12, "a" * 40, "R1")]
    assert github.mutation_log == []


def test_405_reenters_readiness_wait_without_a_second_conditional_put(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Not-ready protections retry later instead of issuing a second PUT in-step."""
    github = _ConditionalGitHub(
        states=[_open_pr(), _open_pr(), _open_pr()],
        merge_results=[
            ConditionalMergeResult(
                status=405,
                body={"message": "not ready"},
                transport_error=False,
                malformed=False,
                dry_run=False,
            )
        ],
        readiness=[
            {**_open_pr(), "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"},
            {**_open_pr(), "mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED"},
            {**_open_pr(), "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"},
        ],
    )
    item = _reviewed_item(make_work_item)

    result = _complete_merge_cycle(
        MergeWaitStage(), item, make_ctx(github=github, budget_fn=lambda _route: 2)
    )

    assert result == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
    assert item.payload["retry_delay_s"] == 5.0
    assert github.merge_attempts == [(12, "a" * 40, "R1")]


@pytest.mark.parametrize("merge_state_status", ["CLEAN", "HAS_HOOKS", "UNSTABLE"])
def test_persistent_405_unchanged_readiness_does_not_duplicate_the_put(
    make_ctx: Any, make_work_item: Any, merge_state_status: str
) -> None:
    """A 405 parks while readiness is unchanged instead of retrying the same PUT."""
    readiness = {
        **_open_pr(),
        "mergeable": "MERGEABLE",
        "mergeStateStatus": merge_state_status,
    }
    github = _ConditionalGitHub(
        states=[_open_pr()],
        merge_results=[
            ConditionalMergeResult(
                status=405,
                body={"message": "not ready"},
                transport_error=False,
                malformed=False,
                dry_run=False,
            )
        ],
        readiness=[readiness, readiness, readiness],
    )
    item = _reviewed_item(make_work_item)
    stage = MergeWaitStage()
    ctx = make_ctx(github=github, budget_fn=lambda _route: 2)

    first = _complete_merge_cycle(stage, item, ctx)

    assert first == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
    assert item.payload["retry_delay_s"] == 5.0
    assert item.attempts["merge"] == 1
    assert github.merge_attempts == [(12, "a" * 40, "R1")]

    second = _complete_merge_cycle(stage, item, ctx)

    assert second == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
    assert item.payload["retry_delay_s"] == 10.0
    assert item.attempts["merge"] == 1
    assert github.merge_attempts == [(12, "a" * 40, "R1")]


def test_persistent_405_has_hooks_retries_only_after_readiness_changes(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Unchanged HAS_HOOKS after 405 parks across wakes until readiness changes."""
    has_hooks = {
        **_open_pr(),
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "HAS_HOOKS",
    }
    clean = {
        **_open_pr(),
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
    }
    scripted_states: list[dict[str, object] | None] = [_open_pr() for _ in range(7)]
    scripted_states.append({"state": "MERGED"})
    github = _ConditionalGitHub(
        states=scripted_states,
        merge_results=[
            ConditionalMergeResult(
                status=405,
                body={"message": "not ready"},
                transport_error=False,
                malformed=False,
                dry_run=False,
            ),
            ConditionalMergeResult(
                status=200,
                body={"merged": True},
                transport_error=False,
                malformed=False,
                dry_run=False,
            ),
        ],
        readiness=[has_hooks, has_hooks, has_hooks, has_hooks, clean],
    )
    item = _reviewed_item(make_work_item)
    stage = MergeWaitStage()
    ctx = make_ctx(
        github=github,
        budget_fn=lambda _route: 2,
        config_overrides={"enable_learn": False},
    )

    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.RETRY, "merge_readiness_wait"
    )
    assert item.attempts["merge"] == 1
    assert github.merge_attempts == [(12, "a" * 40, "R1")]

    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.RETRY, "merge_readiness_wait"
    )
    assert item.attempts["merge"] == 1
    assert github.merge_attempts == [(12, "a" * 40, "R1")]

    # A second unchanged HAS_HOOKS wake must remain non-mutating. This makes
    # the regression independent of the one-step in-call 405 reconciliation.
    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.RETRY, "merge_readiness_wait"
    )
    assert item.attempts["merge"] == 1
    assert github.merge_attempts == [(12, "a" * 40, "R1")]

    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.FINISH_PASS, "merged"
    )
    assert item.attempts["merge"] == 2
    assert github.merge_attempts == [(12, "a" * 40, "R1"), (12, "a" * 40, "R1")]


def test_fresh_same_head_proof_retries_a_prior_unstable_decline(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A re-review of unchanged code gets its own permitted conditional request."""
    unstable = {
        **_open_pr(),
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "UNSTABLE",
    }

    class FreshProofGitHub(_ConditionalGitHub):
        def gh_pr_state(self, pr_number: int) -> dict[str, object] | None:
            del pr_number
            return {"state": "MERGED"} if len(self.merge_attempts) == 2 else _open_pr()

    github = FreshProofGitHub(
        merge_results=[
            ConditionalMergeResult(
                status=405,
                body={"message": "not ready"},
                transport_error=False,
                malformed=False,
                dry_run=False,
            ),
            ConditionalMergeResult(
                status=200,
                body={"merged": True},
                transport_error=False,
                malformed=False,
                dry_run=False,
            ),
        ],
        readiness=[unstable, unstable, unstable],
    )
    item = _reviewed_item(make_work_item)
    stage = MergeWaitStage()
    ctx = make_ctx(
        github=github,
        budget_fn=lambda _route: 2,
        config_overrides={"enable_learn": False},
    )

    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.RETRY, "merge_readiness_wait"
    )

    item.payload["reviewed_pr_proof_generation"] = 1

    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.FINISH_PASS, "merged"
    )
    assert github.merge_attempts == [(12, "a" * 40, "R1"), (12, "a" * 40, "R1")]


@pytest.mark.parametrize("merge_state_status", ["CONFLICTING", "DIRTY"])
def test_405_conflicting_or_dirty_readiness_returns_to_implementer(
    make_ctx: Any, make_work_item: Any, merge_state_status: str
) -> None:
    """A reviewed conflict is handed to the implementation agent to resolve."""
    github = _ConditionalGitHub(
        states=[_open_pr(), _open_pr(), _open_pr()],
        merge_results=[
            ConditionalMergeResult(
                status=405,
                body={"message": "not ready"},
                transport_error=False,
                malformed=False,
                dry_run=False,
            )
        ],
        readiness=[
            {**_open_pr(), "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"},
            {
                **_open_pr(),
                "mergeable": "CONFLICTING",
                "mergeStateStatus": merge_state_status,
            },
        ],
    )

    item = _reviewed_item(make_work_item)
    result = _complete_merge_cycle(MergeWaitStage(), item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "merge_conflicting")
    assert item.payload["post_review_rebase_required"] is True
    assert github.merge_attempts == [(12, "a" * 40, "R1")]
    assert github.mutation_log == []


def test_reviewed_behind_head_returns_to_implementer_for_rebase(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A reviewer does not validate against current main; implementation rebases later."""
    github = _ConditionalGitHub(
        readiness={
            **_open_pr(),
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BEHIND",
        }
    )
    item = _reviewed_item(make_work_item)

    result = _complete_merge_cycle(MergeWaitStage(), item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "post_review_rebase_required")
    assert item.payload["post_review_rebase_required"] is True
    assert github.merge_attempts == []


def test_409_reconciliation_external_arm_blocks_without_label_mutation(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A fresh external arm after a 409 blocks the run without trying to revoke it."""
    github = _ConditionalGitHub(
        states=[_open_pr(), _open_pr(), _open_pr(auto_merge_request={"enabledAt": "external"})],
        merge_results=[
            ConditionalMergeResult(
                status=409,
                body={"message": "head changed"},
                transport_error=False,
                malformed=False,
                dry_run=False,
            )
        ],
    )

    result = _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=github)
    )

    assert result == StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
    assert github.merge_attempts == [(12, "a" * 40, "R1")]
    assert github.mutation_log == []


def test_label_loss_or_non_main_base_prevents_the_conditional_request(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Final authorization facts are all required immediately before the PUT."""
    label_lost = _ConditionalGitHub(labels=(False, False), states=[_open_pr()])
    wrong_base = _ConditionalGitHub(states=[_open_pr(base="release")])

    assert _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=label_lost)
    ) == StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")
    assert _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=wrong_base)
    ) == StageOutcome(Disposition.FINISH_FAIL, "non_main_base")
    assert label_lost.merge_attempts == []
    assert wrong_base.merge_attempts == []


def test_contradictory_implementation_labels_prevent_the_conditional_request(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A contradictory durable state is not an exclusive implementation approval."""
    github = _ConditionalGitHub(labels=(True, True), states=[_open_pr()])

    result = _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=github)
    )

    assert result == StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")
    assert github.merge_attempts == []
    assert github.mutation_log == []


def test_review_prose_cannot_replace_a_missing_implementation_go_label(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Decision-shaped review text is never an admission fact."""
    github = _ConditionalGitHub(labels=(False, False), states=[_open_pr()])
    item = _reviewed_item(make_work_item)
    item.payload["review_feedback"] = "Verdict: GO"

    result = _complete_merge_cycle(MergeWaitStage(), item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")
    assert github.merge_attempts == []
    assert github.mutation_log == []


def test_200_without_merged_true_is_terminal(make_ctx: Any, make_work_item: Any) -> None:
    """A successful transport does not imply the PR was merged."""
    github = _ConditionalGitHub(
        merge_results=[
            ConditionalMergeResult(
                status=200,
                body={"merged": False},
                transport_error=False,
                malformed=False,
                dry_run=False,
            )
        ]
    )

    result = _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=github)
    )

    assert result == StageOutcome(Disposition.FINISH_FAIL, "merge_not_merged")


def test_blocked_readiness_wait_allows_one_full_ci_restart_with_a_bounded_deadline(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A normal CI restart fits while readiness waiting remains bounded."""
    github = _ConditionalGitHub(
        states=[_open_pr()],
        readiness={
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "autoMergeRequest": None,
            "baseRefName": "main",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BLOCKED",
        },
    )
    item = _reviewed_item(make_work_item)
    stage = MergeWaitStage()
    now = [1000.0]
    ctx = make_ctx(
        github=github,
        budget_fn=lambda _route: 2,
        now_fn=lambda: now[0],
    )

    for _ in range(33):
        result = _complete_merge_cycle(stage, item, ctx)
        assert result == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
        assert github.merge_attempts == []
        now[0] += item.payload["retry_delay_s"]

    result = _complete_merge_cycle(stage, item, ctx)

    assert result == StageOutcome(Disposition.FINISH_FAIL, "merge_readiness_timeout")
    assert github.merge_attempts == []
    assert item.payload["merge_readiness_deadline_s"] == 2800.0
    assert item.payload["merge_readiness_polls"] == 33


@pytest.mark.parametrize("now", [1900.0, 1901.0])
def test_requestable_readiness_honors_an_existing_matching_deadline_without_puts(
    make_ctx: Any, make_work_item: Any, now: float
) -> None:
    """A ready PR cannot outlive an already-established matching wait deadline."""
    github = _ConditionalGitHub(
        readiness={
            **_open_pr(),
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
    )
    item = _reviewed_item(make_work_item)
    item.payload.update(
        {
            "merge_readiness_head_sha": "a" * 40,
            "merge_readiness_proof_generation": 0,
            "merge_readiness_deadline_s": 1900.0,
            "merge_readiness_polls": 17,
        }
    )

    result = _complete_merge_cycle(
        MergeWaitStage(), item, make_ctx(github=github, now_fn=lambda: now)
    )

    assert result == StageOutcome(Disposition.FINISH_FAIL, "merge_readiness_timeout")
    assert github.merge_attempts == []
    assert item.payload["merge_readiness_deadline_s"] == 1900.0
    assert item.payload["merge_readiness_polls"] == 17


def test_readiness_wait_resets_its_deadline_for_a_fresh_reviewed_head(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A new review proof cannot inherit an earlier head's waiting budget."""
    fresh_head = "b" * 40
    github = _ConditionalGitHub(
        states=[_open_pr(fresh_head)],
        readiness={
            **_open_pr(fresh_head),
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BLOCKED",
        },
    )
    item = _reviewed_item(make_work_item, head=fresh_head)
    item.payload.update(
        {
            "merge_readiness_head_sha": "a" * 40,
            "merge_readiness_deadline_s": 1.0,
            "merge_readiness_polls": 17,
        }
    )

    result = _complete_merge_cycle(
        MergeWaitStage(), item, make_ctx(github=github, now_fn=lambda: 100.0)
    )

    assert result == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
    assert item.payload["merge_readiness_head_sha"] == fresh_head
    assert item.payload["merge_readiness_deadline_s"] == 1900.0
    assert item.payload["merge_readiness_polls"] == 1
    assert github.merge_attempts == []


def test_readiness_wait_resets_for_a_fresh_proof_of_the_same_head(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Fresh review of unchanged code receives a fresh bounded wait window."""
    github = _ConditionalGitHub(
        readiness={
            **_open_pr(),
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BLOCKED",
        }
    )
    item = _reviewed_item(make_work_item)
    item.payload.update(
        {
            "reviewed_pr_proof_generation": 2,
            "merge_readiness_head_sha": "a" * 40,
            "merge_readiness_proof_generation": 1,
            "merge_readiness_deadline_s": 1.0,
            "merge_readiness_polls": 17,
        }
    )

    result = _complete_merge_cycle(
        MergeWaitStage(), item, make_ctx(github=github, now_fn=lambda: 100.0)
    )

    assert result == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
    assert item.payload["merge_readiness_proof_generation"] == 2
    assert item.payload["merge_readiness_deadline_s"] == 1900.0
    assert item.payload["merge_readiness_polls"] == 1
    assert github.merge_attempts == []


def test_unknown_mergeability_waits_without_a_conditional_put(
    make_ctx: Any, make_work_item: Any
) -> None:
    """GitHub's transient mergeability calculation is operationally pending."""
    github = _ConditionalGitHub(
        readiness={
            **_open_pr(),
            "mergeable": "UNKNOWN",
            "mergeStateStatus": "CLEAN",
        }
    )

    result = _complete_merge_cycle(
        MergeWaitStage(), _reviewed_item(make_work_item), make_ctx(github=github)
    )

    assert result == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
    assert github.merge_attempts == []


def test_exhausted_merge_budget_does_not_enter_readiness_wait(
    make_ctx: Any, make_work_item: Any
) -> None:
    """No wait is useful once the next conditional request is forbidden."""
    github = _ConditionalGitHub()
    item = _reviewed_item(make_work_item)
    item.attempts["merge"] = 2

    result = MergeWaitStage().step(item, make_ctx(github=github, budget_fn=lambda _route: 2))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "merge_attempts_exhausted")
    assert github.merge_attempts == []


def test_stage_github_exposes_a_conditional_merge_adapter(make_ctx: Any) -> None:
    """The stage contract has an explicit adapter rather than a CLI merge escape hatch."""
    assert callable(getattr(make_ctx().github, "merge_pr_if_head", None))
