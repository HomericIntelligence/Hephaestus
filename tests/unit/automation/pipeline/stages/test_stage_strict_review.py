"""Tests for the in-loop `$athena:pr-review` handoff."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hephaestus.automation.claude_invoke import ReviewVerdict
from hephaestus.automation.pipeline.jobs import AgentJob, GitJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.base import StrictReviewEvidence
from hephaestus.automation.pipeline.stages.strict_review import (
    EVAL,
    HEAD_CHECK,
    REVIEW_WAIT,
    SR_FINISH,
    StrictReviewStage,
    parse_strict_review_verdict,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

_HEAD = "a" * 40
_EVIDENCE = StrictReviewEvidence(
    head_sha=_HEAD,
    issue_title="Task",
    issue_body="Do the task.",
    diff="diff --git a/a.py b/a.py\n+",
    prior_pr_review_verdict="Grade: A\nVerdict: GO",
)


class _StrictReviewGuard:
    """In-memory strict-review ownership fake for concurrency contracts."""

    def __init__(self) -> None:
        self.owners: dict[tuple[str, str, int], int] = {}

    def try_claim(self, org: str, repo: str, pr_number: int, owner: int) -> bool:
        key = (org, repo, pr_number)
        current = self.owners.get(key)
        if current is not None and current != owner:
            return False
        self.owners[key] = owner
        return True


def test_same_head_strict_review_loser_retries_without_a_second_review(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Only one same-PR strict-review item may proceed through the gate."""
    guard = _StrictReviewGuard()
    config = SimpleNamespace(strict_review_guard=guard)
    github = FakeStageGitHub(pr_state={"state": "OPEN", "headRefOid": _HEAD})
    first = make_work_item(stage=StageName.STRICT_REVIEW, pr=12, state="ENTER")
    second = make_work_item(stage=StageName.STRICT_REVIEW, pr=12, state="ENTER")
    stage = StrictReviewStage()

    assert stage.step(first, make_ctx(config=config, github=github)) == Continue(
        next_state=HEAD_CHECK
    )
    assert stage.step(second, make_ctx(config=config, github=github)) == StageOutcome(
        Disposition.RETRY, "strict_review_busy"
    )


