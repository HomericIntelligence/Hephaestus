"""Tests for the planning stage (doc section "2. planning")."""

from __future__ import annotations

import re
from typing import Any

from hephaestus.automation.pipeline.jobs import AgentJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.planning import (
    PlanningStage,
    _normalize_plan_comment,
    build_plan_prompt,
)
from hephaestus.automation.prompts._shared import get_untrusted_notice
from hephaestus.automation.prompts.planning import get_plan_prompt
from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_COMMENT_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
)
from hephaestus.automation.review_journal import (
    IssueComment,
    render_current_plan,
    render_current_review,
)
from hephaestus.automation.state_labels import (
    STATE_NEEDS_PLAN,
    STATE_PLAN_BLOCKED,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
    STATE_SKIP,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _fence_present(prompt: str, label: str) -> bool:
    """Return True when a prompt has nonce-delimited markers for label."""
    return bool(
        re.search(rf"BEGIN_[0-9A-F]+_{label}\b", prompt)
        and re.search(rf"END_[0-9A-F]+_{label}\b", prompt)
    )


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

    def test_merged_pr_closes_issue(self, make_ctx: Any, make_work_item: Any) -> None:
        """A merged closing PR closes the issue as covered (gate A)."""
        stage = PlanningStage()
        github = FakeStageGitHub(merged_pr=123)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=3)

        outcome = stage.on_enter(item, ctx)

        assert outcome is not None
        assert outcome.disposition == Disposition.SKIP
        assert github.mutation_log == [("close_issue_as_covered", (3, 123))]

    def test_open_pr_skips(self, make_ctx: Any, make_work_item: Any) -> None:
        """An open PR for the issue skips planning with zero writes (gate B)."""
        stage = PlanningStage()
        github = FakeStageGitHub(open_pr=456)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=4)

        outcome = stage.on_enter(item, ctx)

        assert outcome is not None
        assert outcome.disposition == Disposition.SKIP
        assert github.mutation_log == []

    def test_unlabeled_entry_adds_needs_plan(self, make_ctx: Any, make_work_item: Any) -> None:
        """Unlabeled entry durably writes state:needs-plan before proceeding."""
        stage = PlanningStage()
        github = FakeStageGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=5)

        outcome = stage.on_enter(item, ctx)

        assert outcome is None  # proceed to step()
        assert github.mutation_log == [("gh_issue_add_labels", (5, (STATE_NEEDS_PLAN,)))]
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

    def test_label_refresh_failure_falls_back_to_cache(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A failing label read falls back to the cached labels."""

        class BrokenGitHub(FakeStageGitHub):
            def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
                raise RuntimeError("gh unavailable")

        stage = PlanningStage()
        github = BrokenGitHub()
        ctx = make_ctx(github=github)
        item = make_work_item(issue=8, labels=[STATE_PLAN_GO])

        outcome = stage.on_enter(item, ctx)

        assert outcome is not None
        assert outcome.disposition == Disposition.ADVANCE  # cached plan-go honored

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

    def test_human_feedback_after_blocked_review_resumes_planner_with_history(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        stage = PlanningStage()
        github = FakeStageGitHub(labels=["state:plan-blocked"], has_plan=True)
        github.comments[14] = [
            f"{PLAN_COMMENT_MARKER}\n\nPlan awaiting a decision.",
            "## 🔍 Plan Review\n\nNeed the API choice.\n\nstate:plan-blocked",
            IssueComment(
                body="Use the existing REST endpoint; do not add GraphQL.",
                author_login="maintainer",
                author_association="MEMBER",
            ),
        ]
        ctx = make_ctx(github=github)
        item = make_work_item(issue=14, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        assert item.state == "ENTER"
        assert "Use the existing REST endpoint" in item.payload["issue_history"]

        item.state = "PLAN_WAIT"
        result = stage.step(item, ctx)
        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert "Use the existing REST endpoint" in result.job.prompt_kwargs["issue_history"]

    def test_double_on_enter_is_idempotent(self, make_ctx: Any, make_work_item: Any) -> None:
        """A literal double on_enter produces no extra mutations or moves."""
        stage = PlanningStage()
        github = FakeStageGitHub(has_plan=True)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=10, state="ENTER")

        assert stage.on_enter(item, ctx) is None
        assert item.state == "VERIFY"
        log_after_first = list(github.mutation_log)
        assert log_after_first == [("gh_issue_add_labels", (10, (STATE_NEEDS_PLAN,)))]

        assert stage.on_enter(item, ctx) is None  # second literal call

        assert item.state == "VERIFY"
        assert github.mutation_log == log_after_first  # nothing new written

    def test_replan_entry_swaps_no_go_for_needs_plan_atomically(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A state:plan-no-go fail-back entry swaps to needs-plan in ONE write."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO])
        ctx = make_ctx(github=github)
        item = make_work_item(issue=20, state="ENTER")

        outcome = stage.on_enter(item, ctx)

        assert outcome is None  # proceed to re-plan, not fast-forward
        assert item.state == "ENTER"  # no premature VERIFY fast-forward
        # Exactly one atomic edit — invariant never transiently broken.
        assert github.mutation_log == [
            (
                "edit_labels",
                (20, (STATE_NEEDS_PLAN,), (STATE_PLAN_NO_GO, STATE_PLAN_GO, "state:plan-blocked")),
            ),
        ]
        assert STATE_PLAN_NO_GO not in github.labels[20]
        assert STATE_PLAN_GO not in github.labels[20]
        assert STATE_NEEDS_PLAN in github.labels[20]

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
        assert github.mutation_log == [
            (
                "edit_labels",
                (23, (STATE_NEEDS_PLAN,), (STATE_PLAN_NO_GO, STATE_PLAN_GO, "state:plan-blocked")),
            ),
        ]

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

    def test_restart_after_label_swap_still_recognizes_replan(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The canonical NOGO recovers replan intent after the label swap commits."""
        stage = PlanningStage()
        github = FakeStageGitHub(labels=[STATE_NEEDS_PLAN])
        github.comments[25] = [
            render_current_plan("Plan v1", revision=1),
            render_current_review("Missing rollback.\n\nstate:plan-no-go", revision=1),
        ]
        ctx = make_ctx(github=github)
        item = make_work_item(issue=25, state="ENTER")

        assert stage.on_enter(item, ctx) is None

        assert item.payload["requires_plan_revision"] is True
        assert item.state == "ENTER"
        assert github.mutation_log == [
            (
                "edit_labels",
                (25, (STATE_NEEDS_PLAN,), (STATE_PLAN_NO_GO, STATE_PLAN_GO, STATE_PLAN_BLOCKED)),
            )
        ]

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
        assert isinstance(result.job, AgentJob)  # narrow the job union
        assert result.on_done_state == "PLAN_WAIT"
        assert result.job.descr == "advise"
        assert result.job.prompt_kwargs["issue_number"] == 3

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
        github = FakeStageGitHub(has_plan=True)
        ctx = make_ctx(github=github)
        item = make_work_item(issue=5, state="VERIFY")
        item.payload["plan_text"] = "# Implementation Plan\n\nAlready posted."

        result = stage.step(item, ctx)

        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.ADVANCE
        assert github.mutation_log == []  # existing plan: no duplicate upsert

    def test_verify_posts_plan_comment_then_advances(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The PIPELINE posts the plan comment (M1).

        VERIFY upserts the durable artifact BEFORE the verify/ADVANCE
        decision (journal order).
        """
        stage = PlanningStage()
        github = FakeStageGitHub(has_plan=False)
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

        class StaleNoGoGitHub(FakeStageGitHub):
            def has_existing_plan(self, issue_number: int) -> bool:
                return False

        stage = PlanningStage()
        github = StaleNoGoGitHub(has_plan=False)
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

    def test_replan_archives_pair_before_updating_both_canonical_comments(
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
        assert "<!-- revision: 2 -->" in comments[0]
        assert "Review pending for implementation plan revision 2" in comments[1]
        assert comments[2].startswith("<!-- hephaestus-plan-history:revision=1:kind=plan -->")
        assert comments[3].startswith("<!-- hephaestus-plan-history:revision=1:kind=review -->")
        assert [entry[0] for entry in github.mutation_log] == [
            "edit_labels",
            "append_issue_comment",
            "append_issue_comment",
            "gh_issue_upsert_comment",
            "gh_issue_upsert_comment",
        ]

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
        assert github.mutation_log[-1] == (
            "edit_labels",
            (28, (STATE_PLAN_BLOCKED,), (STATE_NEEDS_PLAN, STATE_PLAN_NO_GO, STATE_PLAN_GO)),
        )

    def test_verify_posts_exactly_once_on_reentry(self, make_ctx: Any, make_work_item: Any) -> None:
        """Re-entering VERIFY never double-posts.

        The upsert is guarded by has_existing_plan (idempotent on re-entry).
        """
        stage = PlanningStage()
        github = FakeStageGitHub(has_plan=False)
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

    def test_normalize_plan_comment_docstring_distinguishes_marker_from_advance_gate(
        self,
    ) -> None:
        """Docstring identifies the opaque ownership marker."""
        doc = _normalize_plan_comment.__doc__ or ""

        assert "opaque ownership marker" in doc

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
        result = JobResult(ok=True, value="advise findings here")

        stage.on_job_done(item, result, ctx)

        assert item.payload["advise_findings"] == "advise findings here"

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
                pool.submit(result.job, result.on_done_state)  # type: ignore[arg-type]
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
        assert github.mutation_log == [
            ("gh_issue_add_labels", (40, (STATE_NEEDS_PLAN,))),
            ("gh_issue_upsert_comment", (40, PLAN_CANONICAL_MARKER)),
            ("gh_issue_upsert_comment", (40, PLAN_REVIEW_CANONICAL_MARKER)),
        ]
        assert PLAN_COMMENT_MARKER in github.comments[40][0]
        assert github.comments[40][0].endswith("Steps.")
