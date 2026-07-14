"""Strict-review stage: the single automatic authority for implementation-go.

Ninth queue stage (issue #2055), inserted between ``pr_review`` and ``ci``
(``docs/AUTOMATION_LOOP_ARCHITECTURE.md`` "strict_review" section is the
binding contract). This stage is the ONLY automatic producer of
``state:implementation-go``; it never arms auto-merge itself — arming stays
the sole responsibility of ``MergeWaitStage``, which re-validates a
head-bound artifact this stage publishes before it ever arms.

- States: ENTER -> HEAD_CHECK -> REVIEW_WAIT -> EVAL -> SR_FINISH.
  Budget: ``strict_review_iter`` = 1 (a single independent pass; a NOGO
  routes straight to ``implementation``, never loops in-stage).
- ``on_enter``: refresh labels; if a prior pass's captured head
  (``payload["strict_review_head"]``) no longer matches the PR's live head,
  clear the stale payload so a fresh pass restarts (revocation semantics —
  see ``_revoke_on_head_change``).
- HEAD_CHECK [M]: capture ``gh_pr_state(pr)["headRefOid"]`` into
  ``payload["strict_review_head"]`` — the exact value baked into both the
  session key (:func:`~hephaestus.automation.agent_config.strict_review_agent`)
  and the published artifact, so the artifact and the session are both
  provably bound to one commit.
- REVIEW_WAIT [W:A]: submit a READ-ONLY agent job
  (``AgentJob(sandbox="read-only", ...)``) — never write/GitHub-mutation
  capable — using a fresh per-head/per-attempt session
  (``strict_review_agent``), so a rejected artifact's retry or a new head
  never resumes a stale transcript. Prompt composed in-worker by
  :func:`~hephaestus.automation.prompts.strict_review_gate.build_strict_review_prompt`.
- EVAL [M]:
  - GO: durably ``publish_strict_review_artifact`` (artifact BEFORE label —
    doc contract) then ``mark_pr_implementation_go``; ADVANCE to ``ci``.
    Never arms — that stays ``merge_wait``'s exclusive job.
  - NOGO: idempotently ``defer_auto_merge``, readback-verify it actually
    disabled, post fenced remediation feedback, ``mark_pr_implementation_no_go``,
    FAIL_BACK(``nogo``) to ``implementation`` (a real implementation job, not
    a review-only shortcut).
  - Missing/ERROR verdict: RETRY (bounded by the ``strict_review_iter``
    budget the coordinator enforces via ROUTES; no round is charged here —
    the coordinator's budget accounting owns that).
- Head-change revocation (``on_enter``): reusing merge_wait's "no waiting
  out a state that can never resolve" philosophy — a head that moved since
  the artifact/session was captured means the OLD review no longer proves
  anything about the NEW head. The stage clears the GO label, verifies
  auto-merge disabled, and restarts HEAD_CHECK for the new head.
"""

from __future__ import annotations

import logging

from hephaestus.automation.agent_config import pr_reviewer_claude_timeout, reviewer_model
from hephaestus.automation.claude_invoke import parse_review_verdict
from hephaestus.automation.prompts.strict_review_gate import build_strict_review_prompt
from hephaestus.automation.session_naming import strict_review_agent
from hephaestus.automation.state_labels import STATE_IMPLEMENTATION_GO

from .base import (
    AgentJob,
    Continue,
    Disposition,
    JobRequest,
    JobResult,
    Stage,
    StageContext,
    StageOutcome,
    StepResult,
    WorkItem,
    _terminal_pr_outcome,
    _worktree_path,
    agent_provider,
    stage_model,
)

logger = logging.getLogger(__name__)

# In-memory mini-states (stage-local strings, never GitHub labels).
ENTER = "ENTER"
HEAD_CHECK = "HEAD_CHECK"
REVIEW_WAIT = "REVIEW_WAIT"
EVAL = "EVAL"
SR_FINISH = "SR_FINISH"
FINISH = SR_FINISH