def test_strict_review_without_an_ownership_guard_fails_closed(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A production entrypoint may never silently run strict review unguarded."""
    item = make_work_item(stage=StageName.STRICT_REVIEW, pr=12, state="ENTER")
    config = SimpleNamespace(strict_review_guard=None)

    assert StrictReviewStage().step(item, make_ctx(config=config)) == StageOutcome(
        Disposition.FINISH_FAIL, "strict_review_guard_unavailable"
    )


def test_orphan_strict_review_fails_before_gate_side_effects(
    make_ctx: Any, make_work_item: Any
) -> None:
    """An orphan cannot claim, contain, or dispatch the strict-review gate."""
    item = make_work_item(issue=None, stage=StageName.STRICT_REVIEW, pr=12, state="ENTER")
    github = FakeStageGitHub(pr_state={"state": "OPEN", "headRefOid": _HEAD})

    assert StrictReviewStage().on_enter(item, make_ctx(github=github)) == StageOutcome(
        Disposition.FINISH_FAIL, "strict_review_orphan"
    )
    assert github.mutation_log == []


def test_review_job_uses_athena_pr_review_prompt(make_ctx: Any, make_work_item: Any) -> None:
    """The approval reviewer is explicitly the Athena PR-review skill."""
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=REVIEW_WAIT,
        payload={"strict_review_head": _HEAD, "strict_review_worktree": "/review/strict-12"},
    )
    github = FakeStageGitHub(strict_evidence=_EVIDENCE)

    result = StrictReviewStage().step(item, make_ctx(github=github))

    assert isinstance(result, JobRequest)
    assert isinstance(result.job, AgentJob)
    prompt = result.job.prompt_builder(**result.job.prompt_kwargs)
    assert "MUST invoke `$athena:pr-review --ci-free 12`" in prompt
    assert "Automation-loop handoff: <GO|NOGO>" in prompt
    assert "PR-review-specific graded dimensions" not in prompt
    assert "CI-free" in prompt
    assert "collect_evidence.py" in prompt
    assert "operator-authorized CI-free profile" in prompt
    assert "sole durable merge authorization" in prompt
    assert "do not propose, require, or implement a durable head-bound approval record" in prompt
    assert "cached handoff" in prompt
    assert result.job.sandbox == "read-only"
    assert result.job.agent == "codex"


def test_direct_pr_review_requests_an_isolated_detached_worktree(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A direct review must not reuse a writer checkout of the PR branch (#2276)."""
    item = make_work_item(stage=StageName.STRICT_REVIEW, pr=12, state=HEAD_CHECK)
    github = FakeStageGitHub(
        pr_state={"state": "OPEN", "headRefOid": _HEAD},
        pr_head_branch="12-auto-impl",
    )

    result = StrictReviewStage().step(item, make_ctx(github=github))

    assert isinstance(result, JobRequest)
    assert isinstance(result.job, GitJob)
    assert result.job.op == "create_worktree"
    assert result.job.kwargs["isolated"] is True
    assert result.job.kwargs["issue_number"] == 12
    assert result.job.kwargs["branch_name"] == "12-auto-impl"


def test_same_issue_prs_request_distinct_isolated_review_paths(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Concurrent PRs for one issue must not share a strict-review worktree (#2276)."""
    first = make_work_item(stage=StageName.STRICT_REVIEW, issue=1, pr=12, state=HEAD_CHECK)
    second = make_work_item(stage=StageName.STRICT_REVIEW, issue=1, pr=13, state=HEAD_CHECK)
    first_result = StrictReviewStage().step(
        first,
        make_ctx(
            github=FakeStageGitHub(
                pr_state={"state": "OPEN", "headRefOid": _HEAD},
                pr_head_branch="12-auto-impl",
            )
        ),
    )
    second_result = StrictReviewStage().step(
        second,
        make_ctx(
            github=FakeStageGitHub(
                pr_state={"state": "OPEN", "headRefOid": _HEAD},
                pr_head_branch="13-auto-impl",
            )
        ),
    )

    assert isinstance(first_result, JobRequest)
    assert isinstance(second_result, JobRequest)
    assert isinstance(first_result.job, GitJob)
    assert isinstance(second_result.job, GitJob)
    assert first_result.job.kwargs["issue_number"] == 12
    assert second_result.job.kwargs["issue_number"] == 13


def test_isolated_review_worktree_preserves_implementation_writer(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Strict review records its disposable checkout without replacing the writer (#2276)."""
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=HEAD_CHECK,
    )
    item.worktree = "/writer/issue-12"
    github = FakeStageGitHub(
        pr_state={"state": "OPEN", "headRefOid": _HEAD},
        pr_head_branch="12-auto-impl",
    )
    ctx = make_ctx(github=github)

    result = StrictReviewStage().step(item, ctx)

    assert isinstance(result, JobRequest)
    StrictReviewStage().on_job_done(
        item,
        JobResult(ok=True, value={"path": "/review/strict-review-12", "dirty": False}),
        ctx,
    )
    assert item.worktree == "/writer/issue-12"
    assert item.payload["strict_review_worktree"] == "/review/strict-review-12"

    item.state = REVIEW_WAIT
    item.payload["strict_review_head"] = _HEAD
    review_job = StrictReviewStage().step(
        item,
        make_ctx(github=FakeStageGitHub(strict_evidence=_EVIDENCE)),
    )
    assert isinstance(review_job, JobRequest)
    assert isinstance(review_job.job, AgentJob)
    assert review_job.job.cwd == Path("/review/strict-review-12")


def test_skill_handoff_must_be_the_final_line() -> None:
    """Only the explicit post-skill handoff can grant in-loop approval."""
    good = parse_strict_review_verdict("Skill report\nAutomation-loop handoff: GO\n")
    quoted = parse_strict_review_verdict("Automation-loop handoff: GO\ntrailing text")

    assert good.verdict == "GO"
    assert quoted.verdict == "AMBIGUOUS"


def test_go_labels_current_head_for_merge_wait(make_ctx: Any, make_work_item: Any) -> None:
    """A current-head review GO is the loop's sole label producer."""
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=EVAL,
        payload={
            "issue_title": "Issue title",
            "issue_body": "Issue body",
            "advise_findings": "Findings",
            "marketplace_path": "/marketplace",
            "existing_pr": 12,
            "entry_stage": "repo",
            "entry_reason": "seeded",
            "_fail_backs": 0,
            "direct_pr_worktree": "/review/direct-12",
            "strict_review_head": _HEAD,
            "strict_review_attempt": 1,
            "strict_review_verdict": "GO",
            "strict_review_text": "Grade: A\nVerdict: GO",
            "strict_review_worktree": "/review/strict-12",
            "strict_review_worktree_head": _HEAD,
            # Alternate names are equally prohibited as a MergeWait handoff.
            "reviewed_head": _HEAD,
            "approval_head": _HEAD,
            "reviewed_sha": _HEAD,
            "approval_sha": _HEAD,
            "strict_review_commit": _HEAD,
            "review_evidence_sha": _HEAD,
            "proof_sha": _HEAD,
            "approval_ref": "reviewed-branch",
            "validated_sha": _HEAD,
            "gate_revision": _HEAD,
            "attestation": _HEAD,
            # Unknown spellings and forged ingress bookkeeping cannot escape
            # the closed GO handoff allowlist either.
            "approved_head": _HEAD,
            "authorization_sha": _HEAD,
            "audit_stamp": _HEAD,
            "_strict_review_entry_payload_keys": ("strict_review_head",),
        },
    )
    github = FakeStageGitHub(pr_state={"state": "OPEN", "headRefOid": _HEAD})

    result = StrictReviewStage().step(item, make_ctx(github=github))

    assert isinstance(result, Continue)
    assert ("mark_pr_implementation_go", (12,)) in github.mutation_log
    # The current-head value is only a transient check while this stage
    # applies GO.  It must not cross into merge_wait where it could become a
    # second authorization condition beside the loop-owned label.
    assert (
        not {
            "strict_review_head",
            "strict_review_attempt",
            "strict_review_verdict",
            "strict_review_text",
            "strict_review_worktree_head",
            "reviewed_head",
            "approval_head",
            "reviewed_sha",
            "approval_sha",
            "strict_review_commit",
            "review_evidence_sha",
            "proof_sha",
            "approval_ref",
            "validated_sha",
            "gate_revision",
            "attestation",
            "approved_head",
            "authorization_sha",
            "audit_stamp",
            "_strict_review_entry_payload_keys",
        }
        & item.payload.keys()
    )
    assert item.payload["strict_review_worktree"] == "/review/strict-12"
    assert "_strict_review_guard_owner" in item.payload
    assert {
        key: item.payload[key]
        for key in (
            "issue_title",
            "issue_body",
            "advise_findings",
            "marketplace_path",
            "existing_pr",
            "entry_stage",
            "entry_reason",
            "_fail_backs",
            "direct_pr_worktree",
        )
    } == {
        "issue_title": "Issue title",
        "issue_body": "Issue body",
        "advise_findings": "Findings",
        "marketplace_path": "/marketplace",
        "existing_pr": 12,
        "entry_stage": "repo",
        "entry_reason": "seeded",
        "_fail_backs": 0,
        "direct_pr_worktree": "/review/direct-12",
    }
    assert set(item.payload) == {
        "issue_title",
        "issue_body",
        "advise_findings",
        "marketplace_path",
        "existing_pr",
        "entry_stage",
        "entry_reason",
        "_fail_backs",
        "direct_pr_worktree",
        "strict_review_worktree",
        "_strict_review_guard_owner",
    }


def test_strict_review_uses_codex_even_when_the_loop_agent_is_claude(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Athena review is never run with an unenforceable Claude shell boundary."""
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=REVIEW_WAIT,
        payload={"strict_review_head": _HEAD, "strict_review_worktree": "/review/strict-12"},
    )
    github = FakeStageGitHub(strict_evidence=_EVIDENCE)

    result = StrictReviewStage().step(item, make_ctx(github=github))

    assert isinstance(result, JobRequest)
    assert isinstance(result.job, AgentJob)
    assert result.job.agent == "codex"


def test_strict_review_applies_reviewer_reasoning_effort_with_forced_codex_provider(
    make_ctx: Any, make_work_item: Any
) -> None:
    """The forced Codex review job still receives reviewer reasoning overrides."""
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=REVIEW_WAIT,
        payload={"strict_review_head": _HEAD, "strict_review_worktree": "/review/strict-12"},
    )
    config = SimpleNamespace(
        strict_review_guard=_StrictReviewGuard(),
        agent="claude",
        model="",
        reviewer_model="terra:default",
        reviewer_reasoning_effort="xhigh",
    )
    github = FakeStageGitHub(strict_evidence=_EVIDENCE)

    result = StrictReviewStage().step(item, make_ctx(config=config, github=github))

    assert isinstance(result, JobRequest)
    assert isinstance(result.job, AgentJob)
    assert result.job.agent == "codex"
    assert result.job.model == "terra:xhigh"


def test_strict_review_ingress_disarms_before_revoking_the_go_label(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A prior arm cannot merge in a label-write-before-deferral window."""
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state="ENTER",
    )
    github = FakeStageGitHub(
        pr_state={"state": "OPEN", "headRefOid": _HEAD, "autoMergeRequest": None}
    )

    assert StrictReviewStage().on_enter(item, make_ctx(github=github)) is None

    actions = [action for action, _args in github.mutation_log]
    assert actions.index("defer_auto_merge") < actions.index("gh_issue_remove_labels")


def test_strict_review_ingress_rechecks_after_revoking_the_go_label(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A same-head re-arm during label removal cannot survive into review."""

    class RearmDuringLabelRemovalGitHub(FakeStageGitHub):
        def defer_auto_merge(self, pr_number: int) -> None:
            super().defer_auto_merge(pr_number)
            self._pr_state = {"state": "OPEN", "headRefOid": _HEAD, "autoMergeRequest": None}

        def remove_labels(self, pr_number: int, labels: list[str]) -> None:
            super().remove_labels(pr_number, labels)
            self._pr_state = {
                "state": "OPEN",
                "headRefOid": _HEAD,
                "autoMergeRequest": {"enabledAt": "raced"},
            }

    item = make_work_item(stage=StageName.STRICT_REVIEW, pr=12, state="ENTER")
    github = RearmDuringLabelRemovalGitHub(
        pr_state={"state": "OPEN", "headRefOid": _HEAD, "autoMergeRequest": None}
    )

    assert StrictReviewStage().on_enter(item, make_ctx(github=github)) is None

    assert [action for action, _args in github.mutation_log].count("defer_auto_merge") == 2
    state = github.gh_pr_state(12)
    assert state is not None
    assert state["autoMergeRequest"] is None


def test_strict_review_ingress_rechecks_after_go_label_removal_error(
    make_ctx: Any, make_work_item: Any
) -> None:
    """An ambiguous failed label RPC cannot leave a same-head re-arm live."""

    class RearmAndFailLabelRemovalGitHub(FakeStageGitHub):
        def defer_auto_merge(self, pr_number: int) -> None:
            super().defer_auto_merge(pr_number)
            self._pr_state = {"state": "OPEN", "headRefOid": _HEAD, "autoMergeRequest": None}

        def remove_labels(self, pr_number: int, labels: list[str]) -> None:
            super().remove_labels(pr_number, labels)
            self._pr_state = {
                "state": "OPEN",
                "headRefOid": _HEAD,
                "autoMergeRequest": {"enabledAt": "raced"},
            }
            raise RuntimeError("label response lost")

    item = make_work_item(stage=StageName.STRICT_REVIEW, pr=12, state="ENTER")
    github = RearmAndFailLabelRemovalGitHub(
        pr_state={"state": "OPEN", "headRefOid": _HEAD, "autoMergeRequest": None}
    )

    assert StrictReviewStage().on_enter(item, make_ctx(github=github)) == StageOutcome(
        Disposition.FINISH_FAIL, "implementation_go_revoke_failed"
    )

    assert [action for action, _args in github.mutation_log].count("defer_auto_merge") == 2
    state = github.gh_pr_state(12)
    assert state is not None
    assert state["autoMergeRequest"] is None


def test_go_label_remains_the_authorization_when_a_push_races_the_label_write(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A later push cannot make discarded review state override the GO label."""

    class PushDuringLabelGitHub(FakeStageGitHub):
        def mark_pr_implementation_go(self, pr_number: int) -> None:
            super().mark_pr_implementation_go(pr_number)
            self._pr_state = {"state": "OPEN", "headRefOid": "b" * 40}

    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=EVAL,
        payload={
            "strict_review_head": _HEAD,
            "strict_review_attempt": 1,
            "strict_review_verdict": "GO",
            "strict_review_text": "Grade: A\nVerdict: GO",
            "strict_review_worktree_head": _HEAD,
        },
    )
    github = PushDuringLabelGitHub(pr_state={"state": "OPEN", "headRefOid": _HEAD})

    result = StrictReviewStage().step(item, make_ctx(github=github))

    assert result == Continue(next_state=SR_FINISH)
    assert ("mark_pr_implementation_go", (12,)) in github.mutation_log
    assert ("defer_auto_merge", (12,)) not in github.mutation_log
    assert not any(action == "gh_issue_remove_labels" for action, _ in github.mutation_log)
    assert "strict_review_attempt" not in item.payload
    assert "strict_review_worktree_head" not in item.payload


def test_job_result_keeps_final_verdict(make_ctx: Any, make_work_item: Any) -> None:
    """A worker result is preserved for the stage's current-head decision."""
    item = make_work_item(stage=StageName.STRICT_REVIEW, pr=12, state=REVIEW_WAIT)
    stage = StrictReviewStage()

    stage.on_job_done(
        item,
        JobResult(
            ok=True,
            value=ReviewVerdict(grade="A", verdict="GO", raw="Grade: A\nVerdict: GO"),
        ),
        make_ctx(),
    )

    assert item.payload["strict_review_verdict"] == "GO"


def test_nogo_disables_auto_merge_and_returns_to_implementation(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A failed in-loop review keeps the head ineligible and gives actionable feedback."""
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=EVAL,
        payload={
            "strict_review_head": _HEAD,
            "strict_review_verdict": "NOGO",
            "strict_review_text": "Missing a regression test.\nGrade: C\nVerdict: NOGO",
        },
    )
    github = FakeStageGitHub(pr_state={"state": "OPEN", "headRefOid": _HEAD})

    result = StrictReviewStage().step(item, make_ctx(github=github))

    assert isinstance(result, StageOutcome)
    assert result.disposition is Disposition.FAIL_BACK
    assert result.note == "nogo"
    assert ("defer_auto_merge", (12,)) in github.mutation_log
    assert ("mark_pr_implementation_no_go", (12,)) in github.mutation_log
    assert any("Missing a regression test." in body for body in github.comments[12])


def test_nogo_rechecks_after_go_label_revocation(make_ctx: Any, make_work_item: Any) -> None:
    """A same-head re-arm during NOGO containment remains disarmed."""

    class RearmDuringGoRevocationGitHub(FakeStageGitHub):
        def defer_auto_merge(self, pr_number: int) -> None:
            super().defer_auto_merge(pr_number)
            self._pr_state = {"state": "OPEN", "headRefOid": _HEAD, "autoMergeRequest": None}

        def remove_labels(self, pr_number: int, labels: list[str]) -> None:
            super().remove_labels(pr_number, labels)
            self._pr_state = {
                "state": "OPEN",
                "headRefOid": _HEAD,
                "autoMergeRequest": {"enabledAt": "raced"},
            }

    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=EVAL,
        payload={
            "strict_review_head": _HEAD,
            "strict_review_verdict": "NOGO",
            "strict_review_text": "Missing a regression test.\nGrade: C\nVerdict: NOGO",
        },
    )
    github = RearmDuringGoRevocationGitHub(
        pr_state={"state": "OPEN", "headRefOid": _HEAD, "autoMergeRequest": None}
    )

    assert StrictReviewStage().step(item, make_ctx(github=github)) == StageOutcome(
        Disposition.FAIL_BACK, "nogo"
    )

    assert [action for action, _args in github.mutation_log].count("defer_auto_merge") == 2
    state = github.gh_pr_state(12)
    assert state is not None
    assert state["autoMergeRequest"] is None


def test_nogo_rechecks_after_go_label_removal_error(make_ctx: Any, make_work_item: Any) -> None:
    """NOGO containment repeats deferral after an ambiguous label error."""

    class RearmAndFailLabelRemovalGitHub(FakeStageGitHub):
        def defer_auto_merge(self, pr_number: int) -> None:
            super().defer_auto_merge(pr_number)
            self._pr_state = {"state": "OPEN", "headRefOid": _HEAD, "autoMergeRequest": None}

        def remove_labels(self, pr_number: int, labels: list[str]) -> None:
            super().remove_labels(pr_number, labels)
            self._pr_state = {
                "state": "OPEN",
                "headRefOid": _HEAD,
                "autoMergeRequest": {"enabledAt": "raced"},
            }
            raise RuntimeError("label response lost")

    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=EVAL,
        payload={
            "strict_review_head": _HEAD,
            "strict_review_verdict": "NOGO",
            "strict_review_text": "Missing a regression test.\nGrade: C\nVerdict: NOGO",
        },
    )
    github = RearmAndFailLabelRemovalGitHub(
        pr_state={"state": "OPEN", "headRefOid": _HEAD, "autoMergeRequest": None}
    )

    assert StrictReviewStage().step(item, make_ctx(github=github)) == StageOutcome(
        Disposition.FINISH_FAIL, "auto_merge_disable_failed"
    )

    assert [action for action, _args in github.mutation_log].count("defer_auto_merge") == 2
    state = github.gh_pr_state(12)
    assert state is not None
    assert state["autoMergeRequest"] is None


def test_nogo_push_during_containment_restarts_without_stale_feedback(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A NOGO for H1 cannot annotate or label a pushed H2."""

    class PushDuringContainmentGitHub(FakeStageGitHub):
        def defer_auto_merge(self, pr_number: int) -> None:
            super().defer_auto_merge(pr_number)
            self._pr_state = {"state": "OPEN", "headRefOid": "b" * 40}

    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=EVAL,
        payload={
            "strict_review_head": _HEAD,
            "strict_review_verdict": "NOGO",
            "strict_review_text": "Missing a regression test.\nGrade: C\nVerdict: NOGO",
        },
    )
    github = PushDuringContainmentGitHub(pr_state={"state": "OPEN", "headRefOid": _HEAD})

    result = StrictReviewStage().step(item, make_ctx(github=github))

    assert result == Continue(next_state=HEAD_CHECK)
    assert ("mark_pr_implementation_no_go", (12,)) not in github.mutation_log
    assert github.comments.get(12, []) == []


def test_nogo_push_during_label_write_revokes_stale_label(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A push after NOGO remediation cannot retain H1's no-go label on H2."""

    class PushDuringNoGoLabelGitHub(FakeStageGitHub):
        def mark_pr_implementation_no_go(self, pr_number: int) -> None:
            super().mark_pr_implementation_no_go(pr_number)
            self._pr_state = {"state": "OPEN", "headRefOid": "b" * 40}

    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=EVAL,
        payload={
            "strict_review_head": _HEAD,
            "strict_review_verdict": "NOGO",
            "strict_review_text": "Missing a regression test.\nGrade: C\nVerdict: NOGO",
        },
    )
    github = PushDuringNoGoLabelGitHub(pr_state={"state": "OPEN", "headRefOid": _HEAD})

    result = StrictReviewStage().step(item, make_ctx(github=github))

    assert result == Continue(next_state=HEAD_CHECK)
    assert ("mark_pr_implementation_no_go", (12,)) in github.mutation_log
    assert any(action == "gh_issue_remove_labels" for action, _ in github.mutation_log)


def test_head_drift_restarts_review_without_reusing_old_verdict(
    make_ctx: Any, make_work_item: Any
) -> None:
    """The loop never reuses a PR-review verdict for a different head."""
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=EVAL,
        payload={
            "strict_review_head": _HEAD,
            "strict_review_verdict": "GO",
            "strict_review_text": "Grade: A\nVerdict: GO",
        },
    )
    github = FakeStageGitHub(pr_state={"state": "OPEN", "headRefOid": "b" * 40})

    result = StrictReviewStage().step(item, make_ctx(github=github))

    assert result == Continue(next_state=HEAD_CHECK)
    assert ("defer_auto_merge", (12,)) in github.mutation_log


def test_missing_context_is_a_nogo(make_ctx: Any, make_work_item: Any) -> None:
    """Incomplete review context cannot apply loop-owned approval."""
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=12,
        state=REVIEW_WAIT,
        payload={"strict_review_head": _HEAD, "strict_review_worktree": "/review/strict-12"},
    )
    github = FakeStageGitHub(pr_state={"state": "OPEN", "headRefOid": _HEAD})

    result = StrictReviewStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "nogo")
