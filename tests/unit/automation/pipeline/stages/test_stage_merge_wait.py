"""Tests for the conditional, head-bound merge-wait stage."""

from __future__ import annotations

from typing import Any

import pytest

from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import ConditionalMergeResult, Continue, StageOutcome
from hephaestus.automation.pipeline.stages.merge_wait import MergeWaitStage
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

MERGE = "MERGE"


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
    ) -> None:
        scripted_states = states or [_open_pr()]
        super().__init__(
            pr_impl_state=labels,
            pr_state=scripted_states[-1],
            conversation_resolution=conversation_resolution,
        )
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
        self.merge_attempts: list[tuple[int, str]] = []

    def gh_pr_state(self, pr_number: int) -> dict[str, object] | None:
        del pr_number
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0]

    def merge_pr_if_head(self, pr_number: int, reviewed_sha: str) -> ConditionalMergeResult:
        self.merge_attempts.append((pr_number, reviewed_sha))
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


def test_conditional_merge_succeeds_only_after_lifecycle_confirms_merged(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A 200 response is insufficient until the terminal lifecycle read is merged."""
    github = _ConditionalGitHub(states=[_open_pr(), _open_pr(), {"state": "MERGED"}])
    ctx = make_ctx(github=github)
    ctx.config.enable_learn = False

    result = MergeWaitStage().step(_reviewed_item(make_work_item), ctx)

    assert result == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert github.merge_attempts == [(12, "a" * 40)]


@pytest.mark.parametrize("merge_state_status", ["HAS_HOOKS", "UNSTABLE"])
def test_mergeable_requestable_readiness_states_merge_successfully(
    make_ctx: Any, make_work_item: Any, merge_state_status: str
) -> None:
    """GitHub mergeable states that permit a request reach the conditional PUT."""
    github = _ConditionalGitHub(
        states=[_open_pr(), _open_pr(), {"state": "MERGED"}],
        readiness={
            **_open_pr(),
            "mergeable": "MERGEABLE",
            "mergeStateStatus": merge_state_status,
        },
    )
    ctx = make_ctx(github=github)
    ctx.config.enable_learn = False

    result = MergeWaitStage().step(_reviewed_item(make_work_item), ctx)

    assert result == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert github.merge_attempts == [(12, "a" * 40)]


def test_readiness_waits_before_the_first_conditional_merge(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Minute-scale readiness is polled without burning a conditional PUT."""
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
    ctx = make_ctx(github=github)
    ctx.config.enable_learn = False
    item = _reviewed_item(make_work_item)

    first = MergeWaitStage().step(item, ctx)

    assert first == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
    assert item.payload["retry_delay_s"] == 5.0
    assert github.merge_attempts == []

    second = MergeWaitStage().step(item, ctx)

    assert second == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert github.merge_attempts == [(12, "a" * 40)]


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

    assert stage.step(item, ctx) == StageOutcome(Disposition.RETRY, "merge_readiness_wait")

    result = stage.step(item, ctx)

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

    assert stage.step(item, ctx) == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
    assert stage.step(item, ctx) == StageOutcome(
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

    assert stage.step(item, ctx) == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
    assert stage.step(item, ctx) == StageOutcome(
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

        def merge_pr_if_head(self, pr_number: int, reviewed_sha: str) -> ConditionalMergeResult:
            self.merge_attempts.append((pr_number, reviewed_sha))
            self.merged = True
            return ConditionalMergeResult(
                status=200,
                body={"merged": True},
                transport_error=False,
                malformed=False,
                dry_run=False,
            )

    github = DelayedReadyGitHub()
    ctx = make_ctx(github=github, now_fn=lambda: now[0])
    ctx.config.enable_learn = False
    item = _reviewed_item(make_work_item)
    stage = MergeWaitStage()

    while now[0] < 10 * 60:
        result = stage.step(item, ctx)
        assert result == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
        assert github.merge_attempts == []
        now[0] += item.payload["retry_delay_s"]

    result = stage.step(item, ctx)

    assert now[0] >= 10 * 60
    assert result == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert github.merge_attempts == [(12, "a" * 40)]


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
    result = MergeWaitStage().step(item, make_ctx(github=github))

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

    result = MergeWaitStage().step(_reviewed_item(make_work_item), make_ctx(github=github))

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

    result = MergeWaitStage().step(_reviewed_item(make_work_item), make_ctx(github=github))

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

    result = MergeWaitStage().step(_reviewed_item(make_work_item), make_ctx(github=github))

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

        def merge_pr_if_head(self, pr_number: int, reviewed_sha: str) -> ConditionalMergeResult:
            assert self._thread_appeared_after_local_read
            self.merge_attempts.append((pr_number, reviewed_sha))
            return ConditionalMergeResult(
                status=405,
                body={"message": "review conversations must be resolved"},
            )

    github = ServerRejectsLateThreadGitHub()
    item = _reviewed_item(make_work_item)

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
    assert item.payload["retry_delay_s"] == 5.0
    assert github.merge_attempts == [(12, "a" * 40)]


def test_external_arm_blocks_conditional_merge_without_any_mutation(
    make_ctx: Any, make_work_item: Any
) -> None:
    """An arm observed at final admission belongs to an external actor."""
    github = _ConditionalGitHub(states=[_open_pr(auto_merge_request={"enabledAt": "elsewhere"})])

    result = MergeWaitStage().step(_reviewed_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
    assert github.merge_attempts == []
    assert github.mutation_log == []


def test_stale_proof_fails_back_without_revoking_a_label(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A stale process owns no later label and must only request fresh review."""
    github = _ConditionalGitHub(states=[_open_pr("b" * 40)])

    result = MergeWaitStage().step(_reviewed_item(make_work_item), make_ctx(github=github))

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
    ctx = make_ctx(github=github)
    ctx.config.enable_learn = False

    result = MergeWaitStage().step(_reviewed_item(make_work_item), ctx)

    assert result == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert github.merge_attempts == []


def test_already_merged_retry_preserves_post_merge_learning(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A retry that discovers a merge keeps the existing exactly-once learn path."""
    github = _ConditionalGitHub(states=[{"state": "MERGED"}])

    result = MergeWaitStage().step(_reviewed_item(make_work_item), make_ctx(github=github))

    assert result == Continue(next_state="LEARN_WAIT")
    assert github.merge_attempts == []


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
    ctx = make_ctx(github=github)
    ctx.config.enable_learn = False

    result = MergeWaitStage().step(_reviewed_item(make_work_item), ctx)

    assert result == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert github.merge_attempts == [(12, "a" * 40)]


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

    result = MergeWaitStage().step(_reviewed_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "reviewed_head_drift")
    assert github.merge_attempts == [(12, "a" * 40)]
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

    result = MergeWaitStage().step(item, make_ctx(github=github, budget_fn=lambda _route: 2))

    assert result == StageOutcome(Disposition.RETRY, "merge_not_ready")
    assert item.attempts["merge"] == 1
    assert item.payload["retry_delay_s"] == 1.0
    assert github.merge_attempts == [(12, "a" * 40)]
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

    result = MergeWaitStage().step(_reviewed_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
    assert github.merge_attempts == [(12, "a" * 40)]
    assert github.mutation_log == []


def test_restarted_merge_without_process_local_proof_fails_back_without_put(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A restarted process never reconstructs authority from durable labels alone."""
    github = _ConditionalGitHub(states=[_open_pr()])
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=MERGE)

    result = MergeWaitStage().step(item, make_ctx(github=github))

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

    result = MergeWaitStage().step(_reviewed_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "reviewed_head_drift")
    assert github.merge_attempts == [(12, "a" * 40)]
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

    result = MergeWaitStage().step(item, make_ctx(github=github, budget_fn=lambda _route: 2))

    assert result == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
    assert item.payload["retry_delay_s"] == 5.0
    assert github.merge_attempts == [(12, "a" * 40)]


@pytest.mark.parametrize("merge_state_status", ["CONFLICTING", "DIRTY"])
def test_405_conflicting_or_dirty_readiness_is_terminal(
    make_ctx: Any, make_work_item: Any, merge_state_status: str
) -> None:
    """Conflict-like GitHub readiness states are not retried as transient readiness."""
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

    result = MergeWaitStage().step(_reviewed_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "merge_conflicting")
    assert github.merge_attempts == [(12, "a" * 40)]
    assert github.mutation_log == []


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

    result = MergeWaitStage().step(_reviewed_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
    assert github.merge_attempts == [(12, "a" * 40)]
    assert github.mutation_log == []


def test_label_loss_or_non_main_base_prevents_the_conditional_request(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Final authorization facts are all required immediately before the PUT."""
    label_lost = _ConditionalGitHub(labels=(False, False), states=[_open_pr()])
    wrong_base = _ConditionalGitHub(states=[_open_pr(base="release")])

    assert MergeWaitStage().step(
        _reviewed_item(make_work_item), make_ctx(github=label_lost)
    ) == StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")
    assert MergeWaitStage().step(
        _reviewed_item(make_work_item), make_ctx(github=wrong_base)
    ) == StageOutcome(Disposition.FINISH_FAIL, "non_main_base")
    assert label_lost.merge_attempts == []
    assert wrong_base.merge_attempts == []


def test_contradictory_implementation_labels_prevent_the_conditional_request(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A contradictory durable state is not an exclusive implementation approval."""
    github = _ConditionalGitHub(labels=(True, True), states=[_open_pr()])

    result = MergeWaitStage().step(_reviewed_item(make_work_item), make_ctx(github=github))

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

    result = MergeWaitStage().step(item, make_ctx(github=github))

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

    result = MergeWaitStage().step(_reviewed_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "merge_not_merged")


def test_readiness_wait_has_a_bounded_minute_scale_deadline_without_puts(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Ordinary CI waits do not consume conditional merge attempts indefinitely."""
    github = _ConditionalGitHub(
        states=[_open_pr()],
        readiness={
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "autoMergeRequest": None,
            "baseRefName": "main",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BEHIND",
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

    for _ in range(18):
        result = stage.step(item, ctx)
        assert result == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
        assert github.merge_attempts == []
        now[0] += item.payload["retry_delay_s"]

    result = stage.step(item, ctx)

    assert result == StageOutcome(Disposition.FINISH_FAIL, "merge_readiness_timeout")
    assert github.merge_attempts == []
    assert item.payload["merge_readiness_deadline_s"] == 1900.0
    assert item.payload["merge_readiness_polls"] == 18


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

    result = MergeWaitStage().step(
        item,
        make_ctx(github=github, now_fn=lambda: 100.0),
    )

    assert result == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
    assert item.payload["merge_readiness_head_sha"] == fresh_head
    assert item.payload["merge_readiness_deadline_s"] == 1000.0
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

    result = MergeWaitStage().step(
        item,
        make_ctx(github=github, now_fn=lambda: 100.0),
    )

    assert result == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
    assert item.payload["merge_readiness_proof_generation"] == 2
    assert item.payload["merge_readiness_deadline_s"] == 1000.0
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

    result = MergeWaitStage().step(_reviewed_item(make_work_item), make_ctx(github=github))

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