#: HTML-comment marker for the NOGO remediation feedback comment.
STRICT_REVIEW_NOGO_MARKER = "<!-- hephaestus-strict-review-nogo -->"


def _issue_number(item: WorkItem) -> int:
    """Return the issue number after the stage-level guard has run."""
    if item.issue is None:
        raise RuntimeError("strict_review stage reached without an issue number")
    return item.issue


class StrictReviewStage(Stage):
    """Stage: independent read-only GO/NOGO gate; sole implementation-go authority."""

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Fast-forward init and head-change revocation.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            None (always proceed to step()).

        """
        if not item.state:
            item.state = ENTER
        if item.pr is not None:
            self._revoke_on_head_change(item, ctx)
        return None

    def _revoke_on_head_change(self, item: WorkItem, ctx: StageContext) -> None:
        """Clear stale artifact/session state when the PR head has moved.

        A captured head from a prior pass (``payload["strict_review_head"]``)
        that no longer matches the PR's live head means neither the prior
        session nor its artifact prove anything about the current commit:
        durably clear the GO label, verify auto-merge disabled, and restart
        at ``ENTER`` so HEAD_CHECK captures the new head fresh.
        """
        prior_head = item.payload.get("strict_review_head")
        if not prior_head:
            return
        pr_state = ctx.github.gh_pr_state(item.pr)  # type: ignore[arg-type]
        live_head = str((pr_state or {}).get("headRefOid") or "")
        if not live_head or live_head == prior_head:
            return
        logger.info(
            "strict_review:%s: PR #%d head changed (%s -> %s); revoking prior pass",
            item.issue,
            item.pr,
            prior_head,
            live_head,
        )
        item.payload.pop("strict_review_head", None)
        item.payload.pop("strict_review_attempt", None)
        self._clear_go_and_verify_disabled(item.pr, ctx)  # type: ignore[arg-type]
        item.state = ENTER

    def _clear_go_and_verify_disabled(self, pr_number: int, ctx: StageContext) -> None:
        """Durably clear the GO label and verify auto-merge is actually disabled."""
        try:
            ctx.github.remove_labels(pr_number, [STATE_IMPLEMENTATION_GO])
        except Exception as e:
            logger.warning(
                "strict_review: failed to clear implementation-go on PR #%d (non-fatal): %s",
                pr_number,
                e,
            )
        try:
            ctx.github.defer_auto_merge(pr_number)
        except Exception as e:
            logger.warning(
                "strict_review: failed to defer auto-merge on PR #%d (non-fatal): %s",
                pr_number,
                e,
            )
        self._verify_auto_merge_disabled(pr_number, ctx)

    def _verify_auto_merge_disabled(self, pr_number: int, ctx: StageContext) -> None:
        """Readback-verify auto-merge is disabled; log loudly if it is not."""
        confirmed = ctx.github.gh_pr_state(pr_number)
        if confirmed is not None and confirmed.get("autoMergeRequest"):
            logger.error(
                "strict_review: PR #%d still shows autoMergeRequest after "
                "defer_auto_merge; auto-merge may still be armed",
                pr_number,
            )

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Execute the next strict-review action for the item's current state.

        Args:
            item: The work item with current state.
            ctx: Stage context.

        Returns:
            Continue, JobRequest, or StageOutcome.

        """
        if not item.issue:
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
        if item.pr is None:
            logger.warning("strict_review:%d: no PR on item; failing back", item.issue)
            return StageOutcome(Disposition.FAIL_BACK, "nogo")

        if item.state == ENTER:
            return Continue(next_state=HEAD_CHECK)
        if item.state == HEAD_CHECK:
            return self._head_check(item, ctx)
        if item.state == REVIEW_WAIT:
            return self._review_wait(item, ctx)
        if item.state == EVAL:
            return self._eval(item, ctx)
        if item.state == SR_FINISH:
            return StageOutcome(Disposition.ADVANCE, "strict review GO")
        logger.warning("strict_review:%s: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    def _head_check(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """HEAD_CHECK [M]: capture the PR's live head SHA before dispatching review."""
        terminal = _terminal_pr_outcome(ctx.github.gh_pr_state(item.pr), item.pr)  # type: ignore[arg-type]
        if terminal is not None:
            return terminal
        pr_state = ctx.github.gh_pr_state(item.pr)  # type: ignore[arg-type]
        head_sha = str((pr_state or {}).get("headRefOid") or "")
        if not head_sha:
            logger.warning(
                "strict_review:%s: could not read PR #%d head SHA; retrying",
                item.issue,
                item.pr,
            )
            item.payload["retry_delay_s"] = 30
            return StageOutcome(Disposition.RETRY, "no_head_sha")
        item.payload["strict_review_head"] = head_sha
        return Continue(next_state=REVIEW_WAIT)

    def _review_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """REVIEW_WAIT [W:A]: submit the read-only, per-head/per-attempt review job."""
        issue = _issue_number(item)
        head_sha = str(item.payload.get("strict_review_head") or "")
        if not head_sha:
            # Guarded by HEAD_CHECK; restart-safety fallback.
            return Continue(next_state=HEAD_CHECK)
        attempt = item.payload.get("strict_review_attempt", 0)
        item.payload["strict_review_attempt"] = attempt + 1
        item.payload.pop("strict_review_verdict", None)
        item.payload.pop("strict_review_text", None)
        item.payload.pop("strict_review_failed", None)
        logger.info(
            "strict_review:%d: requesting read-only review job (head %s, attempt %d, PR #%d)",
            issue,
            head_sha[:12],
            attempt,
            item.pr,
        )
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "reviewer", reviewer_model),
            prompt_builder=build_strict_review_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=pr_reviewer_claude_timeout(),
            session_agent=strict_review_agent(head_sha, attempt),
            sandbox="read-only",
            # Diff / CI status / prior verdict are seeded into item.payload
            # by the coordinator, which owns the gh reads (mirrors pr_review).
            prompt_kwargs={
                "pr_number": item.pr,
                "issue_number": item.issue,
                "head_sha": head_sha,
                "diff": item.payload.get("pr_diff", ""),
                "ci_status": item.payload.get("ci_status", ""),
                "prior_pr_review_verdict": item.payload.get("prior_pr_review_verdict", ""),
            },
            parse=parse_review_verdict,
            descr="strict_review",
        )
        return JobRequest(job, on_done_state=EVAL)

    def _eval(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """EVAL [M]: GO publishes artifact + label (never arms); NOGO remediates."""
        if item.payload.pop("strict_review_failed", None):
            logger.warning(
                "strict_review:%s: review job failed; failing back to implementation",
                item.issue,
            )
            return StageOutcome(Disposition.FAIL_BACK, "nogo")
        verdict = item.payload.get("strict_review_verdict")
        verdict_text = str(item.payload.get("strict_review_text") or "")
        head_sha = str(item.payload.get("strict_review_head") or "")
        if verdict not in ("GO", "NOGO"):
            logger.warning(
                "strict_review:%s: missing/ambiguous verdict (%r); failing back",
                item.issue,
                verdict,
            )
            return StageOutcome(Disposition.FAIL_BACK, "nogo")
        if verdict == "GO":
            return self._handle_go(item, ctx, head_sha, verdict_text)
        return self._handle_nogo(item, ctx, verdict_text)

    def _handle_go(
        self, item: WorkItem, ctx: StageContext, head_sha: str, verdict_text: str
    ) -> StepResult:
        """GO: publish the artifact BEFORE the label (doc contract), never arm."""
        pr_number = item.pr
        if pr_number is None:  # guarded by step(); narrowing
            return StageOutcome(Disposition.FAIL_BACK, "nogo")
        logger.info(
            "strict_review:%s: GO for PR #%d at head %s; publishing artifact",
            item.issue,
            pr_number,
            head_sha[:12],
        )
        try:
            ctx.github.publish_strict_review_artifact(pr_number, head_sha, verdict_text, is_go=True)
        except Exception as e:
            logger.warning(
                "strict_review: failed to publish GO artifact on PR #%d (non-fatal, "
                "state:implementation-go NOT applied): %s",
                pr_number,
                e,
            )
            return Continue(next_state=SR_FINISH)
        try:
            ctx.github.mark_pr_implementation_go(pr_number)
        except Exception as e:
            logger.warning(
                "strict_review: failed to mark PR #%d implementation-go (non-fatal): %s",
                pr_number,
                e,
            )
        return Continue(next_state=SR_FINISH)

    def _handle_nogo(self, item: WorkItem, ctx: StageContext, verdict_text: str) -> StepResult:
        """NOGO: disable+verify auto-merge, post fenced remediation, route to implementation."""
        pr_number = item.pr
        if pr_number is None:  # guarded by step(); narrowing
            return StageOutcome(Disposition.FAIL_BACK, "nogo")
        logger.info(
            "strict_review:%s: NOGO for PR #%d; disabling auto-merge and remediating",
            item.issue,
            pr_number,
        )
        try:
            ctx.github.defer_auto_merge(pr_number)
        except Exception as e:
            logger.warning(
                "strict_review: failed to defer auto-merge on PR #%d (non-fatal): %s",
                pr_number,
                e,
            )
        self._verify_auto_merge_disabled(pr_number, ctx)
        self._post_nogo_remediation(pr_number, ctx, verdict_text)
        try:
            ctx.github.mark_pr_implementation_no_go(pr_number)
        except Exception as e:
            logger.warning(
                "strict_review: failed to mark PR #%d implementation-no-go (non-fatal): %s",
                pr_number,
                e,
            )
        return StageOutcome(Disposition.FAIL_BACK, "nogo")

    @staticmethod
    def _post_nogo_remediation(pr_number: int, ctx: StageContext, verdict_text: str) -> None:
        """Durably post the NOGO verdict as fenced remediation feedback.

        Fenced (not raw-injected) since ``verdict_text`` is agent output
        that may itself echo untrusted diff/PR-body content back — the
        remediation comment must not become a second injection vector.
        """
        body = (
            f"{STRICT_REVIEW_NOGO_MARKER}\n"
            "Independent strict-review result: **NOGO**. Auto-merge has been "
            "disabled and this PR is routed back to implementation.\n\n"
            "BEGIN_STRICT_REVIEW_VERDICT\n"
            f"{verdict_text}\n"
            "END_STRICT_REVIEW_VERDICT"
        )
        try:
            ctx.github.post_pr_comment(pr_number, body)
        except Exception as e:
            logger.warning(
                "strict_review: failed to post NOGO remediation comment on PR #%d (non-fatal): %s",
                pr_number,
                e,
            )

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Store the parsed verdict on the item payload (state still REVIEW_WAIT).

        Args:
            item: The work item whose job completed.
            result: The job result from the worker pool.
            ctx: Stage context.

        """
        if item.state != REVIEW_WAIT:
            return
        if not result.ok:
            logger.warning("strict_review:%s: review job failed: %s", item.issue, result.error)
            item.payload["strict_review_failed"] = True
            return
        verdict_obj = result.value
        verdict = getattr(verdict_obj, "verdict", None)
        raw = getattr(verdict_obj, "raw", None)
        if verdict not in ("GO", "NOGO"):
            item.payload["strict_review_verdict"] = None
        else:
            item.payload["strict_review_verdict"] = verdict
        item.payload["strict_review_text"] = str(raw) if raw is not None else ""
