"""Tests for the planning stage (doc section "2. planning")."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import hephaestus.automation.github_api as github_api_mod
import hephaestus.automation.pipeline_github as pg
from hephaestus.automation.issue_waves import (
    WAVE_LEASE_PAYLOAD,
    WAVE_NON_CODE_INTENT_PAYLOAD,
    WAVE_NON_CODE_PAYLOAD,
    IssueWaveStore,
)
from hephaestus.automation.pipeline.athena_skill_jobs import AthenaSkillJob, AthenaSkillResult
from hephaestus.automation.pipeline.jobs import AgentJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import (
    Continue,
    JobRequest,
    StageOutcome,
)
from hephaestus.automation.pipeline.stages.planning import (
    PlanningStage,
    build_plan_prompt,
)
from hephaestus.automation.prompts._shared import get_untrusted_notice
from hephaestus.automation.prompts.planning import get_plan_prompt
from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_COMMENT_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
)
from hephaestus.automation.requirements_recovery import (
    ATHENA_FINALIZED_PLAN_PREFIX,
    RECOVERY_PROVENANCE_PREFIX,
    RecoveredRequirements,
    RecoveryDisposition,
    RecoveryReview,
    RecoveryVerdict,
    evidence_digest,
    parse_recovery_provenance,
    render_recovered_requirements,
)
from hephaestus.automation.review_journal import (
    FORCED_PLANNING_EPOCH_MARKER,
    IssueComment,
    PlanDiscoveryResult,
    plan_fingerprint,
    render_current_plan,
    render_current_review,
)
from hephaestus.automation.state_labels import (
    ATHENA_FINALIZED_PLAN_LABEL,
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
    STATE_NEEDS_PLAN,
    STATE_PLAN_BLOCKED,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
    STATE_SKIP,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

_RECOVERY_REVISION = "d" * 40


def _recovery_binding(
    source: str,
    *,
    issue: int,
    title: str = "A task",
    repo: str = "test-repo",
) -> str:
    return evidence_digest(repo, issue, _RECOVERY_REVISION, title, source)


def _recovered_body(
    source: str,
    requirements: str,
    *,
    issue: int,
    title: str = "A task",
    repo: str = "test-repo",
    source_digest: str | None = None,
    successor_revision: int | None = None,
    successor_plan_digest: str | None = None,
) -> str:
    return render_recovered_requirements(
        source,
        requirements,
        _recovery_binding(source, issue=issue, title=title, repo=repo),
        source_digest=source_digest,
        successor_revision=successor_revision,
        successor_plan_digest=successor_plan_digest,
        issue_title=title,
        repository_revision=_RECOVERY_REVISION,
    )


def _bind_recovery_revision(item: Any) -> None:
    item.payload["_synced_default_branch_sha"] = _RECOVERY_REVISION


def _fence_present(prompt: str, label: str) -> bool:
    """Return True when a prompt has nonce-delimited markers for label."""
    return bool(
        re.search(rf"BEGIN_[0-9A-F]+_{label}\b", prompt)
        and re.search(rf"END_[0-9A-F]+_{label}\b", prompt)
    )


def _finalized_body(content: str = "## Why\n\nUse the reviewed implementation plan.") -> str:
    """Return a self-verifying finalized planning body fixture."""
    placeholder = (
        f"{content}\n\n"
        f"{ATHENA_FINALIZED_PLAN_PREFIX}R={'a' * 64} P=123456789:{'b' * 64} "
        f"V=987654321:{'c' * 64} F=<F> -->"
    )
    digest = hashlib.sha256(placeholder.encode("utf-8")).hexdigest()
    return placeholder.replace("F=<F>", f"F={digest}")


class TestBuildPlanPrompt:
    """build_plan_prompt composes the plan prompt with the advise block."""

    def test_without_findings_includes_issue_context(self) -> None:
        """The planner prompt carries fenced TASK title/body before the template."""
        prompt = build_plan_prompt(7, "Retry failure", "The loop retries forever.")

        assert get_untrusted_notice() in prompt
        assert _fence_present(prompt, "ISSUE_TITLE")
        assert _fence_present(prompt, "ISSUE_BODY")
        assert "Retry failure" in prompt
        assert "The loop retries forever." in prompt
        assert prompt.endswith(get_plan_prompt(7))

    def test_with_findings_appends_learnings_block(self) -> None:
        """Advise findings ride in a fenced learnings block."""
        prompt = build_plan_prompt(
            7,
            "Retry failure",
            "The loop retries forever.",
            "Use the retry helper from utils.",
        )

        assert "## Prior Learnings from Team Knowledge Base (untrusted)" in prompt
        assert _fence_present(prompt, "ADVISE_FINDINGS")
        assert "Use the retry helper from utils." in prompt
        assert prompt.endswith(get_plan_prompt(7))

    def test_resume_history_is_fenced_as_untrusted(self) -> None:
        prompt = build_plan_prompt(
            7,
            "Retry failure",
            "The loop retries forever.",
            issue_history="Plan 1\nReview 1\nHuman feedback",
        )

        assert _fence_present(prompt, "ISSUE_HISTORY")
        assert "Human feedback" in prompt


class TestPlanningStageEnter:
    """on_enter idempotency guards and fast-forward checks."""

    def test_plan_go_fast_forward_advance(self, make_ctx: Any, make_work_item: Any) -> None:
        """At-or-past state:plan-go advances immediately with zero jobs/writes."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_GO])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=1)

        outcome = stage.on_enter(item, ctx)

        assert outcome is not None
        assert outcome.disposition == Disposition.ADVANCE
        assert github.mutation_log == []  # no mutations on fast-forward

    def test_contaminated_body_recovers_before_plan_go_fast_forward(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = PlanningStage()
        github = FakeStageGitHub(
            labels=[STATE_PLAN_GO],
            issue_body=f"{PLAN_CANONICAL_MARKER}\nStale derived plan",
            has_plan=True,
        )
        ctx = make_ctx(github=github)
        item = make_work_item(issue=101)

        outcome = stage.on_enter(item, ctx)

        assert outcome is None
        assert item.payload["requirements_recovery_required"] is True
        assert item.payload["requirements_recovery_contaminated"] is True
        assert github.mutation_log == []

    def test_verified_finalized_plan_bypasses_force_and_normalizes_plan_go(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A sealed Athena planning epoch is already reviewed planning authority."""
        body = _finalized_body()
        github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO], issue_body=body)
        item = make_work_item(issue=102)

        outcome = PlanningStage().on_enter(
            item,
            make_ctx(github=github, config=SimpleNamespace(force=True)),
        )

        assert outcome is not None
        assert outcome.disposition is Disposition.ADVANCE
        assert github.labels[102] == {STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL}
        assert (
            item.payload["athena_finalized_plan_digest"]
            == hashlib.sha256(
                body.replace(
                    f"F={item.payload['athena_finalized_plan_digest']}",
                    "F=<F>",
                ).encode("utf-8")
            ).hexdigest()
        )
        assert github.mutation_log == [
            (
                "edit_labels",
                (
                    102,
                    (STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL),
                    (
                        STATE_PLAN_NO_GO,
                        STATE_NEEDS_PLAN,
                        STATE_IMPLEMENTATION_NO_GO,
                        STATE_IMPLEMENTATION_GO,
                        STATE_PLAN_BLOCKED,
                    ),
                ),
            )
        ]

    def test_verified_finalized_plan_replaces_stale_blocked_latch(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Authenticated finalization completes planning despite a stale hold."""
        github = FakeStageGitHub(
            labels=[STATE_PLAN_BLOCKED],
            issue_body=_finalized_body(),
        )

        outcome = PlanningStage().on_enter(
            make_work_item(issue=108),
            make_ctx(github=github),
        )

        assert outcome is not None
        assert outcome.disposition is Disposition.ADVANCE
        assert github.labels[108] == {STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL}
        assert github.comments.get(108, []) == []

    def test_verified_finalized_plan_preserves_unrelated_operator_skip(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Finalization cannot revive an issue without skip-transition provenance."""
        github = FakeStageGitHub(
            labels=[STATE_SKIP],
            issue_body=_finalized_body(),
        )

        outcome = PlanningStage().on_enter(
            make_work_item(issue=122),
            make_ctx(github=github),
        )

        assert outcome is not None
        assert outcome.disposition is Disposition.SKIP
        assert github.labels[122] == {STATE_SKIP}
        assert github.mutation_log == []

    def test_finalized_plan_label_failure_exhausts_without_poisoning_coordinator(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Finalization normalization consumes the bounded plan budget."""

        class FailingLabelsGitHub(FakeStageGitHub):
            def edit_labels(
                self,
                issue_number: int,
                *,
                add: list[str],
                remove: list[str],
            ) -> None:
                raise RuntimeError("label mutation unavailable")

        item = make_work_item(issue=115)
        outcome = PlanningStage().on_enter(
            item,
            make_ctx(
                github=FailingLabelsGitHub(
                    labels=[STATE_PLAN_NO_GO],
                    issue_body=_finalized_body(),
                ),
                budget_fn=lambda _name: 1,
            ),
        )

        assert outcome is not None
        assert outcome.disposition is Disposition.FINISH_FAIL
        assert item.attempts["plan"] == 1
        assert "without revoking authority" in outcome.note

    def test_finalized_plan_normalization_retries_through_stale_blocked_latch(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A transient write failure cannot turn authenticated finalization into BLOCKED."""

        class FailsFirstNormalizationGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__(labels=[STATE_PLAN_BLOCKED], issue_body=_finalized_body())
                self.edits = 0

            def edit_labels(
                self,
                issue_number: int,
                *,
                add: list[str],
                remove: list[str],
            ) -> None:
                self.edits += 1
                if self.edits == 1:
                    raise RuntimeError("transient label failure")
                super().edit_labels(issue_number, add=add, remove=remove)

        github = FailsFirstNormalizationGitHub()
        item = make_work_item(issue=117)
        ctx = make_ctx(github=github, budget_fn=lambda _name: 2)

        first = PlanningStage().on_enter(item, ctx)
        second = PlanningStage().on_enter(item, ctx)

        assert first is not None and first.disposition is Disposition.RETRY
        assert second is not None and second.disposition is Disposition.ADVANCE
        assert item.attempts["plan"] == 1
        assert github.labels[117] == {STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL}

    def test_finalized_plan_retry_accepts_eventually_visible_normalization(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A committed label edit must not exhaust when only its first readback fails."""

        class DelayedReadbackGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__(labels=[STATE_PLAN_NO_GO], issue_body=_finalized_body())
                self.fail_next_readback = False

            def edit_labels(
                self,
                issue_number: int,
                *,
                add: list[str],
                remove: list[str],
            ) -> None:
                super().edit_labels(issue_number, add=add, remove=remove)
                self.fail_next_readback = True

            def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
                if self.fail_next_readback:
                    self.fail_next_readback = False
                    raise RuntimeError("eventually consistent readback")
                return super().gh_issue_json(issue_number)

        item = make_work_item(issue=119)
        outcome = PlanningStage().on_enter(
            item,
            make_ctx(github=DelayedReadbackGitHub(), budget_fn=lambda _name: 1),
        )

        assert outcome is not None and outcome.disposition is Disposition.ADVANCE
        assert item.attempts.get("plan", 0) == 0

    def test_finalized_plan_body_drift_during_normalization_cannot_advance(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Fresh body/editor rebinding defeats labels normalized for stale authority."""

        class BodyDriftsDuringNormalizationGitHub(FakeStageGitHub):
            def edit_labels(
                self,
                issue_number: int,
                *,
                add: list[str],
                remove: list[str],
            ) -> None:
                super().edit_labels(issue_number, add=add, remove=remove)
                self._issue_body = "Human replacement requirements"
                self._issue_body_owned_by_viewer = False

        github = BodyDriftsDuringNormalizationGitHub(
            labels=[STATE_PLAN_NO_GO],
            issue_body=_finalized_body(),
        )
        item = make_work_item(issue=127)

        outcome = PlanningStage().on_enter(
            item,
            make_ctx(github=github, budget_fn=lambda _name: 2),
        )

        assert outcome is not None and outcome.disposition is Disposition.RETRY
        assert item.attempts["plan"] == 1
        assert "finalized-plan-reused" not in item.payload.get("summary_actions", [])

        restarted = PlanningStage().on_enter(item, make_ctx(github=github))

        assert restarted is None
        assert item.payload["athena_finalized_plan_invalidated"] is True

    def test_sanitized_finalized_body_fails_closed(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A sanitized projection cannot authenticate the complete issue body."""

        class SanitizedAuthorityGitHub(FakeStageGitHub):
            def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
                snapshot = super().gh_issue_json(issue_number)
                snapshot["authoritySanitized"] = True
                return snapshot

        github = SanitizedAuthorityGitHub(
            labels=[STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL],
            issue_body=_finalized_body(),
        )
        item = make_work_item(issue=128)

        outcome = PlanningStage().on_enter(
            item,
            make_ctx(github=github, budget_fn=lambda _name: 2),
        )

        assert outcome is not None and outcome.disposition is Disposition.RETRY
        assert "authority-bearing issue text required sanitization" in outcome.note
        assert "finalized-plan-reused" not in item.payload.get("summary_actions", [])

    @pytest.mark.parametrize(
        ("body", "owned_by_viewer"),
        [
            (_finalized_body(), False),
            (f"{_finalized_body()}\nmaterial drift", True),
        ],
    )
    def test_invalid_finalized_plan_preserves_blocked_latch(
        self,
        make_ctx: Any,
        make_work_item: Any,
        body: str,
        owned_by_viewer: bool,
    ) -> None:
        """Only authenticated, intact finalization may supersede an operator hold."""
        github = FakeStageGitHub(
            labels=[STATE_PLAN_BLOCKED],
            issue_body=body,
            issue_body_owned_by_viewer=owned_by_viewer,
        )

        outcome = PlanningStage().on_enter(
            make_work_item(issue=109),
            make_ctx(github=github),
        )

        assert outcome is not None
        assert outcome.disposition is Disposition.BLOCKED
        assert github.labels[109] == {STATE_PLAN_BLOCKED}
        assert len(github.comments[109]) == 1

    def test_removed_finalized_marker_reenters_planning_and_clears_evidence(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A later material body edit starts a fresh planning epoch."""
        github = FakeStageGitHub(
            labels=[STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL],
            issue_body="## Revised requirements\n\nThe behavior materially changed.",
        )
        item = make_work_item(issue=103)

        outcome = PlanningStage().on_enter(item, make_ctx(github=github))

        assert outcome is None
        assert github.labels[103] == {STATE_NEEDS_PLAN}
        assert item.payload["athena_finalized_plan_invalidated"] is True

    def test_observed_finalized_plan_fast_forwards_without_mutation(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        github = FakeStageGitHub(
            labels=[STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL],
            issue_body=_finalized_body(),
        )

        outcome = PlanningStage().on_enter(
            make_work_item(issue=104),
            make_ctx(github=github),
        )

        assert outcome is not None
        assert outcome.disposition is Disposition.ADVANCE
        assert github.mutation_log == []

    def test_foreign_replacement_cannot_reuse_observed_finalized_metadata(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        github = FakeStageGitHub(
            labels=[STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL],
            issue_body=_finalized_body("## Different valid finalized plan"),
            issue_body_owned_by_viewer=False,
            open_pr=88,
        )
        item = make_work_item(issue=110)

        outcome = PlanningStage().on_enter(item, make_ctx(github=github))

        assert outcome is None
        assert item.payload["requirements_recovery_required"] is True
        assert item.payload["athena_finalized_plan_invalidated"] is True
        assert "athena_finalized_plan_digest" not in item.payload

    def test_plan_go_without_finalized_evidence_is_normalized(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        github = FakeStageGitHub(labels=[STATE_PLAN_GO], issue_body=_finalized_body())

        outcome = PlanningStage().on_enter(
            make_work_item(issue=105),
            make_ctx(github=github),
        )

        assert outcome is not None
        assert outcome.disposition is Disposition.ADVANCE
        assert github.labels[105] == {STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL}
        assert github.mutation_log == [
            (
                "edit_labels",
                (
                    105,
                    (STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL),
                    (
                        STATE_PLAN_NO_GO,
                        STATE_NEEDS_PLAN,
                        STATE_IMPLEMENTATION_NO_GO,
                        STATE_IMPLEMENTATION_GO,
                        STATE_PLAN_BLOCKED,
                    ),
                ),
            )
        ]

    @pytest.mark.parametrize("force", [False, True])
    def test_drifted_finalized_marker_enters_recovery_without_normalizing_go(
        self, make_ctx: Any, make_work_item: Any, force: bool
    ) -> None:
        body = f"{_finalized_body()}\n\nLater material edit."
        github = FakeStageGitHub(
            labels=[STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL],
            issue_body=body,
            open_pr=88,
        )
        item = make_work_item(issue=106)

        outcome = PlanningStage().on_enter(
            item,
            make_ctx(github=github, config=SimpleNamespace(force=force)),
        )

        assert outcome is None
        assert item.payload["requirements_recovery_required"] is True
        assert item.payload["requirements_recovery_contaminated"] is True
        assert github.labels[106] == {STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL}
        assert github.mutation_log == []

    def test_foreign_finalized_body_cannot_grant_plan_go(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        github = FakeStageGitHub(
            labels=[STATE_PLAN_NO_GO],
            issue_body=_finalized_body(),
            issue_body_owned_by_viewer=False,
        )
        item = make_work_item(issue=107)

        outcome = PlanningStage().on_enter(item, make_ctx(github=github))

        assert outcome is None
        assert item.payload["requirements_recovery_required"] is True
        assert item.payload["requirements_recovery_contaminated"] is True
        assert "athena_finalized_plan_digest" not in item.payload
        assert github.labels[107] == {STATE_PLAN_NO_GO}
        assert github.mutation_log == []

    def test_skip_label_skips(self, make_ctx: Any, make_work_item: Any) -> None:
        """state:skip routes the item away without any writes."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_SKIP])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=2)

        outcome = stage.on_enter(item, ctx)

        assert outcome is not None
        assert outcome.disposition == Disposition.SKIP
        assert github.mutation_log == []

    def test_operator_skip_does_not_require_comment_journal(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An absolute operator skip survives an unavailable recovery journal."""
        stage = PlanningStage()

        class CountingGitHub(FakeStageGitHub):
            comment_reads = 0

            def issue_comments(self, issue_number: int) -> list[IssueComment]:
                self.comment_reads += 1
                return super().issue_comments(issue_number)

        github = CountingGitHub(labels=[STATE_SKIP])
        item = make_work_item(issue=2)

        outcome = stage.on_enter(item, make_ctx(github=github))

        assert outcome is not None
        assert outcome.disposition is Disposition.SKIP
        assert github.comment_reads == 0
        assert github.mutation_log == []

    def test_skip_wins_over_plan_go_with_warning(
        self, make_ctx: Any, make_work_item: Any, caplog: Any
    ) -> None:
        """state:skip + state:plan-go -> SKIP (not ADVANCE), with a loud WARN (#1835)."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_SKIP, STATE_PLAN_GO])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=5)

        with caplog.at_level("WARNING"):
            outcome = stage.on_enter(item, ctx)

        assert outcome is not None
        assert outcome.disposition == Disposition.SKIP
        assert github.mutation_log == []
        assert any("state:skip AND state:plan-go" in record.message for record in caplog.records)

    def test_historical_merged_pr_does_not_close_open_issue(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An open issue remains actionable despite a historic merged closing PR."""
        stage = PlanningStage()
        github = FakeStageGitHub(merged_pr=123)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=3)

        outcome = stage.on_enter(item, ctx)

        assert outcome is None
        assert github.labels[3] == {STATE_NEEDS_PLAN}

    def test_issue_closed_after_seeding_with_merged_pr_finishes_at_entry(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Planning revalidates a close/merge race before creating duplicate work."""
        stage = PlanningStage()
        github = FakeStageGitHub(issue_state="CLOSED", merged_pr=123)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=3)

        outcome = stage.on_enter(item, ctx)

        assert outcome is not None
        assert outcome.disposition == Disposition.FINISH_PASS
        assert github.mutation_log == []

    def test_malformed_issue_state_fails_closed_at_planning_entry(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A malformed refresh cannot authorize planning writes."""
        stage = PlanningStage()
        github = FakeStageGitHub(issue_state="UNKNOWN")
        ctx = make_ctx(github=github)
        item = make_work_item(issue=3)

        outcome = stage.on_enter(item, ctx)

        assert outcome is not None
        assert outcome.disposition == Disposition.FINISH_FAIL
        assert github.mutation_log == []

    def test_open_pr_without_plan_go_still_plans(self, make_ctx: Any, make_work_item: Any) -> None:
        """An open PR cannot bypass the issue's missing plan approval."""
        stage = PlanningStage()
        github = FakeStageGitHub(open_pr=456)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=4)

        outcome = stage.on_enter(item, ctx)

        assert outcome is None
        assert item.state == "ENTER"
        assert github.labels[4] == {STATE_NEEDS_PLAN}

    def test_force_starts_new_epoch_without_reusing_approved_plan(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Forced planning replaces plan-GO and ignores the stale journal epoch."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_GO], has_plan=True, open_pr=456)
        github.comments[40] = [
            render_current_plan("Stale approved plan", revision=3),
            render_current_review("GO\n\nstate:plan-go", revision=3),
        ]
        ctx = make_ctx(github=github, config=SimpleNamespace(force=True))
        item = make_work_item(
            issue=40,
            state="ENTER",
            payload={"plan_text": "cached stale plan", "issue_history": "cached stale review"},
        )

        outcome = stage.on_enter(item, ctx)

        assert outcome is None
        assert item.state == "ENTER"
        assert github.labels[40] == {STATE_PLAN_NO_GO}
        assert item.payload["requires_plan_revision"] is True
        assert "plan_text" not in item.payload
        assert "issue_history" not in item.payload

    @pytest.mark.parametrize(
        "legacy_label",
        [STATE_IMPLEMENTATION_GO, STATE_IMPLEMENTATION_NO_GO],
    )
    def test_planning_entry_removes_legacy_issue_implementation_state(
        self,
        make_ctx: Any,
        make_work_item: Any,
        legacy_label: str,
    ) -> None:
        """Planning normalizes legacy issue-scoped implementation state for restart."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN, legacy_label])
        item = make_work_item(issue=42, state="ENTER")

        outcome = stage.on_enter(item, make_ctx(github=github))

        assert outcome is None
        assert github.labels[42] == {STATE_NEEDS_PLAN}

    def test_force_restart_resumes_published_revision_without_new_planner_epoch(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A durable pending revision is force's restart authority."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN], has_plan=True)
        github.comments[41] = [
            render_current_plan("Fresh forced plan", revision=4, forced_planning_epoch=True),
            render_current_review(
                "Review pending for implementation plan revision 4.",
                revision=4,
            ),
        ]
        ctx = make_ctx(github=github, config=SimpleNamespace(force=True))
        item = make_work_item(issue=41, state="ENTER")

        outcome = stage.on_enter(item, ctx)

        assert outcome is None
        assert item.state == "VERIFY"
        assert item.payload["plan_text"] == "Fresh forced plan"
        assert "requires_plan_revision" not in item.payload
        assert github.mutation_log == []

    def test_force_restart_after_nogo_preserves_amendment_epoch(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A rejected forced plan resumes amendment instead of restarting force."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO], has_plan=True)
        github.comments[45] = [
            render_current_plan("Forced plan", revision=4, forced_planning_epoch=True),
            render_current_review("Add rollback.\n\nstate:plan-no-go", revision=4),
        ]
        item = make_work_item(issue=45, state="ENTER")

        outcome = stage.on_enter(
            item,
            make_ctx(github=github, config=SimpleNamespace(force=True)),
        )

        assert outcome is None
        assert item.state == "ENTER"
        assert item.payload["plan_text"] == "Forced plan"
        assert item.payload["forced_planning_epoch_started"] is True
        assert item.payload["requires_plan_revision"] is True
        assert "Add rollback." in item.payload["issue_history"]
        assert github.mutation_log == []

    def test_force_does_not_reuse_an_ordinary_pending_revision(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Only a forced-epoch journal marker can satisfy a forced restart."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN], has_plan=True)
        github.comments[43] = [
            render_current_plan("Ordinary pending plan", revision=2),
            render_current_review("Review pending for implementation plan revision 2.", revision=2),
        ]
        item = make_work_item(issue=43, state="ENTER")

        outcome = stage.on_enter(item, make_ctx(github=github, config=SimpleNamespace(force=True)))

        assert outcome is None
        assert item.state == "ENTER"
        assert item.payload["requires_plan_revision"] is True
        assert item.payload["forced_planning_epoch_started"] is True
        assert "plan_text" not in item.payload

    def test_force_dry_run_does_not_retry_label_readback_forever(
        self, make_ctx: Any, make_work_item: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dry-run previews force entry without requiring impossible mutation readback."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_GO], has_plan=True)
        monkeypatch.setattr(github, "edit_labels", lambda *_args, **_kwargs: None)
        item = make_work_item(issue=44, state="ENTER")

        outcome = stage.on_enter(
            item,
            make_ctx(
                github=github,
                dry_run=True,
                config=SimpleNamespace(force=True, enable_advise=False),
            ),
        )

        assert outcome is None
        assert item.payload["forced_planning_epoch_started"] is True

    def test_incomplete_requirements_snapshot_is_bounded_and_sets_no_go(
        self, make_ctx: Any, make_work_item: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_GO])
        monkeypatch.setattr(
            github,
            "gh_issue_json",
            lambda issue: {
                "number": issue,
                "state": "OPEN",
                "labels": [{"name": name} for name in sorted(github._issue_labels(issue))],
            },
        )
        ctx = make_ctx(github=github, budget_fn=lambda _name: 2)
        item = make_work_item(issue=45, state="ENTER")

        first = stage.on_enter(item, ctx)
        second = stage.on_enter(item, ctx)

        assert isinstance(first, StageOutcome)
        assert first.disposition == Disposition.RETRY
        assert item.payload["_enter_pending"] is True
        assert isinstance(second, StageOutcome)
        assert second.disposition == Disposition.FINISH_FAIL
        assert github.labels[45] == {STATE_PLAN_NO_GO}

    def test_unlabeled_entry_adds_needs_plan(self, make_ctx: Any, make_work_item: Any) -> None:
        """Unlabeled entry durably writes state:needs-plan before proceeding."""
        stage = PlanningStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=5)

        outcome = stage.on_enter(item, ctx)

        assert outcome is None  # proceed to step()
        assert github.labels[5] == {STATE_NEEDS_PLAN}
        assert STATE_NEEDS_PLAN in github.labels[5]

    def test_reentry_with_needs_plan_is_idempotent(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Re-entry with state:needs-plan already present writes nothing."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=6)

        outcome = stage.on_enter(item, ctx)

        assert outcome is None
        assert github.mutation_log == []

    def test_label_refresh_updates_cache(self, make_ctx: Any, make_work_item: Any) -> None:
        """on_enter refreshes item.labels_cache from GitHub."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=7, labels=["stale:label"])

        stage.on_enter(item, ctx)

        assert STATE_NEEDS_PLAN in item.labels_cache
        assert "stale:label" not in item.labels_cache

    def test_label_refresh_failure_is_bounded_and_cannot_advance_from_cache(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Cached label text cannot authorize a stage transition."""

        class BrokenGitHub(FakeStageGitHub):
            def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
                raise RuntimeError("gh unavailable")

        stage = PlanningStage()
        github = BrokenGitHub()
        ctx = make_ctx(github=github, budget_fn=lambda _name: 2)
        item = make_work_item(issue=8, labels=[STATE_PLAN_GO])

        first = stage.on_enter(item, ctx)
        second = stage.on_enter(item, ctx)

        assert isinstance(first, StageOutcome)
        assert first.disposition is Disposition.RETRY
        assert isinstance(second, StageOutcome)
        assert second.disposition is Disposition.FINISH_FAIL
        assert "fail-closed label unavailable" in second.note

    def test_explicit_tracker_label_enters_semantic_review(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN, "roadmap"])
        item = make_work_item(issue=111)

        outcome = PlanningStage().on_enter(item, make_ctx(github=github))

        assert outcome is None
        assert item.payload["requirements_recovery_required"] is True
        assert item.payload["requirements_recovery_contaminated"] is False

    def test_no_issue_number_fails(self, make_ctx: Any, make_work_item: Any) -> None:
        """A work item without an issue number finishes failed."""
        stage = PlanningStage()
        ctx = make_ctx()
        item = make_work_item(issue=None)

        outcome = stage.on_enter(item, ctx)

        assert outcome is not None
        assert outcome.disposition == Disposition.FINISH_FAIL

    def test_existing_plan_fast_forwards_to_verify(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A restart with a posted plan comment jumps straight to VERIFY.

        Real has-plan semantics: advise + plan are never redone mid-stage.
        """
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN], has_plan=True)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=9, state="ENTER")

        outcome = stage.on_enter(item, ctx)

        assert outcome is None  # proceed, but...
        assert item.state == "VERIFY"  # ...straight to verification
        assert github.mutation_log == []  # no rewrites on re-entry

    def test_blocked_label_stops_planning_even_after_human_feedback(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Comments cannot clear an operator-owned BLOCKED hold."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=["state:plan-blocked"], has_plan=True)
        github.comments[14] = [
            f"{PLAN_COMMENT_MARKER}\n\nPlan awaiting a decision.",
            IssueComment(
                body="## 🔍 Plan Review\n\nNeed the API choice.",
                viewer_did_author=True,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:10:00Z",
            ),
            IssueComment(
                body="Use the existing REST endpoint; do not add GraphQL.",
                author_login="maintainer",
                author_association="MEMBER",
                created_at="2026-01-01T00:11:00Z",
            ),
        ]
        ctx = make_ctx(github=github)
        item = make_work_item(issue=14, state="ENTER")

        outcome = stage.on_enter(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.BLOCKED
        assert "issue_history" not in item.payload
        assert github.labels[14] == {STATE_PLAN_BLOCKED}
        assert github.mutation_log == [
            ("gh_issue_upsert_comment", (14, PLAN_REVIEW_CANONICAL_MARKER))
        ]

    def test_blocked_restart_never_completes_an_interrupted_label_swap(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Even a pending newer revision cannot make automation clear BLOCKED."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_BLOCKED], has_plan=True)
        github.comments[141] = [
            render_current_plan("Plan v2 using REST.", revision=2),
            IssueComment(
                body=render_current_review(
                    "Review pending for implementation plan revision 2.",
                    revision=2,
                ),
                viewer_did_author=True,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:12:00Z",
            ),
            IssueComment(
                body="Use REST.",
                author_login="maintainer",
                author_association="MEMBER",
                created_at="2026-01-01T00:11:00Z",
            ),
        ]
        ctx = make_ctx(github=github)
        item = make_work_item(issue=141, state="ENTER")

        outcome = stage.on_enter(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.BLOCKED
        assert item.state == "ENTER"
        assert github.labels[141] == {STATE_PLAN_BLOCKED}
        assert github.mutation_log == [
            ("gh_issue_upsert_comment", (141, PLAN_REVIEW_CANONICAL_MARKER))
        ]

    def test_double_on_enter_is_idempotent(self, make_ctx: Any, make_work_item: Any) -> None:
        """A literal double on_enter produces no extra mutations or moves."""
        stage = PlanningStage()
        github = FakeStageGitHub(has_plan=True)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=10, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        assert item.state == "VERIFY"
        log_after_first = list(github.mutation_log)
        assert len(log_after_first) == 1
        assert github.labels[10] == {STATE_NEEDS_PLAN}

        assert stage.on_enter(item, ctx) is None  # second literal call

        assert item.state == "VERIFY"
        assert github.mutation_log == log_after_first  # nothing new written

    def test_replan_entry_keeps_no_go_until_revised_plan_is_published(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A rejected plan remains authoritative while the replacement is generated."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=20, state="ENTER")

        outcome = stage.on_enter(item, ctx)

        assert outcome is None  # proceed to re-plan, not fast-forward
        assert item.state == "ENTER"  # no premature VERIFY fast-forward
        assert item.payload["requires_plan_revision"] is True
        assert github.mutation_log == []
        assert STATE_PLAN_NO_GO in github.labels[20]
        assert STATE_PLAN_GO not in github.labels[20]
        assert STATE_NEEDS_PLAN not in github.labels[20]

    def test_normal_no_go_replan_receives_current_rejected_revision_context(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A replan gets its current rejected plan/review pair for recovery."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO], has_plan=True)
        github.comments[29] = [
            render_current_plan("Plan v1", revision=1),
            render_current_review("Missing rollback.\n\nstate:plan-no-go", revision=1),
        ]
        ctx = make_ctx(github=github)
        item = make_work_item(issue=29, state="ENTER")

        assert stage.on_enter(item, ctx) is None

        history = item.payload["issue_history"]
        assert history.index("Plan v1") < history.index("Missing rollback")
        item.state = "PLAN_WAIT"
        request = stage.step(item, ctx)
        assert isinstance(request, JobRequest)
        assert isinstance(request.job, AgentJob)
        assert request.job.prompt_kwargs["issue_history"] == history

    def test_normal_no_go_replan_excludes_superseded_revision_context(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Old NOGO critiques cannot compete with the current rejection."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO], has_plan=True)
        github.comments[29] = [
            "<!-- hephaestus-plan-history:revision=1:kind=plan -->\nPlan v1",
            "<!-- hephaestus-plan-history:revision=1:kind=review -->\nReview v1",
            render_current_plan("Plan v2", revision=2),
            render_current_review("Review v2\n\nstate:plan-no-go", revision=2),
        ]
        ctx = make_ctx(github=github)
        item = make_work_item(issue=29, state="ENTER")

        assert stage.on_enter(item, ctx) is None

        history = item.payload["issue_history"]
        assert "Plan v2" in history
        assert "Review v2" in history
        assert "Plan v1" not in history
        assert "Review v1" not in history

    def test_replan_entry_ignores_existing_rejected_plan_comment(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A NOGO fail-back must not VERIFY against the stale rejected plan."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO], has_plan=True)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=23, state="ENTER")

        outcome = stage.on_enter(item, ctx)

        assert outcome is None
        assert item.state == "ENTER"
        assert item.payload["requires_plan_revision"] is True
        assert github.mutation_log == []

    def test_plan_go_on_entry_fast_forwards_without_swap(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The is_plan_go guard fires first and returns ADVANCE; swap block never reached."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_GO])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=21, state="ENTER")

        outcome = stage.on_enter(item, ctx)

        # The STATE_PLAN_GO guard at line 176 short-circuits and returns ADVANCE
        # before the swap logic at line 206, so no label mutations occur.
        assert outcome is not None
        assert outcome.disposition == Disposition.ADVANCE

    def test_replan_entry_idempotent_when_labels_already_swapped(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Re-entry after a successful swap writes nothing (idempotency)."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=22, state="ENTER")

        outcome = stage.on_enter(item, ctx)

        assert outcome is None
        # No swap triggered (neither STATE_PLAN_NO_GO nor STATE_PLAN_GO present).
        # No add triggered (STATE_NEEDS_PLAN already present).
        assert github.mutation_log == []

    def test_needs_plan_label_does_not_infer_replan_from_review_comment(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A comment token cannot override the authoritative needs-plan label."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN], has_plan=True)
        github.comments[25] = [
            render_current_plan("Plan v1", revision=1),
            render_current_review("Missing rollback.\n\nstate:plan-no-go", revision=1),
        ]
        ctx = make_ctx(github=github)
        item = make_work_item(issue=25, state="ENTER")

        assert stage.on_enter(item, ctx) is None

        assert "requires_plan_revision" not in item.payload
        assert item.state == "VERIFY"
        assert github.mutation_log == []

    def test_blocked_without_new_feedback_exits_before_planning(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A blocked plan cannot spend another agent call until a maintainer responds."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_BLOCKED])
        github.comments[26] = [
            render_current_plan("Plan v1", revision=1),
            render_current_review("Need an API decision.\n\nstate:plan-blocked", revision=1),
        ]
        ctx = make_ctx(github=github)
        item = make_work_item(issue=26, state="ENTER")

        outcome = stage.on_enter(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.BLOCKED
        assert github.mutation_log == []


class TestPlanningStageStep:
    """step state machine: ENTER -> ADVISE_WAIT -> PLAN_WAIT -> VERIFY."""

    def test_enter_routes_to_advise_when_enabled(self, make_ctx: Any, make_work_item: Any) -> None:
        """ENTER advances to ADVISE_WAIT when advise is enabled."""
        stage = PlanningStage()
        ctx = make_ctx()
        ctx.config.enable_advise = True
        item = make_work_item(issue=1, state="ENTER")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "ADVISE_WAIT"

    def test_enter_routes_to_requirements_recovery_first(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = PlanningStage()
        item = make_work_item(issue=1, state="ENTER")
        item.payload["requirements_recovery_required"] = True

        result = stage.step(item, make_ctx())

        assert isinstance(result, Continue)
        assert result.next_state == "REQUIREMENTS_RECOVERY_WAIT"

    def test_requirements_recovery_submits_typed_planner_job(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = PlanningStage()
        item = make_work_item(issue=1, state="REQUIREMENTS_RECOVERY_WAIT")
        item.payload.update(
            {
                "issue_title": "Retry bug",
                "issue_body": f"{PLAN_CANONICAL_MARKER}\nDerived",
                "issue_body_digest": "a" * 64,
                "requirements_recovery_required": True,
            }
        )

        result = stage.step(item, make_ctx())

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.descr == "recover_requirements"
        assert result.job.parse is not None
        assert result.on_done_state == "REQUIREMENTS_RECOVERY_REVIEW_WAIT"

    def test_requirements_recovery_review_is_independent_reviewer_job(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = PlanningStage()
        item = make_work_item(issue=1, state="REQUIREMENTS_RECOVERY_REVIEW_WAIT")
        item.payload.update(
            {
                "issue_title": "Retry bug",
                "issue_body_digest": "a" * 64,
                "requirements_evidence_digest": "b" * 64,
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.REQUIREMENTS,
                    "Keep retries bounded.",
                    "Recovered from tests.",
                    "b" * 64,
                ),
            }
        )

        result = stage.step(item, make_ctx())

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.descr == "review_recovered_requirements"
        assert result.job.parse is not None
        assert result.on_done_state == "REQUIREMENTS_RECOVERY_APPLY"

    def test_requirements_recovery_go_replaces_body_and_starts_fresh_plan(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = PlanningStage()
        old_body = f"{PLAN_CANONICAL_MARKER}\nDerived"
        github = FakeStageGitHub(labels=[STATE_PLAN_GO], issue_body=old_body, has_plan=True)
        item = make_work_item(issue=1, state="REQUIREMENTS_RECOVERY_APPLY")
        item.payload.update(
            {
                "issue_title": "A task",
                "issue_body": old_body,
                "issue_source_body": old_body,
                "issue_body_digest": github.gh_issue_json(1)["bodyDigest"],
                "requirements_evidence_digest": _recovery_binding(old_body, issue=1),
                "requirements_repository_revision": _RECOVERY_REVISION,
                "requirements_recovery_contaminated": True,
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.REQUIREMENTS,
                    "## Required behavior\n\n- Keep retries bounded.",
                    "Recovered from tests.",
                    "test_retry.py",
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.GO,
                    RecoveryDisposition.REQUIREMENTS,
                    "Faithful to repository evidence.",
                ),
            }
        )
        config = SimpleNamespace(enable_advise=True, reset_plan_review_sessions=set())
        ctx = make_ctx(github=github, config=config)

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "ADVISE_WAIT"
        assert github.labels[1] == {STATE_PLAN_NO_GO}
        assert item.payload["requires_plan_revision"] is True
        assert github.gh_issue_json(1)["body"] == old_body
        assert any(comment.startswith(RECOVERY_PROVENANCE_PREFIX) for comment in github.comments[1])
        assert "requirements_recovery_required" not in item.payload
        assert config.reset_plan_review_sessions == {1}

    def test_post_model_snapshot_failure_exhausts_fail_closed(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        class BrokenGitHub(FakeStageGitHub):
            def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
                raise RuntimeError("snapshot unavailable")

        item = make_work_item(issue=112, state="REQUIREMENTS_RECOVERY_APPLY")
        item.payload.update(
            {
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.REQUIREMENTS,
                    "Keep retries bounded.",
                    "Repository evidence.",
                    "evidence",
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.GO,
                    RecoveryDisposition.REQUIREMENTS,
                    "Confirmed.",
                ),
            }
        )

        result = PlanningStage().step(
            item,
            make_ctx(github=BrokenGitHub(), budget_fn=lambda _name: 1),
        )

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.FINISH_FAIL
        assert item.attempts["plan"] == 1
        assert "fail-closed label unavailable" in result.note

    def test_recovery_retry_snapshot_failure_exhausts_fail_closed(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        class FailsAfterApplyRead(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__(labels=[STATE_NEEDS_PLAN])
                self.reads = 0

            def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
                self.reads += 1
                if self.reads > 1:
                    raise RuntimeError("retry snapshot unavailable")
                return super().gh_issue_json(issue_number)

        github = FailsAfterApplyRead()
        item = make_work_item(issue=113, state="REQUIREMENTS_RECOVERY_APPLY")
        item.payload.update(
            {
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.REQUIREMENTS,
                    "Guess.",
                    "Weak.",
                    "evidence",
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.NOGO,
                    RecoveryDisposition.REQUIREMENTS,
                    "Insufficient.",
                ),
            }
        )

        result = PlanningStage().step(
            item,
            make_ctx(github=github, budget_fn=lambda _name: 1),
        )

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.FINISH_FAIL
        assert item.attempts["plan"] == 1

    @pytest.mark.parametrize("first_read_fails", [False, True])
    def test_exhausted_recovery_never_retries_unconfirmed_no_go_forever(
        self,
        make_ctx: Any,
        make_work_item: Any,
        first_read_fails: bool,
    ) -> None:
        """The final label-confirmation attempt is terminal even on conflict."""

        class DropsNoGoGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__(labels=[STATE_NEEDS_PLAN])
                self.reads = 0

            def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
                self.reads += 1
                if first_read_fails and self.reads == 1:
                    raise RuntimeError("initial snapshot unavailable")
                return super().gh_issue_json(issue_number)

            def edit_labels(
                self,
                issue_number: int,
                *,
                add: list[str],
                remove: list[str],
            ) -> None:
                return None

        github = DropsNoGoGitHub()
        item = make_work_item(issue=114, state="REQUIREMENTS_RECOVERY_APPLY")
        item.payload.update(
            {
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.REQUIREMENTS,
                    "Guess.",
                    "Weak.",
                    "evidence",
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.NOGO,
                    RecoveryDisposition.REQUIREMENTS,
                    "Insufficient.",
                ),
            }
        )

        result = PlanningStage().step(
            item,
            make_ctx(github=github, budget_fn=lambda _name: 1),
        )

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.FINISH_FAIL
        assert item.attempts["plan"] == 1
        assert STATE_PLAN_NO_GO not in github.labels[114]

    def test_recovery_results_are_rejected_when_issue_evidence_changed(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN], issue_body="New human body")
        item = make_work_item(issue=46, state="REQUIREMENTS_RECOVERY_APPLY")
        item.payload.update(
            {
                "issue_title": "A task",
                "issue_body": "Old body",
                "issue_body_digest": "a" * 64,
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.TRACKER, "", "Old evidence.", "Old body."
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.GO, RecoveryDisposition.TRACKER, "Confirmed old body."
                ),
            }
        )

        result = stage.step(item, make_ctx(github=github))

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.RETRY
        assert item.state == "ENTER"
        assert item.payload["_enter_pending"] is True
        assert STATE_SKIP not in github.labels[46]

    @pytest.mark.parametrize("failure", ["label", "provenance"])
    def test_confirmed_recovery_write_failure_exhausts_without_poisoning_coordinator(
        self,
        make_ctx: Any,
        make_work_item: Any,
        failure: str,
    ) -> None:
        """Recovery-owned GitHub writes are converted to bounded outcomes."""

        class FailingRecoveryWriteGitHub(FakeStageGitHub):
            def edit_labels(
                self,
                issue_number: int,
                *,
                add: list[str],
                remove: list[str],
            ) -> None:
                if failure == "label":
                    raise RuntimeError("label mutation unavailable")
                super().edit_labels(issue_number, add=add, remove=remove)

            def upsert_issue_comment(
                self,
                issue_number: int,
                marker: str,
                body: str,
                *,
                legacy_marker: str | None = None,
            ) -> None:
                if failure == "provenance" and marker == RECOVERY_PROVENANCE_PREFIX:
                    raise RuntimeError("provenance publication unavailable")
                super().upsert_issue_comment(
                    issue_number,
                    marker,
                    body,
                    legacy_marker=legacy_marker,
                )

        item = make_work_item(issue=116, state="REQUIREMENTS_RECOVERY_APPLY")
        item.payload.update(
            {
                "requirements_recovery_contaminated": True,
                "issue_source_body": "<!-- hephaestus-plan:canonical -->\nOld plan",
                "issue_body_digest": "a" * 64,
                "requirements_evidence_digest": "c" * 64,
                "requirements_repository_revision": "b" * 40,
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.REQUIREMENTS,
                    "Use the original user requirements.",
                    "Repository evidence.",
                    "c" * 64,
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.GO,
                    RecoveryDisposition.REQUIREMENTS,
                    "Confirmed.",
                ),
            }
        )

        result = PlanningStage().step(
            item,
            make_ctx(
                github=FailingRecoveryWriteGitHub(labels=[STATE_NEEDS_PLAN]),
                budget_fn=lambda _name: 1,
            ),
        )

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.FINISH_FAIL
        assert item.attempts["plan"] == 1

    def test_tracker_skip_requires_matching_independent_go(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
        item = make_work_item(issue=2, state="REQUIREMENTS_RECOVERY_APPLY")
        item.payload.update(
            {
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.TRACKER, "", "Tracks children.", "Checklist"
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.GO, RecoveryDisposition.TRACKER, "Confirmed tracker."
                ),
            }
        )

        result = stage.step(item, make_ctx(github=github))

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_PASS
        assert item.payload[WAVE_NON_CODE_PAYLOAD] is True
        assert github.labels[2] == {STATE_SKIP, "epic"}

    def test_sanitized_recovery_snapshot_cannot_apply_semantic_skip(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """In-flight recovery results cannot authorize a sanitized snapshot."""

        class SanitizedAuthorityGitHub(FakeStageGitHub):
            def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
                snapshot = super().gh_issue_json(issue_number)
                snapshot["authoritySanitized"] = True
                return snapshot

        github = SanitizedAuthorityGitHub(labels=[STATE_NEEDS_PLAN])
        item = make_work_item(issue=3, state="REQUIREMENTS_RECOVERY_APPLY")
        snapshot = github.gh_issue_json(3)
        item.payload.update(
            {
                "issue_title": snapshot["title"],
                "issue_source_body": snapshot["body"],
                "issue_body_digest": snapshot["bodyDigest"],
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.TRACKER, "", "Tracks children.", "Checklist"
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.GO,
                    RecoveryDisposition.TRACKER,
                    "Confirmed tracker.",
                ),
            }
        )

        result = PlanningStage().step(
            item,
            make_ctx(github=github, budget_fn=lambda _name: 2),
        )

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.RETRY
        assert STATE_SKIP not in github.labels[3]

    @pytest.mark.parametrize(
        "labels",
        [[STATE_NEEDS_PLAN], [STATE_SKIP, "epic"]],
        ids=["before-label-write", "after-label-write"],
    )
    def test_pending_wave_non_code_intent_resumes_without_models(
        self,
        tmp_path: Path,
        make_ctx: Any,
        make_work_item: Any,
        labels: list[str],
    ) -> None:
        """Either side of the intent/label crash boundary converges to success."""
        store = IssueWaveStore(tmp_path, "test-org", "test-repo")
        lease = store.seal_selection(store.plan_admission("a" * 40, 1), [118])
        reason = "independently confirmed tracker"
        store.record_non_code_intent(
            lease,
            issue_number=118,
            reason=reason,
            evidence_digest=_recovery_binding("", issue=118),
            repository_revision=_RECOVERY_REVISION,
            extra_labels=("epic",),
        )
        item = make_work_item(issue=118, pr=88)
        item.payload.update(
            {
                WAVE_LEASE_PAYLOAD: lease,
                WAVE_NON_CODE_INTENT_PAYLOAD: {
                    "reason": reason,
                    "extra_labels": ["epic"],
                    "evidence_digest": _recovery_binding("", issue=118),
                    "repository_revision": _RECOVERY_REVISION,
                    "explanation": "",
                },
            }
        )
        github = FakeStageGitHub(labels=labels, open_pr=88)

        outcome = PlanningStage().on_enter(
            item,
            make_ctx(
                github=github,
                paths=SimpleNamespace(repo_root=tmp_path, worktree=tmp_path / "worktree"),
            ),
        )

        checkpoint = store.load()
        assert outcome is not None and outcome.disposition is Disposition.FINISH_PASS
        assert github.labels[118] == {STATE_SKIP, "epic"}
        assert item.payload[WAVE_NON_CODE_PAYLOAD] is True
        assert WAVE_NON_CODE_INTENT_PAYLOAD not in item.payload
        assert checkpoint is not None
        recorded = checkpoint.current_wave.outcomes[0]
        assert recorded.passed and recorded.non_code and recorded.pr_number is None

    def test_sanitized_pending_non_code_intent_retires_without_skip(
        self,
        tmp_path: Path,
        make_ctx: Any,
        make_work_item: Any,
    ) -> None:
        """Sanitized text cannot reuse independently reviewed skip authority."""

        class SanitizedAuthorityGitHub(FakeStageGitHub):
            def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
                snapshot = super().gh_issue_json(issue_number)
                snapshot["authoritySanitized"] = True
                return snapshot

        store = IssueWaveStore(tmp_path, "test-org", "test-repo")
        lease = store.seal_selection(store.plan_admission("a" * 40, 1), [119])
        reason = "independently confirmed tracker"
        store.record_non_code_intent(
            lease,
            issue_number=119,
            reason=reason,
            evidence_digest=_recovery_binding("", issue=119),
            repository_revision=_RECOVERY_REVISION,
            extra_labels=("epic",),
        )
        item = make_work_item(
            issue=119,
            payload={
                WAVE_LEASE_PAYLOAD: lease,
                WAVE_NON_CODE_INTENT_PAYLOAD: {
                    "reason": reason,
                    "extra_labels": ["epic"],
                    "evidence_digest": _recovery_binding("", issue=119),
                    "repository_revision": _RECOVERY_REVISION,
                    "explanation": "",
                },
            },
        )
        github = SanitizedAuthorityGitHub(labels=[STATE_NEEDS_PLAN])

        outcome = PlanningStage().on_enter(
            item,
            make_ctx(
                github=github,
                paths=SimpleNamespace(repo_root=tmp_path, worktree=tmp_path / "worktree"),
            ),
        )

        assert outcome is not None and outcome.disposition is Disposition.RETRY
        assert STATE_SKIP not in github.labels[119]
        assert store.non_code_intent_for(lease, 119) is None

    def test_stale_non_code_intent_yields_to_authenticated_finalized_plan(
        self,
        tmp_path: Path,
        make_ctx: Any,
        make_work_item: Any,
    ) -> None:
        """Edited requirements invalidate pending skip authority before finalization reuse."""
        store = IssueWaveStore(tmp_path, "test-org", "test-repo")
        lease = store.seal_selection(store.plan_admission("a" * 40, 1), [120])
        reason = "independently confirmed tracker"
        store.record_non_code_intent(
            lease,
            issue_number=120,
            reason=reason,
            evidence_digest=_recovery_binding("Old tracker body", issue=120),
            repository_revision=_RECOVERY_REVISION,
            extra_labels=("epic",),
        )
        body = _finalized_body()
        item = make_work_item(issue=120)
        item.payload.update(
            {
                WAVE_LEASE_PAYLOAD: lease,
                WAVE_NON_CODE_INTENT_PAYLOAD: {
                    "reason": reason,
                    "extra_labels": ["epic"],
                    "evidence_digest": _recovery_binding("Old tracker body", issue=120),
                    "repository_revision": _RECOVERY_REVISION,
                    "explanation": "",
                },
            }
        )
        github = FakeStageGitHub(labels=[STATE_SKIP, "epic"], issue_body=body)
        ctx = make_ctx(
            github=github,
            paths=SimpleNamespace(repo_root=tmp_path, worktree=tmp_path / "worktree"),
        )

        stale = PlanningStage().on_enter(item, ctx)
        finalized = PlanningStage().on_enter(item, ctx)

        assert stale is not None and stale.disposition is Disposition.RETRY
        assert finalized is not None and finalized.disposition is Disposition.ADVANCE
        assert github.labels[120] == {STATE_PLAN_GO, ATHENA_FINALIZED_PLAN_LABEL, "epic"}

    def test_applied_stale_non_code_intent_restarts_as_ordinary_code(
        self,
        tmp_path: Path,
        make_ctx: Any,
        make_work_item: Any,
    ) -> None:
        """Evidence drift retires durable skip authority before a real restart."""
        store = IssueWaveStore(tmp_path, "test-org", "test-repo")
        lease = store.seal_selection(store.plan_admission("a" * 40, 1), [123])
        reason = "independently confirmed tracker"
        store.record_non_code_intent(
            lease,
            issue_number=123,
            reason=reason,
            evidence_digest=_recovery_binding("Old tracker body", issue=123),
            repository_revision=_RECOVERY_REVISION,
            extra_labels=("epic",),
        )
        first_item = make_work_item(issue=123)
        first_item.payload.update(
            {
                WAVE_LEASE_PAYLOAD: lease,
                WAVE_NON_CODE_INTENT_PAYLOAD: {
                    "reason": reason,
                    "extra_labels": ["epic"],
                    "evidence_digest": _recovery_binding("Old tracker body", issue=123),
                    "repository_revision": _RECOVERY_REVISION,
                    "explanation": "",
                },
            }
        )
        github = FakeStageGitHub(
            labels=[STATE_SKIP, "epic"],
            issue_body="Implement the new worker behavior.",
        )
        ctx = make_ctx(
            github=github,
            paths=SimpleNamespace(repo_root=tmp_path, worktree=tmp_path / "worktree"),
        )

        drift = PlanningStage().on_enter(first_item, ctx)

        assert drift is not None and drift.disposition is Disposition.RETRY
        assert github.labels[123] == {"epic"}
        assert store.non_code_intent_for(lease, 123) is None

        restarted = make_work_item(issue=123, payload={WAVE_LEASE_PAYLOAD: lease})
        entry = PlanningStage().on_enter(restarted, ctx)

        assert entry is None
        assert restarted.payload["requirements_recovery_required"] is True
        restarted.state = "REQUIREMENTS_RECOVERY_APPLY"
        restarted.payload.update(
            {
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.REQUIREMENTS,
                    "",
                    "Ordinary implementation issue.",
                    "The edited body requests code changes.",
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.GO,
                    RecoveryDisposition.REQUIREMENTS,
                    "Confirmed ordinary code work.",
                ),
            }
        )

        resumed = PlanningStage().step(restarted, ctx)

        assert isinstance(resumed, Continue)
        assert resumed.next_state == "ADVISE_WAIT"
        assert STATE_SKIP not in github.labels[123]

    def test_retired_non_code_intent_resumes_skip_cleanup_after_crash(
        self,
        tmp_path: Path,
        make_ctx: Any,
        make_work_item: Any,
    ) -> None:
        """A crash after durable retirement cannot strand the old skip."""
        store = IssueWaveStore(tmp_path, "test-org", "test-repo")
        lease = store.seal_selection(store.plan_admission("a" * 40, 1), [124])
        reason = "independently confirmed tracker"
        store.record_non_code_intent(
            lease,
            issue_number=124,
            reason=reason,
            evidence_digest=_recovery_binding("Old tracker body", issue=124),
            repository_revision=_RECOVERY_REVISION,
            extra_labels=("epic",),
        )
        active = store.non_code_intent_for(lease, 124)
        assert active is not None
        store.retire_non_code_intent(lease, active)
        retired = store.non_code_intent_for(lease, 124)
        assert retired is not None and retired.retired
        item = make_work_item(issue=124)
        item.payload.update(
            {
                WAVE_LEASE_PAYLOAD: lease,
                WAVE_NON_CODE_INTENT_PAYLOAD: {
                    "reason": retired.reason,
                    "extra_labels": list(retired.extra_labels),
                    "evidence_digest": retired.evidence_digest,
                    "repository_revision": retired.repository_revision,
                    "explanation": retired.explanation,
                    "retired": True,
                },
            }
        )
        github = FakeStageGitHub(
            labels=[STATE_SKIP, "epic"],
            issue_body="Implement the new worker behavior.",
        )

        outcome = PlanningStage().on_enter(
            item,
            make_ctx(
                github=github,
                paths=SimpleNamespace(repo_root=tmp_path, worktree=tmp_path / "worktree"),
            ),
        )

        assert outcome is not None and outcome.disposition is Disposition.RETRY
        assert github.labels[124] == {"epic"}
        assert store.non_code_intent_for(lease, 124) is None

    def test_retired_intent_resumes_after_skip_removal_before_tombstone_cleanup(
        self,
        tmp_path: Path,
        make_ctx: Any,
        make_work_item: Any,
    ) -> None:
        """A second cleanup crash window resumes without repeating label mutation."""
        store = IssueWaveStore(tmp_path, "test-org", "test-repo")
        lease = store.seal_selection(store.plan_admission("a" * 40, 1), [126])
        store.record_non_code_intent(
            lease,
            issue_number=126,
            reason="independently confirmed tracker",
            evidence_digest=_recovery_binding("Old tracker body", issue=126),
            repository_revision=_RECOVERY_REVISION,
            extra_labels=("epic",),
        )
        active = store.non_code_intent_for(lease, 126)
        assert active is not None
        store.retire_non_code_intent(lease, active)
        retired = store.non_code_intent_for(lease, 126)
        assert retired is not None and retired.retired
        github = FakeStageGitHub(
            labels=["epic"],
            issue_body="Implement the new worker behavior.",
        )
        ctx = make_ctx(
            github=github,
            paths=SimpleNamespace(repo_root=tmp_path, worktree=tmp_path / "worktree"),
        )
        item = make_work_item(
            issue=126,
            payload={
                WAVE_LEASE_PAYLOAD: lease,
                WAVE_NON_CODE_INTENT_PAYLOAD: {
                    "reason": retired.reason,
                    "extra_labels": list(retired.extra_labels),
                    "evidence_digest": retired.evidence_digest,
                    "repository_revision": retired.repository_revision,
                    "explanation": retired.explanation,
                    "retired": True,
                },
            },
        )

        cleanup = PlanningStage().on_enter(item, ctx)

        assert cleanup is not None and cleanup.disposition is Disposition.RETRY
        assert github.labels[126] == {"epic"}
        assert github.mutation_log == []
        assert store.non_code_intent_for(lease, 126) is None

        restarted = make_work_item(issue=126, payload={WAVE_LEASE_PAYLOAD: lease})
        entry = PlanningStage().on_enter(restarted, ctx)

        assert entry is None
        assert restarted.payload["requirements_recovery_required"] is True
        assert github.mutation_log == []

    def test_retired_intent_preserves_nonmatching_operator_skip(
        self,
        tmp_path: Path,
        make_ctx: Any,
        make_work_item: Any,
    ) -> None:
        """Cleanup cannot claim a skip missing the intent's expected labels."""
        store = IssueWaveStore(tmp_path, "test-org", "test-repo")
        lease = store.seal_selection(store.plan_admission("a" * 40, 1), [125])
        store.record_non_code_intent(
            lease,
            issue_number=125,
            reason="independently confirmed tracker",
            evidence_digest=_recovery_binding("Old tracker body", issue=125),
            repository_revision=_RECOVERY_REVISION,
            extra_labels=("epic",),
        )
        active = store.non_code_intent_for(lease, 125)
        assert active is not None
        store.retire_non_code_intent(lease, active)
        retired = store.non_code_intent_for(lease, 125)
        assert retired is not None
        item = make_work_item(issue=125)
        item.payload.update(
            {
                WAVE_LEASE_PAYLOAD: lease,
                WAVE_NON_CODE_INTENT_PAYLOAD: {
                    "reason": retired.reason,
                    "extra_labels": list(retired.extra_labels),
                    "evidence_digest": retired.evidence_digest,
                    "repository_revision": retired.repository_revision,
                    "explanation": retired.explanation,
                    "retired": True,
                },
            }
        )
        github = FakeStageGitHub(
            labels=[STATE_SKIP],
            issue_body="Implement the new worker behavior.",
        )
        ctx = make_ctx(
            github=github,
            paths=SimpleNamespace(repo_root=tmp_path, worktree=tmp_path / "worktree"),
        )

        cleanup = PlanningStage().on_enter(item, ctx)
        operator_skip = PlanningStage().on_enter(item, ctx)

        assert cleanup is not None and cleanup.disposition is Disposition.RETRY
        assert operator_skip is not None and operator_skip.disposition is Disposition.SKIP
        assert github.labels[125] == {STATE_SKIP}
        assert github.mutation_log == []
        assert store.non_code_intent_for(lease, 125) is None

    def test_pending_obsolete_intent_restores_explanation_before_skip(
        self,
        tmp_path: Path,
        make_ctx: Any,
        make_work_item: Any,
    ) -> None:
        """Crash recovery replays the durable obsolete rationale before labels."""
        store = IssueWaveStore(tmp_path, "test-org", "test-repo")
        lease = store.seal_selection(store.plan_admission("a" * 40, 1), [121])
        explanation = "Merged replacement confirmed."
        binding = _recovery_binding("Already resolved.", issue=121)
        store.record_non_code_intent(
            lease,
            issue_number=121,
            reason="independently confirmed obsolete",
            evidence_digest=binding,
            repository_revision=_RECOVERY_REVISION,
            explanation=explanation,
        )
        item = make_work_item(issue=121)
        item.payload.update(
            {
                WAVE_LEASE_PAYLOAD: lease,
                WAVE_NON_CODE_INTENT_PAYLOAD: {
                    "reason": "independently confirmed obsolete",
                    "extra_labels": [],
                    "evidence_digest": binding,
                    "repository_revision": _RECOVERY_REVISION,
                    "explanation": explanation,
                },
            }
        )
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN], issue_body="Already resolved.")

        outcome = PlanningStage().on_enter(
            item,
            make_ctx(
                github=github,
                paths=SimpleNamespace(repo_root=tmp_path, worktree=tmp_path / "worktree"),
            ),
        )

        assert outcome is not None and outcome.disposition is Disposition.FINISH_PASS
        assert github.labels[121] == {STATE_SKIP}
        comments = [str(comment) for comment in github.comments[121]]
        assert len(comments) == 1
        assert explanation in comments[0]

    def test_tracker_skip_requires_epic_label_readback(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A partial tracker label write is not reported as success."""

        class DropsEpicGitHub(FakeStageGitHub):
            def edit_labels(
                self,
                issue_number: int,
                *,
                add: list[str],
                remove: list[str],
            ) -> None:
                super().edit_labels(
                    issue_number,
                    add=[label for label in add if label != "epic"],
                    remove=remove,
                )

        github = DropsEpicGitHub(labels=[STATE_NEEDS_PLAN])
        item = make_work_item(issue=73, state="REQUIREMENTS_RECOVERY_APPLY")
        item.payload.update(
            {
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.TRACKER, "", "Tracks children.", "Checklist"
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.GO, RecoveryDisposition.TRACKER, "Confirmed tracker."
                ),
            }
        )

        result = PlanningStage().step(item, make_ctx(github=github))

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.RETRY

    @pytest.mark.parametrize(
        ("github", "expected_disposition"),
        [
            (FakeStageGitHub(labels=[STATE_SKIP]), Disposition.SKIP),
            (FakeStageGitHub(issue_state="CLOSED", merged_pr=88), Disposition.FINISH_PASS),
        ],
    )
    def test_recovery_apply_honors_terminal_live_issue_state(
        self,
        make_ctx: Any,
        make_work_item: Any,
        github: FakeStageGitHub,
        expected_disposition: Disposition,
    ) -> None:
        """Recovery cannot mutate an issue that became skipped or closed."""
        item = make_work_item(issue=74, state="REQUIREMENTS_RECOVERY_APPLY")
        item.payload.update(
            {
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.REQUIREMENTS, "", "Recovered.", "Evidence"
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.GO, RecoveryDisposition.REQUIREMENTS, "Confirmed."
                ),
            }
        )

        result = PlanningStage().step(item, make_ctx(github=github))

        assert isinstance(result, StageOutcome)
        assert result.disposition == expected_disposition
        assert github.mutation_log == []

    @pytest.mark.parametrize("terminal", ["skip", "closed"])
    def test_recovery_label_readback_rejects_terminal_race(
        self,
        make_ctx: Any,
        make_work_item: Any,
        terminal: str,
    ) -> None:
        """A terminal state appearing during a label write cannot confirm success."""

        class TerminalDuringEditGitHub(FakeStageGitHub):
            def edit_labels(
                self,
                issue_number: int,
                *,
                add: list[str],
                remove: list[str],
            ) -> None:
                super().edit_labels(issue_number, add=add, remove=remove)
                if terminal == "skip":
                    self._issue_labels(issue_number).add(STATE_SKIP)
                else:
                    self._issue_state = "CLOSED"

        github = TerminalDuringEditGitHub(labels=[STATE_NEEDS_PLAN], merged_pr=90)
        item = make_work_item(issue=76, state="REQUIREMENTS_RECOVERY_APPLY")
        item.payload.update(
            {
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.REQUIREMENTS, "", "Weak.", "Evidence"
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.NOGO, RecoveryDisposition.REQUIREMENTS, "Insufficient."
                ),
            }
        )

        result = PlanningStage().step(
            item,
            make_ctx(github=github, budget_fn=lambda _name: 1),
        )

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL
        assert item.attempts["plan"] == 1

    def test_obsolete_skip_records_reason_in_one_actor_owned_comment(
        self, make_ctx: Any, make_work_item: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
        item = make_work_item(issue=3, state="REQUIREMENTS_RECOVERY_APPLY")
        item.payload.update(
            {
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.OBSOLETE, "", "Already supplied by PR #9.", "PR #9"
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.GO, RecoveryDisposition.OBSOLETE, "PR #9 is merged."
                ),
            }
        )

        with caplog.at_level("INFO"):
            result = stage.step(item, make_ctx(github=github))

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_PASS
        assert github.labels[3] == {STATE_SKIP}
        assert (
            sum(action == "gh_issue_upsert_comment" for action, _args in github.mutation_log) == 1
        )
        assert any("PR #9 is merged" in record.message for record in caplog.records)

    def test_recovery_transition_preserves_concurrent_operator_block(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Recovery cannot remove the externally owned plan-blocked latch."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN, STATE_PLAN_BLOCKED])
        item = make_work_item(issue=33, state="REQUIREMENTS_RECOVERY_APPLY")
        item.attempts["plan"] = 1
        item.payload.update(
            {
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.REQUIREMENTS, "Guess.", "Weak.", "None"
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.NOGO,
                    RecoveryDisposition.REQUIREMENTS,
                    "Unsupported.",
                ),
            }
        )

        result = stage.step(item, make_ctx(github=github, budget_fn=lambda _name: 2))

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.BLOCKED
        assert STATE_PLAN_BLOCKED in github.labels[33]

    def test_recovery_nogo_uses_plan_no_go_not_blocked(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
        item = make_work_item(issue=4, state="REQUIREMENTS_RECOVERY_APPLY")
        item.attempts["plan"] = 1
        item.payload.update(
            {
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.REQUIREMENTS, "Guess.", "Weak.", "None"
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.NOGO, RecoveryDisposition.REQUIREMENTS, "Unsupported."
                ),
            }
        )

        result = stage.step(item, make_ctx(github=github, budget_fn=lambda _name: 2))

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL
        assert github.labels[4] == {STATE_PLAN_NO_GO}
        assert STATE_PLAN_BLOCKED not in github.labels[4]

    @pytest.mark.parametrize(
        "legacy_label",
        [STATE_IMPLEMENTATION_GO, STATE_IMPLEMENTATION_NO_GO],
    )
    def test_recovery_nogo_removes_legacy_issue_implementation_state(
        self,
        make_ctx: Any,
        make_work_item: Any,
        legacy_label: str,
    ) -> None:
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN, legacy_label])
        item = make_work_item(issue=46, state="REQUIREMENTS_RECOVERY_APPLY")
        item.attempts["plan"] = 1
        item.payload.update(
            {
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.REQUIREMENTS, "Guess.", "Weak.", "None"
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.NOGO,
                    RecoveryDisposition.REQUIREMENTS,
                    "Unsupported.",
                ),
            }
        )

        result = stage.step(item, make_ctx(github=github, budget_fn=lambda _name: 2))

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL
        assert github.labels[46] == {STATE_PLAN_NO_GO}

    def test_mismatched_tracker_review_never_applies_skip(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
        item = make_work_item(issue=5, state="REQUIREMENTS_RECOVERY_APPLY")
        item.payload.update(
            {
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.TRACKER, "", "Tracks children.", "Checklist"
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.GO,
                    RecoveryDisposition.REQUIREMENTS,
                    "This is an implementation task.",
                ),
            }
        )

        result = stage.step(item, make_ctx(github=github))

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.RETRY
        assert STATE_SKIP not in {label["name"] for label in github.gh_issue_json(5)["labels"]}

    def test_body_digest_conflict_never_overwrites_concurrent_human_edit(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = PlanningStage()
        original = f"{PLAN_CANONICAL_MARKER}\nDerived"
        github = FakeStageGitHub(labels=[STATE_PLAN_GO], issue_body="Human edit")
        item = make_work_item(issue=6, state="REQUIREMENTS_RECOVERY_APPLY")
        item.payload.update(
            {
                "issue_title": "Retry bug",
                "issue_body": original,
                "issue_body_digest": "a" * 64,
                "requirements_evidence_digest": "b" * 64,
                "requirements_recovery_contaminated": True,
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.REQUIREMENTS,
                    "Keep retries bounded.",
                    "Recovered.",
                    "Tests.",
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.GO,
                    RecoveryDisposition.REQUIREMENTS,
                    "Confirmed.",
                ),
            }
        )

        result = stage.step(item, make_ctx(github=github))

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.RETRY
        assert github.gh_issue_json(6)["body"] == "Human edit"
        assert item.state == "ENTER"
        assert item.payload["_enter_pending"] is True

    def test_recovery_publication_never_writes_the_human_owned_issue_body(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A human edit between model work and publication remains untouched."""
        stage = PlanningStage()
        source = f"{PLAN_CANONICAL_MARKER}\nDerived"
        github = FakeStageGitHub(labels=[STATE_PLAN_GO], issue_body="Human edit")
        item = make_work_item(issue=60, state="REQUIREMENTS_RECOVERY_APPLY")
        item.payload.update(
            {
                "issue_title": "A task",
                "issue_body": source,
                "issue_source_body": source,
                "issue_body_digest": hashlib.sha256(source.encode()).hexdigest(),
                "requirements_evidence_digest": "b" * 64,
                "requirements_recovery_contaminated": True,
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.REQUIREMENTS,
                    "Keep retries bounded.",
                    "Recovered.",
                    "Tests.",
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.GO, RecoveryDisposition.REQUIREMENTS, "Confirmed."
                ),
            }
        )

        result = stage.step(item, make_ctx(github=github))

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.RETRY
        assert github.gh_issue_json(60)["body"] == "Human edit"
        assert not any(
            comment.lstrip().startswith(RECOVERY_PROVENANCE_PREFIX)
            for comment in github.comments.get(60, [])
        )

    def test_recovered_comment_with_semantic_candidate_restarts_as_fresh_revision(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = PlanningStage()
        source = f"{PLAN_CANONICAL_MARKER}\nDerived tracker text"
        recovered = _recovered_body(
            source,
            "New requirements",
            issue=7,
            title="Tracking issue for retry work",
        )
        github = FakeStageGitHub(
            labels=[STATE_PLAN_NO_GO],
            issue_title="Tracking issue for retry work",
            issue_body=source,
            has_plan=True,
        )
        github.comments[7] = [
            render_current_plan("Stale plan", revision=1),
            render_current_review("Review pending for implementation plan revision 1.", revision=1),
            recovered,
        ]
        item = make_work_item(issue=7, state="ENTER")
        _bind_recovery_revision(item)

        outcome = stage.on_enter(item, make_ctx(github=github))

        assert outcome is None
        assert item.state == "ENTER"
        assert item.payload["requires_plan_revision"] is True
        assert item.payload["issue_source_body"] == source
        assert item.payload["issue_body"] == "New requirements"
        assert "plan_text" not in item.payload
        assert "plan_revision" not in item.payload

    def test_recovered_successor_restart_resumes_pending_review_without_replan_entry(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A linked recovered successor survives a fresh planning work item."""
        stage = PlanningStage()
        source = f"{PLAN_CANONICAL_MARKER}\nDerived tracker text"
        requirements = "New requirements"
        plan = "Recovered successor plan"
        source_digest = hashlib.sha256(source.encode()).hexdigest()
        plan_digest = hashlib.sha256(plan.encode()).hexdigest()
        recovered = _recovered_body(
            source,
            requirements,
            issue=72,
            title="Tracking issue for retry work",
            source_digest=source_digest,
            successor_revision=2,
            successor_plan_digest=plan_digest,
        )
        github = FakeStageGitHub(
            labels=[STATE_NEEDS_PLAN],
            issue_title="Tracking issue for retry work",
            issue_body=source,
            has_plan=True,
        )
        github.comments[72] = [
            render_current_plan(
                plan,
                revision=2,
                recovery_source_digest=source_digest,
            ),
            render_current_review("Review pending for implementation plan revision 2.", revision=2),
            recovered,
        ]
        item = make_work_item(issue=72, state="ENTER")
        _bind_recovery_revision(item)

        outcome = stage.on_enter(item, make_ctx(github=github))

        assert outcome is None
        assert item.state == "VERIFY"
        assert item.payload["plan_text"] == plan
        assert item.payload["plan_revision"] == 2
        assert "requires_plan_revision" not in item.payload
        assert not github.mutation_log

    @pytest.mark.parametrize(
        ("labels", "review", "expected_state"),
        [
            (
                [STATE_NEEDS_PLAN],
                "Review pending for implementation plan revision 2.",
                "VERIFY",
            ),
            ([STATE_PLAN_NO_GO], "Add rollback.\n\nstate:plan-no-go", "ENTER"),
        ],
    )
    def test_recovered_plan_restart_reuses_matching_source_epoch(
        self,
        make_ctx: Any,
        make_work_item: Any,
        labels: list[str],
        review: str,
        expected_state: str,
    ) -> None:
        """Only a plan predating recovery is discarded on restart."""
        stage = PlanningStage()
        source = f"{PLAN_CANONICAL_MARKER}\nDerived"
        source_digest = github_api_mod.issue_body_digest(source)
        recovered = _recovered_body(
            source,
            "Recovered requirements",
            issue=72,
            source_digest=source_digest,
        )
        github = FakeStageGitHub(labels=labels, issue_body=source, has_plan=True)
        github.comments[72] = [
            render_current_plan(
                "Fresh recovered plan",
                revision=2,
                recovery_source_digest=source_digest,
            ),
            render_current_review(review, revision=2),
            recovered,
        ]
        item = make_work_item(issue=72, state="ENTER")
        _bind_recovery_revision(item)

        outcome = stage.on_enter(item, make_ctx(github=github))

        assert outcome is None
        assert item.state == expected_state
        assert item.payload["plan_text"] == "Fresh recovered plan"
        assert item.payload["requirements_recovery_source_digest"] == source_digest
        if labels == [STATE_PLAN_NO_GO]:
            assert item.payload["requires_plan_revision"] is True
            assert "Add rollback." in item.payload["issue_history"]
        else:
            assert "requires_plan_revision" not in item.payload
            result = stage.step(item, make_ctx(github=github))
            assert isinstance(result, StageOutcome)
            assert result.disposition is Disposition.ADVANCE
            assert github.mutation_log == [
                ("gh_issue_upsert_comment", (72, RECOVERY_PROVENANCE_PREFIX))
            ]

    def test_recovered_plan_publication_binds_its_successor_provenance(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The fresh recovery epoch records the exact plan it publishes."""
        stage = PlanningStage()
        source = f"{PLAN_CANONICAL_MARKER}\nDerived tracker text"
        recovered = _recovered_body(source, "New requirements", issue=73)
        github = FakeStageGitHub(
            labels=[STATE_PLAN_NO_GO],
            issue_body=source,
        )
        github.comments[73] = [recovered]
        item = make_work_item(issue=73, state="VERIFY")
        item.payload.update(
            {
                "issue_source_body": source,
                "issue_title": "A task",
                "issue_body_digest": hashlib.sha256(source.encode()).hexdigest(),
                "requirements_recovered_comment": True,
                "requires_plan_revision": True,
                "plan_text": "Recovered successor plan",
            }
        )

        result = stage.step(item, make_ctx(github=github))

        assert isinstance(result, StageOutcome)
        assert result.disposition is Disposition.ADVANCE
        provenance_comment = next(
            comment
            for comment in github.comments[73]
            if comment.startswith(RECOVERY_PROVENANCE_PREFIX)
        )
        provenance = parse_recovery_provenance(provenance_comment)
        assert provenance is not None
        assert provenance.successor_revision == 1
        assert provenance.successor_plan_digest == plan_fingerprint("Recovered successor plan")

    def test_recovered_plan_followup_retry_does_not_republish_identical_plan(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        class DropFirstPendingLabelGitHub(FakeStageGitHub):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self.drop_pending_once = True

            def edit_labels(
                self,
                issue_number: int,
                *,
                add: list[str],
                remove: list[str],
            ) -> None:
                if self.drop_pending_once and STATE_NEEDS_PLAN in add:
                    self.drop_pending_once = False
                    return
                super().edit_labels(issue_number, add=add, remove=remove)

        source = f"{PLAN_CANONICAL_MARKER}\nDerived tracker text"
        recovered = _recovered_body(source, "New requirements", issue=74)
        github = DropFirstPendingLabelGitHub(
            labels=[STATE_PLAN_NO_GO],
            issue_body=source,
        )
        github.comments[74] = [recovered]
        item = make_work_item(issue=74, state="VERIFY")
        item.payload.update(
            {
                "issue_source_body": source,
                "issue_title": "A task",
                "issue_body_digest": hashlib.sha256(source.encode()).hexdigest(),
                "requirements_recovered_comment": True,
                "requires_plan_revision": True,
                "plan_text": "Recovered successor plan",
            }
        )
        ctx = make_ctx(github=github)

        first = PlanningStage().step(item, ctx)
        plan_writes_after_first = sum(
            mutation[0] == "gh_issue_upsert_comment" and mutation[1][1] == PLAN_CANONICAL_MARKER
            for mutation in github.mutation_log
        )
        second = PlanningStage().step(item, ctx)

        assert isinstance(first, StageOutcome)
        assert first.disposition is Disposition.RETRY
        assert isinstance(second, StageOutcome)
        assert second.disposition is Disposition.ADVANCE
        assert plan_writes_after_first == 1
        assert (
            sum(
                mutation[0] == "gh_issue_upsert_comment" and mutation[1][1] == PLAN_CANONICAL_MARKER
                for mutation in github.mutation_log
            )
            == 1
        )
        assert "published_plan_pending_followup" not in item.payload

    @pytest.mark.parametrize(
        ("labels", "review", "expected_state"),
        [
            (
                [STATE_NEEDS_PLAN],
                "Review pending for implementation plan revision 3.",
                "VERIFY",
            ),
            ([STATE_PLAN_NO_GO], "Add tests.\n\nstate:plan-no-go", "ENTER"),
        ],
    )
    def test_forced_recovery_restart_reuses_both_epoch_markers(
        self,
        make_ctx: Any,
        make_work_item: Any,
        labels: list[str],
        review: str,
        expected_state: str,
    ) -> None:
        """Force and recovery provenance jointly resume one durable epoch."""
        source = f"{PLAN_CANONICAL_MARKER}\nDerived"
        source_digest = github_api_mod.issue_body_digest(source)
        github = FakeStageGitHub(labels=labels, issue_body=source, has_plan=True)
        github.comments[75] = [
            render_current_plan(
                "Forced recovered plan",
                revision=3,
                forced_planning_epoch=True,
                recovery_source_digest=source_digest,
            ),
            render_current_review(review, revision=3),
            _recovered_body(
                source,
                "Recovered requirements",
                issue=75,
                source_digest=source_digest,
            ),
        ]
        item = make_work_item(issue=75, state="ENTER")
        _bind_recovery_revision(item)

        outcome = PlanningStage().on_enter(
            item,
            make_ctx(github=github, config=SimpleNamespace(force=True)),
        )

        assert outcome is None
        assert item.state == expected_state
        assert item.payload["plan_text"] == "Forced recovered plan"
        assert item.payload["forced_planning_epoch_started"] is True
        if labels == [STATE_NEEDS_PLAN]:
            assert github.mutation_log == [
                ("gh_issue_upsert_comment", (75, RECOVERY_PROVENANCE_PREFIX))
            ]
        else:
            assert github.mutation_log == []
        if labels == [STATE_PLAN_NO_GO]:
            assert item.payload["requires_plan_revision"] is True
            assert "Add tests." in item.payload["issue_history"]

    def test_recovery_with_force_starts_durable_forced_epoch(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = PlanningStage()
        source = f"{PLAN_CANONICAL_MARKER}\nDerived"
        github = FakeStageGitHub(
            labels=[STATE_PLAN_GO],
            issue_title="Retry bug",
            issue_body=source,
        )
        item = make_work_item(issue=70, state="REQUIREMENTS_RECOVERY_APPLY")
        item.payload.update(
            {
                "issue_title": "Retry bug",
                "issue_body": source,
                "issue_source_body": source,
                "issue_body_digest": github.gh_issue_json(70)["bodyDigest"],
                "requirements_evidence_digest": _recovery_binding(
                    source,
                    issue=70,
                    title="Retry bug",
                ),
                "requirements_repository_revision": _RECOVERY_REVISION,
                "requirements_recovery_contaminated": True,
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.REQUIREMENTS,
                    "Keep retries bounded.",
                    "Recovered.",
                    "Tests.",
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.GO,
                    RecoveryDisposition.REQUIREMENTS,
                    "Confirmed.",
                ),
            }
        )

        result = stage.step(
            item,
            make_ctx(github=github, config=SimpleNamespace(force=True, enable_advise=False)),
        )

        assert isinstance(result, Continue)
        assert result.next_state == "PLAN_WAIT"
        assert item.payload["forced_planning_epoch_started"] is True

        item.state = "VERIFY"
        item.payload["plan_text"] = "Fresh recovered plan"
        published = stage.step(
            item,
            make_ctx(github=github, config=SimpleNamespace(force=True, enable_advise=False)),
        )

        assert isinstance(published, StageOutcome)
        assert published.disposition == Disposition.ADVANCE
        assert any(FORCED_PLANNING_EPOCH_MARKER in body for body in github.comments[70])

    def test_false_semantic_candidate_does_not_consume_force_request(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A semantic false positive must return through forced entry."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_GO], has_plan=True)
        item = make_work_item(issue=71, state="REQUIREMENTS_RECOVERY_APPLY")
        item.payload.update(
            {
                "requirements_recovery_contaminated": False,
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.REQUIREMENTS,
                    "Keep the existing requirements.",
                    "This is an ordinary task.",
                    "Issue body.",
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.GO,
                    RecoveryDisposition.REQUIREMENTS,
                    "Confirmed ordinary task.",
                ),
            }
        )

        result = stage.step(
            item,
            make_ctx(github=github, config=SimpleNamespace(force=True)),
        )

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.RETRY
        assert item.state == "ENTER"
        assert {label["name"] for label in github.gh_issue_json(71)["labels"]} == {STATE_PLAN_GO}

    def test_confirmed_obsolete_skip_has_one_restart_safe_explanation_comment(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN], issue_body="Already resolved.")
        item = make_work_item(issue=74, state="REQUIREMENTS_RECOVERY_APPLY")
        item.payload.update(
            {
                "recovered_requirements": RecoveredRequirements(
                    RecoveryDisposition.OBSOLETE, "", "Merged replacement.", "Merged PR #1."
                ),
                "requirements_recovery_review": RecoveryReview(
                    RecoveryVerdict.GO,
                    RecoveryDisposition.OBSOLETE,
                    "Merged replacement confirmed.",
                ),
            }
        )

        result = stage.step(item, make_ctx(github=github))
        retry = stage.step(item, make_ctx(github=github))

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_PASS
        assert isinstance(retry, StageOutcome)
        explanations = [
            comment
            for comment in github.comments[74]
            if comment.lstrip().startswith("<!-- hephaestus-obsolete-explanation:")
        ]
        assert len(explanations) == 1
        assert "Merged replacement confirmed." in explanations[0]

    def test_failed_forced_revision_planner_retries_instead_of_accepting_old_plan(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An old canonical plan cannot satisfy a pending forced revision."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO], has_plan=True)
        item = make_work_item(
            issue=73,
            state="VERIFY",
            payload={"requires_plan_revision": True},
        )

        result = stage.step(item, make_ctx(github=github))

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.RETRY
        assert item.state == "PLAN_WAIT"
        assert item.attempts["plan"] == 1

    def test_enter_skips_advise_when_disabled(self, make_ctx: Any, make_work_item: Any) -> None:
        """ENTER advances straight to PLAN_WAIT when advise is disabled."""
        stage = PlanningStage()
        ctx = make_ctx()
        ctx.config.enable_advise = False
        item = make_work_item(issue=2, state="ENTER")

        result = stage.step(item, ctx)

        assert isinstance(result, Continue)
        assert result.next_state == "PLAN_WAIT"

    def test_advise_wait_requests_advise_job(self, make_ctx: Any, make_work_item: Any) -> None:
        """ADVISE_WAIT submits the advise job and lands in PLAN_WAIT."""
        stage = PlanningStage()
        ctx = make_ctx()
        item = make_work_item(issue=3, state="ADVISE_WAIT")

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AthenaSkillJob)  # narrow the job union
        assert result.on_done_state == "PLAN_WAIT"
        assert result.job.descr == "advise"
        assert result.job.request.kind == "advise"
        assert result.job.request.payload["issue_number"] == 3

    def test_codex_advise_job_is_provider_neutral_read_only(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Codex advise turns keep provider identity in the host-owned request."""
        stage = PlanningStage()
        ctx = make_ctx()
        ctx.config.agent = "codex"
        item = make_work_item(issue=3, state="ADVISE_WAIT")

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AthenaSkillJob)
        assert result.job.request.agent == "codex"
        assert result.job.request.kind == "advise"

    def test_codex_advise_job_builds_same_typed_request_shape(
        self,
        make_ctx: Any,
        make_work_item: Any,
    ) -> None:
        """The planning job now uses the provider-neutral Athena request contract."""
        stage = PlanningStage()
        ctx = make_ctx()
        ctx.config.agent = "codex"
        item = make_work_item(issue=3, state="ADVISE_WAIT")
        request = stage.step(item, ctx)

        assert isinstance(request, JobRequest)
        assert isinstance(request.job, AthenaSkillJob)
        assert request.job.request.agent == "codex"
        assert request.job.request.payload == {
            "issue_number": 3,
            "issue_title": "",
            "issue_body": "",
        }

    def test_plan_wait_requests_plan_job(self, make_ctx: Any, make_work_item: Any) -> None:
        """PLAN_WAIT submits the plan job (planner session) and lands in VERIFY."""
        stage = PlanningStage()
        ctx = make_ctx()
        item = make_work_item(issue=4, state="PLAN_WAIT")
        item.payload["issue_title"] = "Retry failure"
        item.payload["issue_body"] = "The loop retries forever."
        item.payload["advise_findings"] = "prior learnings"

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.on_done_state == "VERIFY"
        assert result.job.descr == "plan"
        assert result.job.sandbox == "read-only"
        assert result.job.prompt_builder is build_plan_prompt
        # Advise findings travel via prompt_kwargs (builders run in-worker;
        # AgentJob is frozen, so no closures over payload).
        assert result.job.prompt_kwargs == {
            "issue_number": 4,
            "issue_title": "Retry failure",
            "issue_body": "The loop retries forever.",
            "advise_findings": "prior learnings",
            "issue_history": "",
        }

    def test_plan_job_uses_selected_provider_and_planner_session_role(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Provider selection is distinct from the persisted planner session role."""
        stage = PlanningStage()
        config = type(
            "Cfg",
            (),
            {
                "enable_advise": True,
                "enable_learn": True,
                "force": False,
                "agent": "codex",
                "model": "gpt-default",
                "planner_model": "gpt-plan",
                "reviewer_model": "",
                "implementer_model": "",
                "dry_run": False,
            },
        )()
        ctx = make_ctx(config=config)
        item = make_work_item(issue=9, state="PLAN_WAIT")

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.agent == "codex"
        assert result.job.session_agent == "planner"
        assert result.job.model == "gpt-plan"

    def test_verify_with_plan_advances(self, make_ctx: Any, make_work_item: Any) -> None:
        """VERIFY with an existing plan comment advances without re-posting."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN], has_plan=True)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=5, state="VERIFY")
        item.payload["plan_text"] = "# Implementation Plan\n\nAlready posted."

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE
        assert github.mutation_log == []  # existing plan: no duplicate upsert

    def test_verify_rechecks_found_plan_before_advancing(
        self, make_ctx: Any, make_work_item: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plan appearing after the initial absence read must not spend retry budget."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
        lookups = iter(
            [
                PlanDiscoveryResult.absent(),
                PlanDiscoveryResult.found(render_current_plan("Concurrent plan")),
            ]
        )
        monkeypatch.setattr(github, "discover_plan", lambda _issue: next(lookups))
        item = make_work_item(issue=15, state="VERIFY")

        outcome = stage.step(item, make_ctx(github=github))

        assert outcome == StageOutcome(Disposition.ADVANCE, "plan generated and verified")
        assert item.attempts.get("plan", 0) == 0
        assert github.mutation_log == []

    def test_verify_retries_when_plan_disappears_before_advancing(
        self, make_ctx: Any, make_work_item: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plan deleted after discovery cannot authorize plan-review advancement."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
        lookups = iter(
            [
                PlanDiscoveryResult.found(render_current_plan("Initially durable")),
                PlanDiscoveryResult.absent(),
            ]
        )
        monkeypatch.setattr(github, "discover_plan", lambda _issue: next(lookups))
        item = make_work_item(issue=16, state="VERIFY")

        outcome = stage.step(item, make_ctx(github=github))

        assert outcome == StageOutcome(Disposition.RETRY, "plan disappeared before verification")
        assert item.attempts.get("plan", 0) == 0
        assert github.mutation_log == []

    def test_on_enter_persistent_journal_read_error_is_bounded_to_plan_no_go(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Persistent journal corruption cannot requeue the issue forever."""
        github = FakeStageGitHub(labels=[], journal_read_error="rate limited")
        item = make_work_item(issue=11, state="ENTER")
        ctx = make_ctx(github=github, budget_fn=lambda _name: 2)

        first = PlanningStage().on_enter(item, ctx)
        second = PlanningStage().on_enter(item, ctx)

        assert isinstance(first, StageOutcome)
        assert first.disposition == Disposition.RETRY
        assert isinstance(second, StageOutcome)
        assert second.disposition == Disposition.FINISH_FAIL
        assert item.state == "ENTER"
        assert item.attempts["plan"] == 2
        assert github.labels[11] == {STATE_PLAN_NO_GO}

    def test_verify_read_error_is_bounded_to_plan_no_go(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An uncertain plan lookup cannot requeue VERIFY forever."""
        github = FakeStageGitHub(
            labels=[STATE_NEEDS_PLAN],
            plan_read_error="malformed comment body",
        )
        item = make_work_item(issue=12, state="VERIFY")
        item.payload["plan_text"] = "# Implementation Plan\n\nCandidate"
        ctx = make_ctx(github=github, budget_fn=lambda _name: 2)

        first = PlanningStage().step(item, ctx)
        item.state = "VERIFY"
        second = PlanningStage().step(item, ctx)

        assert isinstance(first, StageOutcome)
        assert first.disposition == Disposition.RETRY
        assert isinstance(second, StageOutcome)
        assert second.disposition == Disposition.FINISH_FAIL
        assert item.attempts["plan"] == 2
        assert github.labels[12] == {STATE_PLAN_NO_GO}

    @pytest.mark.parametrize(
        ("github", "expected_disposition"),
        [
            (
                FakeStageGitHub(labels=[STATE_SKIP], plan_read_error="unavailable"),
                Disposition.SKIP,
            ),
            (
                FakeStageGitHub(
                    issue_state="CLOSED",
                    merged_pr=91,
                    plan_read_error="unavailable",
                ),
                Disposition.FINISH_PASS,
            ),
        ],
    )
    def test_verify_retry_honors_terminal_live_issue_state(
        self,
        make_ctx: Any,
        make_work_item: Any,
        github: FakeStageGitHub,
        expected_disposition: Disposition,
    ) -> None:
        """VERIFY read failures do not mutate a skipped or closed issue."""
        item = make_work_item(issue=77, state="VERIFY")
        item.payload["plan_text"] = "Candidate"

        result = PlanningStage().step(item, make_ctx(github=github))

        assert isinstance(result, StageOutcome)
        assert result.disposition == expected_disposition
        assert item.attempts.get("plan", 0) == 0
        assert github.mutation_log == []

    def test_verify_publication_journal_error_is_bounded_to_plan_no_go(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A persistent publication journal failure consumes the same plan budget."""
        github = FakeStageGitHub(
            labels=[STATE_NEEDS_PLAN],
            journal_read_error="timeline unavailable",
        )
        item = make_work_item(issue=13, state="VERIFY")
        item.payload["plan_text"] = "# Implementation Plan\n\nCandidate"
        ctx = make_ctx(github=github, budget_fn=lambda _name: 2)

        first = PlanningStage().step(item, ctx)
        item.state = "VERIFY"
        second = PlanningStage().step(item, ctx)

        assert isinstance(first, StageOutcome)
        assert first.disposition == Disposition.RETRY
        assert isinstance(second, StageOutcome)
        assert second.disposition == Disposition.FINISH_FAIL
        assert item.attempts["plan"] == 2
        assert github.labels[13] == {STATE_PLAN_NO_GO}

    def test_production_adapter_malformed_comment_retries_without_mutation(
        self,
        make_ctx: Any,
        make_work_item: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Malformed REST comments cannot be treated as an absent plan."""
        adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)
        monkeypatch.setattr(
            adapter,
            "_repo_issue_comments",
            lambda issue: [{"body": None, "user": {"login": "bot"}}],
        )
        monkeypatch.setattr(
            adapter,
            "gh_issue_json",
            lambda issue: {
                "number": issue,
                "title": "Task",
                "body": "Requirements",
                "bodyDigest": github_api_mod.issue_body_digest("Requirements"),
                "state": "OPEN",
                "labels": [{"name": STATE_NEEDS_PLAN}],
            },
        )
        monkeypatch.setattr(github_api_mod, "gh_current_login", lambda: "bot")
        item = make_work_item(issue=14, state="VERIFY")
        item.payload["plan_text"] = "# Implementation Plan\n\nCandidate"

        outcome = PlanningStage().step(item, make_ctx(github=adapter))

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.RETRY
        assert item.state == "ENTER"
        assert item.attempts["plan"] == 1

    def test_verify_posts_plan_comment_then_advances(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The PIPELINE posts the plan comment (M1).

        VERIFY upserts the durable artifact BEFORE the verify/ADVANCE
        decision (journal order).
        """
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN], has_plan=False)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=11, state="VERIFY")
        item.payload["plan_text"] = "# Implementation Plan\n\nDo the thing."

        result = stage.step(item, ctx)

        # Durable write happened, in journal order, before ADVANCE existed.
        assert github.mutation_log == [
            ("gh_issue_upsert_comment", (11, PLAN_CANONICAL_MARKER)),
            ("gh_issue_upsert_comment", (11, PLAN_REVIEW_CANONICAL_MARKER)),
        ]
        assert github.comments[11][0] == (
            f"{PLAN_CANONICAL_MARKER}\n{PLAN_COMMENT_MARKER}\n<!-- revision: 1 -->\n\nDo the thing."
        )
        assert github.comments[11][1].startswith(PLAN_REVIEW_CANONICAL_MARKER)
        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE

    def test_verify_advances_after_upsert_even_when_old_review_gate_is_false(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A just-written revised plan is valid even before a new review exists."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN], has_plan=False)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=24, state="VERIFY")
        item.payload["plan_text"] = "# Implementation Plan\n\nRevised plan."

        result = stage.step(item, ctx)

        assert github.mutation_log == [
            ("gh_issue_upsert_comment", (24, PLAN_CANONICAL_MARKER)),
            ("gh_issue_upsert_comment", (24, PLAN_REVIEW_CANONICAL_MARKER)),
        ]
        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE

    def test_replan_replaces_both_canonical_comments_without_history(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Feedback-triggered planning uses the same durable revision transaction."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO], has_plan=False)
        github.comments[27] = [
            render_current_plan("Plan v1", revision=1),
            render_current_review("Missing rollback.\n\nstate:plan-no-go", revision=1),
        ]
        ctx = make_ctx(github=github)
        item = make_work_item(issue=27, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        item.state = "VERIFY"
        item.payload["plan_text"] = "Plan v2 with rollback"
        outcome = stage.step(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.ADVANCE
        comments = github.comments[27]
        assert len(comments) == 2
        assert "<!-- revision: 2 -->" in comments[0]
        assert "Review pending for implementation plan revision 2" in comments[1]
        assert [entry[0] for entry in github.mutation_log] == [
            "gh_issue_upsert_comment",
            "gh_issue_upsert_comment",
            "edit_labels",
        ]

    def test_revised_plan_cannot_advance_with_needs_plan_and_stale_sibling(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Replacement publication waits for an exclusive confirmed label state."""

        class PartialLabelGitHub(FakeStageGitHub):
            def edit_labels(self, issue_number: int, *, add: list[str], remove: list[str]) -> None:
                self._issue_labels(issue_number).update(add)
                self._log("edit_labels", issue_number, tuple(add), tuple(remove))

        stage = PlanningStage()
        github = PartialLabelGitHub(labels=[STATE_PLAN_NO_GO], has_plan=False)
        github.comments[272] = [
            render_current_plan("Plan v1", revision=1),
            render_current_review("Missing rollback.", revision=1),
        ]
        ctx = make_ctx(github=github)
        item = make_work_item(issue=272, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        item.state = "VERIFY"
        item.payload["plan_text"] = "Plan v2 with rollback"
        outcome = stage.step(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.RETRY
        assert github.labels[272] == {STATE_NEEDS_PLAN, STATE_PLAN_NO_GO}

    def test_operator_blocked_latch_stops_inflight_planner_before_publish(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A BLOCKED label arriving during the planner job prevents all publication."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_BLOCKED], has_plan=False)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=273, state="VERIFY")
        item.payload["plan_text"] = "A newly generated plan"

        outcome = stage.step(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.BLOCKED
        assert github.labels[273] == {STATE_PLAN_BLOCKED}
        assert github.mutation_log == []

    def test_no_go_label_authorizes_revision_without_review_state_text(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Only the GitHub label, not a token in review prose, authorizes superseding."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO], has_plan=False)
        github.comments[270] = [
            render_current_plan("Plan v1", revision=1),
            render_current_review("Missing rollback details.", revision=1),
        ]
        ctx = make_ctx(github=github)
        item = make_work_item(issue=270, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        item.state = "VERIFY"
        item.payload["plan_text"] = "Plan v2 with rollback"

        outcome = stage.step(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.ADVANCE
        assert "<!-- revision: 2 -->" in github.comments[270][0]

    def test_concurrent_revision_owner_ejects_item_without_poisoning_pipeline(
        self,
        make_ctx: Any,
        make_work_item: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A lost plan-label race is reported as another item's work, not fatal."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN], has_plan=False)
        github.comments[271] = [
            render_current_plan("Plan v1", revision=1),
            render_current_review("Missing rollback.\n\nstate:plan-no-go", revision=1),
        ]
        ctx = make_ctx(github=github)
        item = make_work_item(issue=271, state="VERIFY")
        item.payload["plan_text"] = "Plan v2 with rollback"
        item.payload["requires_plan_revision"] = True

        with caplog.at_level("INFO"):
            outcome = stage.step(item, ctx)

        assert outcome == StageOutcome(
            Disposition.FINISH_PASS,
            "plan is being worked by another pipeline item; ejected from queue",
        )
        assert any(
            "being worked by another pipeline item" in message for message in caplog.messages
        )

    def test_replan_without_change_publishes_blocked_review_and_stops(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A repeated replan exits before another review iteration is queued."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO], has_plan=False)
        github.comments[28] = [
            render_current_plan("Plan v1", revision=1),
            render_current_review("Missing rollback.\n\nstate:plan-no-go", revision=1),
        ]
        ctx = make_ctx(github=github)
        item = make_work_item(issue=28, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        item.state = "VERIFY"
        item.payload["plan_text"] = "Plan v1"
        outcome = stage.step(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.BLOCKED
        assert len(github.comments[28]) == 2
        assert github.comments[28][1].endswith(STATE_PLAN_BLOCKED)
        assert github.mutation_log[-2:] == [
            (
                "edit_labels",
                (
                    28,
                    (STATE_PLAN_BLOCKED,),
                    (
                        STATE_NEEDS_PLAN,
                        STATE_PLAN_NO_GO,
                        STATE_PLAN_GO,
                        ATHENA_FINALIZED_PLAN_LABEL,
                    ),
                ),
            ),
            ("gh_issue_upsert_comment", (28, PLAN_REVIEW_CANONICAL_MARKER)),
        ]

    def test_replan_without_change_latches_blocked_if_comment_write_fails(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A failed audit write cannot prevent the durable BLOCKED safety latch."""

        class FailingReviewCommentGitHub(FakeStageGitHub):
            def upsert_issue_comment(self, *args: Any, **kwargs: Any) -> None:
                if len(args) > 1 and args[1] == PLAN_REVIEW_CANONICAL_MARKER:
                    raise RuntimeError("comment write failed")
                super().upsert_issue_comment(*args, **kwargs)

        stage = PlanningStage()
        github = FailingReviewCommentGitHub(labels=[STATE_PLAN_NO_GO], has_plan=False)
        github.comments[30] = [
            render_current_plan("Plan v1", revision=1),
            render_current_review("Missing rollback.\n\nstate:plan-no-go", revision=1),
        ]
        ctx = make_ctx(github=github)
        item = make_work_item(issue=30, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        item.state = "VERIFY"
        item.payload["plan_text"] = "Plan v1"
        with pytest.raises(RuntimeError, match="comment write failed"):
            stage.step(item, ctx)

        assert github.labels[30] == {STATE_PLAN_BLOCKED}
        assert github.mutation_log[-1] == (
            "edit_labels",
            (
                30,
                (STATE_PLAN_BLOCKED,),
                (
                    STATE_NEEDS_PLAN,
                    STATE_PLAN_NO_GO,
                    STATE_PLAN_GO,
                    ATHENA_FINALIZED_PLAN_LABEL,
                ),
            ),
        )

    def test_empty_initial_plan_blocks_instead_of_retrying_planner(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An empty successful planner result is a durable no-progress outcome."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN], has_plan=False)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=301, state="PLAN_WAIT")

        stage.on_job_done(item, JobResult(ok=True, value=""), ctx)
        item.state = "VERIFY"
        outcome = stage.step(item, ctx)

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.BLOCKED
        assert github.labels[301] == {STATE_PLAN_BLOCKED}
        assert github.comments[301][-1].endswith(STATE_PLAN_BLOCKED)

    def test_verify_posts_exactly_once_on_reentry(self, make_ctx: Any, make_work_item: Any) -> None:
        """Re-entering VERIFY never double-posts.

        The upsert is guarded by tri-state plan discovery (idempotent on re-entry).
        """
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN], has_plan=False)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=12, state="VERIFY")
        item.payload["plan_text"] = "# Implementation Plan\n\nOnce only."

        first = stage.step(item, ctx)
        second = stage.step(item, ctx)  # re-entry (e.g. after a restart)

        plan_upserts = [
            m
            for m in github.mutation_log
            if m == ("gh_issue_upsert_comment", (12, PLAN_CANONICAL_MARKER))
        ]
        assert len(plan_upserts) == 1
        assert isinstance(first, StageOutcome)
        assert first.disposition == Disposition.ADVANCE
        assert isinstance(second, StageOutcome)
        assert second.disposition == Disposition.ADVANCE

    def test_verify_normalizes_plan_body_to_marker(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Marker normalization is re-housed from _upsert_plan_comment.

        A markerless (or whitespace-prefixed) plan gets the marker prepended.
        """
        stage = PlanningStage()
        github = FakeStageGitHub(has_plan=False)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=13, state="VERIFY")
        item.payload["plan_text"] = "\n\nSome plan without the heading."

        stage.step(item, ctx)

        body = github.comments[13][0]
        assert body.startswith(PLAN_CANONICAL_MARKER)
        assert body == (
            f"{PLAN_CANONICAL_MARKER}\n{PLAN_COMMENT_MARKER}\n"
            "<!-- revision: 1 -->\n\nSome plan without the heading."
        )

    def test_verify_without_plan_retries_by_requesting_fresh_plan(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A missing plan retries through PLAN_WAIT and requests a fresh plan job."""
        stage = PlanningStage()
        github = FakeStageGitHub(has_plan=False)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=6, state="VERIFY")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.RETRY
        assert item.attempts["plan"] == 1
        assert item.state == "PLAN_WAIT"

        retry_request = stage.step(item, ctx)

        assert isinstance(retry_request, JobRequest)
        assert isinstance(retry_request.job, AgentJob)
        assert retry_request.job.descr == "plan"
        assert retry_request.on_done_state == "VERIFY"

    def test_verify_exhausts_budget(self, make_ctx: Any, make_work_item: Any) -> None:
        """VERIFY fails after exhausting the plan budget (2)."""
        stage = PlanningStage()
        github = FakeStageGitHub(has_plan=False)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=7, state="VERIFY")
        item.attempts["plan"] = 1  # this attempt becomes 2/2

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL

    def test_unknown_state_fails(self, make_ctx: Any, make_work_item: Any) -> None:
        """An unknown state finishes failed instead of looping silently."""
        stage = PlanningStage()
        ctx = make_ctx()
        item = make_work_item(issue=8, state="BOGUS")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL

    def test_no_issue_number_fails(self, make_ctx: Any, make_work_item: Any) -> None:
        """Step without an issue number finishes failed."""
        stage = PlanningStage()
        ctx = make_ctx()
        item = make_work_item(issue=None, state="ENTER")

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL


class TestPlanningStageOnJobDone:
    """on_job_done payload handling (state still at the WAIT state)."""

    def test_advise_result_stored_in_payload(self, make_ctx: Any, make_work_item: Any) -> None:
        """The advise job's findings are stored on the payload."""
        stage = PlanningStage()
        ctx = make_ctx()
        item = make_work_item(issue=1, state="ADVISE_WAIT")
        result = JobResult(
            ok=True,
            value=AthenaSkillResult(
                kind="advise",
                context="advise findings here",
                receipt={"binding": "ok"},
            ),
        )

        stage.on_job_done(item, result, ctx)

        assert item.payload["advise_findings"] == "advise findings here"
        assert item.payload["athena_advise_receipt"] == {"binding": "ok"}

    def test_plan_result_stored_in_payload(self, make_ctx: Any, make_work_item: Any) -> None:
        """The plan job's text is stored on the payload."""
        stage = PlanningStage()
        ctx = make_ctx()
        item = make_work_item(issue=2, state="PLAN_WAIT")
        result = JobResult(ok=True, value="# Issue plan here")

        stage.on_job_done(item, result, ctx)

        assert item.payload["plan_text"] == "# Issue plan here"

    def test_failed_result_is_not_stored(self, make_ctx: Any, make_work_item: Any) -> None:
        """A failed job result is logged and never stored."""
        stage = PlanningStage()
        ctx = make_ctx()
        item = make_work_item(issue=3, state="PLAN_WAIT")
        result = JobResult(ok=False, error="agent timeout")

        stage.on_job_done(item, result, ctx)

        assert "plan_text" not in item.payload


class TestPlanningFlowWithFakePool:
    """Drive the whole stage through the canonical FakeWorkerPool (m6)."""

    def test_full_walk_enter_to_advance(self, make_ctx: Any, make_work_item: Any) -> None:
        """Full pool-driven walk of the whole stage.

        ENTER -> ADVISE_WAIT -> PLAN_WAIT -> VERIFY -> ADVANCE, with the
        durable writes in journal order.
        """
        from tests.unit.automation.pipeline.conftest import FakeWorkerPool

        stage = PlanningStage()
        github = FakeStageGitHub()  # unlabeled, no PRs, no plan yet
        ctx = make_ctx(github=github)
        item = make_work_item(issue=40, state="ENTER")

        pool = FakeWorkerPool()
        pool.script(
            JobResult(ok=True, value="advise findings"),  # advise
            JobResult(ok=True, value="# Implementation Plan\n\nSteps."),  # plan
        )

        assert stage.on_enter(item, ctx) is None

        outcome = None
        for _ in range(10):  # bounded driver loop
            result = stage.step(item, ctx)
            if isinstance(result, Continue):
                item.state = result.next_state
                continue
            if isinstance(result, JobRequest):
                pool.submit(result.job, result.on_done_state)
                _handle, job_result = pool.completion_q.get_nowait()
                assert not job_result.interrupted
                stage.on_job_done(item, job_result, ctx)
                item.state = result.on_done_state
                continue
            outcome = result
            break

        assert isinstance(outcome, StageOutcome)
        assert outcome.disposition == Disposition.ADVANCE
        # Both agent jobs ran, in order, with the payload threaded through.
        assert [h.job.descr for h in pool.submitted] == ["advise", "plan"]
        plan_job = pool.submitted[1].job
        assert isinstance(plan_job, AgentJob)  # narrows the job union for mypy
        assert plan_job.prompt_kwargs["advise_findings"] == "advise findings"
        # Durable writes, pinned in journal order: entry label first, then
        # the plan-comment artifact — both before the ADVANCE outcome.
        assert github.mutation_log[1:] == [
            ("gh_issue_upsert_comment", (40, PLAN_CANONICAL_MARKER)),
            ("gh_issue_upsert_comment", (40, PLAN_REVIEW_CANONICAL_MARKER)),
        ]
        assert github.labels[40] == {STATE_NEEDS_PLAN}
        assert PLAN_COMMENT_MARKER in github.comments[40][0]
        assert github.comments[40][0].endswith("Steps.")
