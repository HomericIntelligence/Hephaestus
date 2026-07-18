"""In-loop ``$athena:pr-review`` stage.

The stage sits between ``pr_review`` and ``merge_wait``. It invokes the Athena
skill in a clean, read-only Codex worktree and, after a second current-head
read, applies ``state:implementation-go`` itself. ``merge_wait`` owns the sole
auto-merge arm.

- States: ENTER -> HEAD_CHECK -> WORKTREE_WAIT -> REVIEW_WAIT -> EVAL ->
  SR_FINISH.
  Budget: ``strict_review_iter`` = 1 (a single independent pass; a NOGO
  routes straight to ``implementation``, never loops in-stage).
- ``on_enter``: refresh labels; if a prior pass's captured head
  (``payload["strict_review_head"]``) no longer matches the PR's live head,
  clear the stale payload so a fresh pass restarts (revocation semantics —
  see ``_revoke_on_head_change``).
- HEAD_CHECK [M]: capture ``gh_pr_state(pr)["headRefOid"]`` into
  ``payload["strict_review_head"]`` — the exact value baked into the session
  key (:func:`~hephaestus.automation.agent_config.strict_review_agent`). A
  synced isolated worktree and worker-side ``git rev-parse HEAD`` check must
  match this remote SHA before dispatch.
- REVIEW_WAIT [W:A]: submit a READ-ONLY agent job
  (``AgentJob(sandbox="read-only", ...)``) — never write/GitHub-mutation
  capable — using a fresh per-head/per-attempt session
  (``strict_review_agent``), so a retry or a new head never resumes a stale
  transcript. Prompt composed in-worker by
  :func:`~hephaestus.automation.prompts.strict_review_gate.build_strict_review_prompt`.
- EVAL [M]:
  - GO: applies the loop-owned label only after the reviewed head is read back
    as current, then discards the review-local head before advancing to
    ``merge_wait``.
  - NOGO: idempotently ``defer_auto_merge``, readback-verify it actually
    disabled, post fenced remediation feedback, ``mark_pr_implementation_no_go``,
    FAIL_BACK(``nogo``) to ``implementation`` (a real implementation job, not
    a review-only shortcut).
  - Missing/ERROR verdict: FAIL_BACK(``nogo``) to implementation.  A
    reviewer result that cannot be trusted must never leave the current head
    eligible for an automatic merge.
- Head-change revocation (``on_enter``): a head that moved since the session
  was captured means the old review is no longer applicable. The stage clears
  the label, verifies auto-merge disabled, and restarts HEAD_CHECK for the
  new head.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from hephaestus.automation.agent_config import pr_reviewer_claude_timeout, reviewer_model
from hephaestus.automation.claude_invoke import ReviewVerdict
from hephaestus.automation.prompts.strict_review_gate import build_strict_review_prompt
from hephaestus.automation.session_naming import strict_review_agent
from hephaestus.automation.state_labels import STATE_IMPLEMENTATION_GO, STATE_IMPLEMENTATION_NO_GO

from .base import (
    GIT_JOB_TIMEOUT_S,
    AgentJob,
    Continue,
    Disposition,
    GitJob,
    JobRequest,
    JobResult,
    Stage,
    StageContext,
    StageOutcome,
    StepResult,
    WorkItem,
    _terminal_pr_outcome,
    stage_model,
)

logger = logging.getLogger(__name__)

# In-memory mini-states (stage-local strings, never GitHub labels).
ENTER = "ENTER"
HEAD_CHECK = "HEAD_CHECK"
WORKTREE_WAIT = "WORKTREE_WAIT"
REVIEW_WAIT = "REVIEW_WAIT"
EVAL = "EVAL"
SR_FINISH = "SR_FINISH"
FINISH = SR_FINISH

# Ownership can be absent only in a broken entrypoint configuration.  Keep
# that distinct from a contended, healthy guard so the former fails closed
# instead of silently running a second independent review.
_OWNERSHIP_CLAIMED = "claimed"
_OWNERSHIP_BUSY = "busy"
_OWNERSHIP_UNAVAILABLE = "unavailable"

# A successful strict review passes only ordinary issue context, its worktree
# cleanup path, and the in-process mutex to the next stage.  This is a closed
# allowlist rather than a review-key denylist: an unknown or renamed review
# field must never become a second durable merge authorization.  Additions
# require an explicit review of their authorization consequences.
_GO_CONTEXT_PAYLOAD_KEYS = frozenset(
    {
        "_fail_backs",
        "advise_findings",
        "direct_pr_worktree",
        "entry_reason",
        "entry_stage",
        "existing_pr",
        "issue_body",
        "issue_title",
        "marketplace_path",
    }
)
_GO_HANDOFF_PAYLOAD_KEYS = _GO_CONTEXT_PAYLOAD_KEYS | {
    "_strict_review_guard_owner",
    "strict_review_worktree",
}

#: HTML-comment marker for the NOGO remediation feedback comment.
STRICT_REVIEW_NOGO_MARKER = "<!-- hephaestus-strict-review-nogo -->"

# Unlike the general review-loop parser, this loop-authorization stage
# accepts only the final, exact handoff emitted after the complete Athena
# skill report. A diff or prior review may legitimately contain a quoted
# ``Verdict: GO`` line; taking the first such line would let untrusted evidence
# choose the gate result.
_STRICT_FINAL_VERDICT_RE = re.compile(r"(?m)^Automation-loop handoff: (GO|NOGO)[ \t]*\n?\Z")


def parse_strict_review_verdict(text: str) -> ReviewVerdict:
    """Parse only the final exact Athena review handoff.

    The generic review parser intentionally supports legacy variants and
    therefore finds the first matching line.  This gate has a stronger
    authorization contract: the final line must be the exact GO/NOGO handoff
    requested by its prompt. Anything else is ambiguous and is handled as a
    fail-closed NOGO by :meth:`StrictReviewStage._eval`.
    """
    match = _STRICT_FINAL_VERDICT_RE.search(text)
    if match is None:
        return ReviewVerdict(grade=None, verdict="AMBIGUOUS", raw=text)
    return ReviewVerdict(grade=None, verdict=match.group(1), raw=text)


def _issue_number(item: WorkItem) -> int:
    """Return a stable issue id; orphan PRs use zero for review-session scope."""
    return item.issue if item.issue is not None else 0


class StrictReviewStage(Stage):
    """Stage: independent, read-only `$athena:pr-review` handoff."""

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Fast-forward init and head-change revocation.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            ``None`` to proceed, or a terminal containment failure after a
            head change that could not be safely disarmed.

        """
        if not item.state:
            item.state = ENTER
        if item.issue is None:
            # Requirements must originate from a real linked issue, never
            # from PR-authored content.  Reject an orphan before claiming the
            # gate, mutating labels, or dispatching review work.
            return StageOutcome(Disposition.FINISH_FAIL, "strict_review_orphan")
        if "_strict_review_guard_owner" not in item.payload:
            item.payload.pop("_strict_review_entry_contained", None)
        if item.pr is not None:
            return self._claim_and_contain_entry(item, ctx)
        return None

    def _claim_and_contain_entry(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Claim the PR before its ingress containment writes."""
        ownership = self._claim_review_ownership(item, ctx)
        if ownership == _OWNERSHIP_UNAVAILABLE:
            return StageOutcome(Disposition.FINISH_FAIL, "strict_review_guard_unavailable")
        if ownership != _OWNERSHIP_CLAIMED:
            item.payload["retry_delay_s"] = 1
            return StageOutcome(Disposition.RETRY, "strict_review_busy")
        outcome = self._contain_and_revoke_on_entry(item, ctx)
        if outcome is None:
            item.payload["_strict_review_entry_contained"] = True
        return outcome

    def _contain_and_revoke_on_entry(
        self, item: WorkItem, ctx: StageContext
    ) -> StageOutcome | None:
        """Disarm every strict-review ingress and revoke stale head state.

        A strict-review job can take minutes.  Before submitting one, revoke
        any earlier eligibility and verify GitHub has actually disarmed
        auto-merge.  This covers direct/reseeded ingress as well as a saved
        head that has moved.  Otherwise a legacy or parallel arm could merge
        while the independent reviewer is still running.
        """
        pr_number = item.pr
        if pr_number is None:  # guarded by on_enter; narrowing
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        # An ingress can arrive from a restart with GitHub already holding a
        # previous auto-merge arm.  Disarm it before *any* label mutation or
        # state read: label removal alone does not prevent that arm from
        # merging while this independent review is being scheduled.
        pr_state = self._defer_and_read_disabled(pr_number, ctx)
        if pr_state is None:
            return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_disable_failed")
        terminal = _terminal_pr_outcome(pr_state, pr_number)
        if terminal is not None:
            return terminal
        label_revoked, pr_state = self._revoke_go_and_recontain(pr_number, ctx)
        # Label removal is a separate remote mutation.  A parallel actor can
        # re-arm the same head during that call, so repeat disable+readback
        # before a reviewer is ever allowed to inspect it.
        if pr_state is None:
            return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_disable_failed")
        terminal = _terminal_pr_outcome(pr_state, pr_number)
        if terminal is not None:
            return terminal
        if not label_revoked:
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_revoke_failed")
        live_head = str((pr_state or {}).get("headRefOid") or "")
        if not live_head:
            return StageOutcome(Disposition.FINISH_FAIL, "no_head_sha")
        prior_head = item.payload.get("strict_review_head")
        if prior_head and live_head != prior_head:
            logger.info(
                "strict_review:%s: PR #%d head changed (%s -> %s); revoking prior pass",
                item.issue,
                pr_number,
                prior_head,
                live_head,
            )
            item.payload.pop("strict_review_head", None)
            item.payload.pop("strict_review_attempt", None)
            item.payload.pop("strict_review_verdict", None)
            item.payload.pop("strict_review_text", None)
            item.payload.pop("strict_review_failed", None)
            item.payload.pop("strict_review_worktree_head", None)
            item.state = ENTER
        return None

    def _clear_go_and_verify_disabled(self, pr_number: int, ctx: StageContext) -> bool:
        """Disarm/read back first, then remove the obsolete GO label."""
        if self._defer_and_read_disabled(pr_number, ctx) is None:
            return False
        label_revoked, final_state = self._revoke_go_and_recontain(pr_number, ctx)
        return label_revoked and final_state is not None

    def _revoke_go_and_recontain(
        self, pr_number: int, ctx: StageContext
    ) -> tuple[bool, dict[str, Any] | None]:
        """Remove GO, then always re-disable and read back the remote arm.

        A failed label mutation is ambiguous: GitHub may have applied it even
        though the client lost the response.  Therefore the final containment
        runs regardless and callers distinguish a verified label error from
        an inability to establish a safely disarmed state.
        """
        label_revoked = self._remove_go_label(pr_number, ctx)
        return label_revoked, self._defer_and_read_disabled(pr_number, ctx)

    @staticmethod
    def _remove_go_label(pr_number: int, ctx: StageContext) -> bool:
        """Remove a stale label only after the PR is confirmed disarmed."""
        try:
            ctx.github.remove_labels(pr_number, [STATE_IMPLEMENTATION_GO])
        except Exception as e:
            logger.warning(
                "strict_review: failed to clear implementation-go on PR #%d: %s",
                pr_number,
                e,
            )
            # Deferral is already read-back confirmed before this call; keep
            # the failure explicit rather than letting an unlabeled PR drift.
            return False
        return True

    def _defer_and_read_disabled(self, pr_number: int, ctx: StageContext) -> dict[str, Any] | None:
        """Disable auto-merge and return its readback only when unarmed."""
        try:
            ctx.github.defer_auto_merge(pr_number)
        except Exception as e:
            logger.warning(
                "strict_review: failed to defer auto-merge on PR #%d: %s",
                pr_number,
                e,
            )
            return None
        confirmed = ctx.github.gh_pr_state(pr_number)
        if confirmed is None or confirmed.get("autoMergeRequest"):
            logger.error(
                "strict_review: PR #%d could not verify auto-merge disabled after defer",
                pr_number,
            )
            return None
        return confirmed

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:  # noqa: C901
        """Execute the next strict-review action for the item's current state.

        Args:
            item: The work item with current state.
            ctx: Stage context.

        Returns:
            Continue, JobRequest, or StageOutcome.

        """
        if item.pr is None:
            logger.warning("strict_review:%d: no PR on item; failing back", item.issue)
            return StageOutcome(Disposition.FAIL_BACK, "nogo")
        if item.issue is None:
            # The strict gate must judge the diff against an issue's concrete
            # requirements.  A PR-only item cannot meet that contract, and a
            # NOGO cannot safely enter the issue-bound implementation stage.
            return StageOutcome(Disposition.FINISH_FAIL, "strict_review_orphan")

        ownership = self._claim_review_ownership(item, ctx)
        if ownership == _OWNERSHIP_UNAVAILABLE:
            return StageOutcome(Disposition.FINISH_FAIL, "strict_review_guard_unavailable")
        if ownership != _OWNERSHIP_CLAIMED:
            item.payload["retry_delay_s"] = 1
            return StageOutcome(Disposition.RETRY, "strict_review_busy")
        if item.state == ENTER and not item.payload.get("_strict_review_entry_contained"):
            outcome = self._contain_and_revoke_on_entry(item, ctx)
            if outcome is not None:
                return outcome
            item.payload["_strict_review_entry_contained"] = True

        if item.state == ENTER:
            return Continue(next_state=HEAD_CHECK)
        if item.state == HEAD_CHECK:
            return self._head_check(item, ctx)
        if item.state == WORKTREE_WAIT:
            if item.payload.pop("strict_review_worktree_failed", None):
                return StageOutcome(Disposition.FINISH_FAIL, "strict_review_worktree_failed")
            if not item.payload.get("strict_review_worktree"):
                # A restored item cannot safely assume an unrecorded worker
                # completion created a usable checkout.  Fail closed instead
                # of sending the reviewer to the shared repository root.
                return StageOutcome(Disposition.FINISH_FAIL, "strict_review_worktree_unfinished")
            return Continue(next_state=HEAD_CHECK)
        if item.state == REVIEW_WAIT:
            return self._review_wait(item, ctx)
        if item.state == EVAL:
            return self._eval(item, ctx)
        if item.state == SR_FINISH:
            return StageOutcome(Disposition.ADVANCE, "strict review GO")
        logger.warning("strict_review:%s: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    @staticmethod
    def _claim_review_ownership(item: WorkItem, ctx: StageContext) -> str:
        """Claim this PR's loop-owned review slot before any gate action."""
        guard = getattr(ctx.config, "strict_review_guard", None)
        if guard is None:
            logger.error(
                "strict_review:%s: no ownership guard is configured; failing closed",
                item.issue,
            )
            return _OWNERSHIP_UNAVAILABLE
        pr_number = item.pr
        if pr_number is None:  # guarded by step(); narrowing
            return _OWNERSHIP_UNAVAILABLE
        owner = id(item)
        try:
            claimed = bool(guard.try_claim(ctx.org, item.repo, pr_number, owner))
        except Exception as exc:
            logger.warning(
                "strict_review:%s: unable to acquire PR #%d review ownership: %s",
                item.issue,
                pr_number,
                exc,
            )
            return _OWNERSHIP_BUSY
        if claimed:
            item.payload["_strict_review_guard_owner"] = owner
            return _OWNERSHIP_CLAIMED
        return _OWNERSHIP_BUSY

    def _head_check(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """HEAD_CHECK [M]: capture the PR's live head SHA before dispatching review."""
        if item.payload.pop("strict_review_worktree_failed", None):
            return StageOutcome(Disposition.FINISH_FAIL, "strict_review_worktree_failed")
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
        # A reviewer must never inspect a local checkout whose commit has not
        # been synchronized to this exact remote head. This is required for a
        # restored/direct ingress as well as a normal push during review.
        review_worktree = str(item.payload.get("strict_review_worktree") or "")
        synced_worktree_head = str(item.payload.get("strict_review_worktree_head") or "")
        if not review_worktree or synced_worktree_head.lower() != head_sha.lower():
            branch = item.branch or ctx.github.get_pr_head_branch(item.pr)  # type: ignore[arg-type]
            if not branch:
                logger.warning(
                    "strict_review:%s: could not resolve PR #%d head branch",
                    item.issue,
                    item.pr,
                )
                item.payload["retry_delay_s"] = 30
                return StageOutcome(Disposition.RETRY, "no_head_branch")
            item.branch = branch
            # A direct PR/drive-green entry has no implementation worktree.
            # Create and sync an isolated checkout before a read-only reviewer
            # can inspect files, so its local Read/Glob context is this exact
            # remote PR branch rather than the shared checkout's base branch.
            worktree_job = GitJob(
                repo=item.repo,
                op="create_worktree",
                timeout_s=GIT_JOB_TIMEOUT_S,
                kwargs={
                    # The detached path is keyed by PR, not issue: more than
                    # one PR can refer to an issue and their reviewers may run
                    # concurrently under different ownership claims.
                    "issue_number": item.pr,
                    "branch_name": branch,
                    "refresh_base": False,
                    "isolated": True,
                    "isolated_name": f"strict-review-pr-{item.pr}",
                    "sync_to_remote": True,
                    "pr_number": item.pr,
                    "repo_root": str(ctx.paths.repo_root),
                },
                descr="strict_review_worktree",
            )
            # The coordinator invokes ``on_job_done`` before applying the
            # handle's target state.  Record an explicit pending marker so
            # the callback can identify this completion while the item is
            # still in HEAD_CHECK, then transition through WORKTREE_WAIT.
            item.payload["strict_review_worktree_pending"] = True
            return JobRequest(worktree_job, on_done_state=WORKTREE_WAIT)
        return Continue(next_state=REVIEW_WAIT)

    def _review_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """REVIEW_WAIT [W:A]: submit the read-only, per-head/per-attempt review job."""
        issue = _issue_number(item)
        head_sha = str(item.payload.get("strict_review_head") or "")
        if not head_sha:
            # Guarded by HEAD_CHECK; restart-safety fallback.
            return Continue(next_state=HEAD_CHECK)
        review_worktree = str(item.payload.get("strict_review_worktree") or "")
        if not review_worktree:
            # The review agent may never fall back to the writer or shared
            # checkout. Recreate its detached checkout instead.
            return Continue(next_state=HEAD_CHECK)
        evidence = ctx.github.strict_review_evidence(item.pr, head_sha, item.issue)  # type: ignore[arg-type]
        if evidence is None or evidence.head_sha.lower() != head_sha.lower():
            logger.warning(
                "strict_review:%s: current-head evidence unavailable for PR #%d; invalidating",
                item.issue,
                item.pr,
            )
            return self._handle_nogo(
                item,
                ctx,
                head_sha,
                "PR-review context could not be fetched for this head.\nGrade: F\nVerdict: NOGO",
            )
        attempt = int(item.payload.get("strict_review_attempt", 0))
        if attempt >= ctx.budget("strict_review_iter"):
            return self._handle_nogo(
                item,
                ctx,
                head_sha,
                "Strict-review attempt budget exhausted.\nGrade: F\nVerdict: NOGO",
            )
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
        # Athena's review skill needs a shell to invoke its local helpers.
        # Claude's non-interactive path has no OS-level read-only sandbox, so
        # this dedicated review stage always selects Codex's read-only
        # sandbox instead of inheriting the implementation-agent setting.
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent="codex",
            model=stage_model(ctx, "reviewer", reviewer_model),
            prompt_builder=build_strict_review_prompt,
            cwd=Path(review_worktree),
            timeout_s=pr_reviewer_claude_timeout(),
            session_agent=strict_review_agent(head_sha, attempt),
            expected_head_sha=head_sha,
            sandbox="read-only",
            # The bounded issue/diff/prior-review context is fetched by the
            # coordinator adapter; no CI state is an input to this review.
            prompt_kwargs={
                "pr_number": item.pr,
                "issue_number": issue,
                "head_sha": head_sha,
                "issue_title": evidence.issue_title,
                "issue_body": evidence.issue_body,
                "diff": evidence.diff,
                "prior_pr_review_verdict": evidence.prior_pr_review_verdict,
            },
            parse=parse_strict_review_verdict,
            descr="strict_review",
        )
        return JobRequest(job, on_done_state=EVAL)

    def _eval(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """EVAL [M]: hand off GO in memory; NOGO routes to implementation."""
        head_sha = str(item.payload.get("strict_review_head") or "")
        if item.payload.pop("strict_review_failed", None):
            logger.warning(
                "strict_review:%s: review job failed; invalidating eligibility",
                item.issue,
            )
            return self._handle_nogo(
                item,
                ctx,
                head_sha,
                "Strict-review infrastructure failed.\nGrade: F\nVerdict: NOGO",
            )
        verdict = item.payload.get("strict_review_verdict")
        verdict_text = str(item.payload.get("strict_review_text") or "")
        if verdict not in ("GO", "NOGO"):
            logger.warning(
                "strict_review:%s: missing/ambiguous verdict (%r); invalidating eligibility",
                item.issue,
                verdict,
            )
            return self._handle_nogo(
                item,
                ctx,
                head_sha,
                "Strict-review output did not satisfy the final verdict contract.\n"
                "Grade: F\nVerdict: NOGO",
            )
        if verdict == "GO":
            return self._handle_go(item, ctx, head_sha, verdict_text)
        return self._handle_nogo(item, ctx, head_sha, verdict_text)

    def _current_head_or_restart(
        self, item: WorkItem, ctx: StageContext, head_sha: str, *, action: str
    ) -> StepResult | None:
        """Return a terminal/restart outcome unless this exact head remains live."""
        pr_number = item.pr
        if pr_number is None:  # guarded by step(); narrowing
            return StageOutcome(Disposition.FAIL_BACK, "nogo")
        pr_state = ctx.github.gh_pr_state(pr_number)
        terminal = _terminal_pr_outcome(pr_state, pr_number)
        if terminal is not None:
            return terminal
        live_head = str((pr_state or {}).get("headRefOid") or "")
        if head_sha and live_head == head_sha:
            return None
        logger.info(
            "strict_review:%s: head changed before %s (%s -> %s); restarting",
            item.issue,
            action,
            head_sha[:12],
            live_head[:12],
        )
        outcome = self._contain_and_revoke_on_entry(item, ctx)
        return outcome if outcome is not None else Continue(next_state=HEAD_CHECK)

    def _handle_go(
        self, item: WorkItem, ctx: StageContext, head_sha: str, verdict_text: str
    ) -> StepResult:
        """Label a successful current-head PR-review verdict and discard its proof state."""
        pr_number = item.pr
        if pr_number is None:  # guarded by step(); narrowing
            return StageOutcome(Disposition.FAIL_BACK, "nogo")
        current_outcome = self._current_head_or_restart(item, ctx, head_sha, action="GO handoff")
        if current_outcome is not None:
            return current_outcome
        try:
            ctx.github.mark_pr_implementation_go(pr_number)
        except Exception as exc:
            logger.error(
                "strict_review: failed to mark PR #%d implementation-go: %s", pr_number, exc
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_label_failed")
        # Review state is needed only while this stage verifies the current
        # head and applies GO.  It must not cross into merge_wait: the
        # loop-owned label is that stage's only authorization.  Retain only a
        # fixed, non-authorization context; an unknown field is discarded
        # even if it originated before this strict-review pass.
        for key in tuple(item.payload):
            if key not in _GO_HANDOFF_PAYLOAD_KEYS:
                item.payload.pop(key, None)
        return Continue(next_state=SR_FINISH)

    def _handle_nogo(
        self, item: WorkItem, ctx: StageContext, head_sha: str, verdict_text: str
    ) -> StepResult:
        """NOGO: disable+verify auto-merge, post fenced remediation, route to implementation."""
        pr_number = item.pr
        if pr_number is None:  # guarded by step(); narrowing
            return StageOutcome(Disposition.FAIL_BACK, "nogo")
        # A NOGO is as head-bound as a GO: never remediate or label a newer
        # head from a reviewer result that inspected an older one.
        current_outcome = self._current_head_or_restart(item, ctx, head_sha, action="NOGO handling")
        if current_outcome is not None:
            return current_outcome
        if not self._clear_go_and_verify_disabled(pr_number, ctx):
            return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_disable_failed")
        # Containment itself can race with a push.  Do not attach stale
        # remediation feedback or an implementation-no-go label to that new
        # head; restart the review after the safe revocation/defer completed.
        current_outcome = self._current_head_or_restart(
            item, ctx, head_sha, action="NOGO containment"
        )
        if current_outcome is not None:
            return current_outcome
        if not self._post_nogo_remediation(pr_number, ctx, verdict_text):
            return StageOutcome(Disposition.FINISH_FAIL, "strict_nogo_feedback_failed")
        # Posting feedback is not an authorization boundary, but a push while
        # it is written must still prevent the H1 verdict from labeling H2.
        current_outcome = self._current_head_or_restart(
            item, ctx, head_sha, action="NOGO remediation"
        )
        if current_outcome is not None:
            return current_outcome
        try:
            ctx.github.mark_pr_implementation_no_go(pr_number)
        except Exception as e:
            logger.error(
                "strict_review: failed to mark PR #%d implementation-no-go: %s",
                pr_number,
                e,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "strict_nogo_label_failed")
        # The label write is another racing mutation.  Revoke a stale NOGO
        # immediately and restart instead of leaving H2 with H1's verdict.
        current_outcome = self._current_head_or_restart(
            item, ctx, head_sha, action="implementation-no-go handoff"
        )
        if current_outcome is not None:
            try:
                ctx.github.remove_labels(pr_number, [STATE_IMPLEMENTATION_NO_GO])
            except Exception as exc:
                logger.error(
                    "strict_review: failed to revoke stale implementation-no-go on PR #%d: %s",
                    pr_number,
                    exc,
                )
                return StageOutcome(Disposition.FINISH_FAIL, "implementation_no_go_revoke_failed")
            return current_outcome
        item.payload["strict_review_feedback"] = verdict_text
        item.payload.pop("existing_pr_impl_go", None)
        return StageOutcome(Disposition.FAIL_BACK, "nogo")

    @staticmethod
    def _post_nogo_remediation(pr_number: int, ctx: StageContext, verdict_text: str) -> bool:
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
            logger.error(
                "strict_review: failed to post NOGO remediation comment on PR #%d: %s",
                pr_number,
                e,
            )
            return False
        return True

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Store the parsed verdict on the item payload (state still REVIEW_WAIT).

        Args:
            item: The work item whose job completed.
            result: The job result from the worker pool.
            ctx: Stage context.

        """
        if item.payload.pop("strict_review_worktree_pending", None):
            if not result.ok:
                logger.warning(
                    "strict_review:%s: isolated worktree setup failed: %s",
                    item.issue,
                    result.error,
                )
                item.payload["strict_review_worktree_failed"] = True
                return
            value = result.value
            if isinstance(value, dict):
                item.payload["strict_review_worktree"] = str(value.get("path") or "")
                if value.get("dirty"):
                    # The worker intentionally preserves dirty worktrees.
                    # They cannot safely represent an exact remote PR head.
                    item.payload["strict_review_worktree_failed"] = True
            elif isinstance(value, str):
                item.payload["strict_review_worktree"] = value
            if not item.payload.get("strict_review_worktree"):
                item.payload["strict_review_worktree_failed"] = True
            else:
                synced_head = str(item.payload.get("strict_review_head") or "")
                if not item.payload.get("strict_review_worktree_failed") and synced_head:
                    item.payload["strict_review_worktree_head"] = synced_head
            return
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
