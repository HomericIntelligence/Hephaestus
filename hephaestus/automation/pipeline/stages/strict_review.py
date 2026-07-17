"""Strict-review stage: the single automatic authority for implementation-go.

Ninth queue stage (issue #2055), inserted between ``pr_review`` and ``ci``
(``docs/AUTOMATION_LOOP_ARCHITECTURE.md`` "strict_review" section is the
binding contract). This stage is the ONLY automatic producer of
``state:implementation-go``; it never arms auto-merge itself — arming stays
the sole responsibility of ``MergeWaitStage``, which re-validates a
head-bound artifact this stage publishes before it ever arms.

- States: ENTER -> HEAD_CHECK -> WORKTREE_WAIT -> REVIEW_WAIT -> EVAL ->
  SR_FINISH.
  Budget: ``strict_review_iter`` = 1 (a single independent pass; a NOGO
  routes straight to ``implementation``, never loops in-stage).
- ``on_enter``: refresh labels; if a prior pass's captured head
  (``payload["strict_review_head"]``) no longer matches the PR's live head,
  clear the stale payload so a fresh pass restarts (revocation semantics —
  see ``_revoke_on_head_change``).
- HEAD_CHECK [M]: capture ``gh_pr_state(pr)["headRefOid"]`` into
  ``payload["strict_review_head"]`` — the exact value baked into both the
  session key (:func:`~hephaestus.automation.agent_config.strict_review_agent`)
  and the published artifact. A synced isolated worktree and worker-side
  ``git rev-parse HEAD`` check must match this remote SHA before dispatch.
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
  - Missing/ERROR verdict: FAIL_BACK(``nogo``) to implementation.  A
    reviewer result that cannot be trusted must never leave the current head
    eligible for an automatic merge.
- Head-change revocation (``on_enter``): reusing merge_wait's "no waiting
  out a state that can never resolve" philosophy — a head that moved since
  the artifact/session was captured means the OLD review no longer proves
  anything about the NEW head. The stage clears the GO label, verifies
  auto-merge disabled, and restarts HEAD_CHECK for the new head.
"""

from __future__ import annotations

import logging
import re

from hephaestus.automation.agent_config import pr_reviewer_claude_timeout, reviewer_model
from hephaestus.automation.claude_invoke import ReviewVerdict
from hephaestus.automation.prompts.strict_review_gate import build_strict_review_prompt
from hephaestus.automation.session_naming import strict_review_agent
from hephaestus.automation.state_labels import STATE_IMPLEMENTATION_GO

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
    StrictReviewLease,
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
WORKTREE_WAIT = "WORKTREE_WAIT"
REVIEW_WAIT = "REVIEW_WAIT"
EVAL = "EVAL"
SR_FINISH = "SR_FINISH"
FINISH = SR_FINISH

#: HTML-comment marker for the NOGO remediation feedback comment.
STRICT_REVIEW_NOGO_MARKER = "<!-- hephaestus-strict-review-nogo -->"

# Unlike the general review-loop parser, the merge-authorizing strict gate
# accepts only the final, exact output contract.  A diff or prior review may
# legitimately contain a quoted ``Verdict: GO`` line; taking the *first* such
# line would let untrusted evidence choose the gate result.
_STRICT_FINAL_VERDICT_RE = re.compile(r"(?m)^Grade: ([A-F][+-]?)\nVerdict: (GO|NOGO)[ \t]*\n?\Z")


def parse_strict_review_verdict(text: str) -> ReviewVerdict:
    """Parse only the final exact strict-review verdict contract.

    The generic review parser intentionally supports legacy variants and
    therefore finds the first matching line.  This gate has a stronger
    authorization contract: the final two lines must be the exact grade and
    GO/NOGO verdict requested by its prompt.  Anything else is ambiguous and
    is handled as a fail-closed NOGO by :meth:`StrictReviewStage._eval`.
    """
    match = _STRICT_FINAL_VERDICT_RE.search(text)
    if match is None:
        return ReviewVerdict(grade=None, verdict="AMBIGUOUS", raw=text)
    return ReviewVerdict(grade=match.group(1), verdict=match.group(2), raw=text)


def _issue_number(item: WorkItem) -> int:
    """Return a stable issue id; orphan PRs use zero for review-session scope."""
    return item.issue if item.issue is not None else 0


class StrictReviewStage(Stage):
    """Stage: independent read-only GO/NOGO gate; sole implementation-go authority."""

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
        if item.pr is not None:
            return self._contain_and_revoke_on_entry(item, ctx)
        return None

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
        pr_state = ctx.github.gh_pr_state(pr_number)
        terminal = _terminal_pr_outcome(pr_state, pr_number)
        if terminal is not None:
            return terminal
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
            self._drop_lease(item)
            item.state = ENTER
        if not self._clear_go_and_verify_disabled(pr_number, ctx):
            return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_disable_failed")
        return None

    def _clear_go_and_verify_disabled(self, pr_number: int, ctx: StageContext) -> bool:
        """Durably clear the GO label and verify auto-merge is actually disabled."""
        labels_cleared = True
        try:
            ctx.github.remove_labels(pr_number, [STATE_IMPLEMENTATION_GO])
        except Exception as e:
            logger.warning(
                "strict_review: failed to clear implementation-go on PR #%d: %s",
                pr_number,
                e,
            )
            # Do not short-circuit containment: a failed label mutation says
            # nothing about a pre-existing remote auto-merge arm.  We still
            # must attempt the independent deferral below before stopping.
            labels_cleared = False
        deferred = True
        try:
            ctx.github.defer_auto_merge(pr_number)
        except Exception as e:
            logger.warning(
                "strict_review: failed to defer auto-merge on PR #%d: %s",
                pr_number,
                e,
            )
            deferred = False
        disabled = deferred and self._auto_merge_is_disabled(pr_number, ctx)
        return labels_cleared and disabled

    def _auto_merge_is_disabled(self, pr_number: int, ctx: StageContext) -> bool:
        """Read back the disabled state; ambiguity is containment failure."""
        confirmed = ctx.github.gh_pr_state(pr_number)
        if confirmed is None or confirmed.get("autoMergeRequest"):
            logger.error(
                "strict_review: PR #%d could not verify auto-merge disabled after defer",
                pr_number,
            )
            return False
        return True

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
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

        if item.state == ENTER:
            return Continue(next_state=HEAD_CHECK)
        if item.state == HEAD_CHECK:
            return self._head_check(item, ctx)
        if item.state == WORKTREE_WAIT:
            if item.payload.pop("strict_review_worktree_failed", None):
                return StageOutcome(Disposition.FINISH_FAIL, "strict_review_worktree_failed")
            if not item.worktree:
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
        synced_worktree_head = str(item.payload.get("strict_review_worktree_head") or "")
        if not item.worktree or synced_worktree_head.lower() != head_sha.lower():
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
                    "issue_number": item.issue if item.issue is not None else item.pr,
                    "branch_name": branch,
                    "refresh_base": False,
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
        # A terminal result can survive a coordinator restart.  It must be
        # inspected before claiming a lease so a durable NOGO is not confused
        # with a competing live lease and retried forever.  GO remains
        # merge-authorized only through the v2 GO-only accessor below.
        terminal = ctx.github.strict_review_terminal_artifact(item.pr, head_sha)  # type: ignore[arg-type]
        if terminal is not None and not terminal.is_go:
            return self._resume_terminal_nogo(item, ctx, head_sha, terminal.verdict_body)
        # A prior durable v2 GO can survive a coordinator restart.  It is
        # already globally elected and authenticated by the accessor, so no
        # second reviewer may be dispatched merely to recreate its label.
        existing_go = ctx.github.strict_review_artifact(item.pr, head_sha)  # type: ignore[arg-type]
        if existing_go is not None:
            item.payload["strict_review_existing_go"] = True
            item.payload["strict_review_verdict"] = "GO"
            item.payload["strict_review_text"] = ""
            item.payload.pop("strict_review_failed", None)
            return Continue(next_state=EVAL)
        lease = self._lease_from_payload(item)
        if lease is None:
            try:
                lease = ctx.github.claim_strict_review_lease(item.pr, head_sha)  # type: ignore[arg-type]
            except Exception as e:
                logger.warning(
                    "strict_review:%s: could not claim durable review lease for PR #%d: %s",
                    item.issue,
                    item.pr,
                    e,
                )
                item.payload["retry_delay_s"] = 30
                return StageOutcome(Disposition.RETRY, "strict_review_lease_unavailable")
            if lease is None:
                # A concurrent coordinator owns the elected lease (or a
                # terminal NOGO has already fenced this generation).  Do not
                # run a second reviewer or mutate labels from stale output.
                item.payload["retry_delay_s"] = 30
                return StageOutcome(Disposition.RETRY, "strict_review_lease_unavailable")
            item.payload["strict_review_lease_id"] = lease.lease_id
            item.payload["strict_review_lease_comment_id"] = lease.comment_id
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
                "Strict-review evidence could not be fetched and authenticated for this head.\n"
                "Grade: F\nVerdict: NOGO",
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
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "reviewer", reviewer_model),
            prompt_builder=build_strict_review_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=pr_reviewer_claude_timeout(),
            session_agent=strict_review_agent(head_sha, attempt),
            expected_head_sha=head_sha,
            sandbox="read-only",
            # Diff / CI status / prior verdict are seeded into item.payload
            # by the coordinator, which owns the gh reads (mirrors pr_review).
            prompt_kwargs={
                "pr_number": item.pr,
                "issue_number": issue,
                "head_sha": head_sha,
                "issue_title": evidence.issue_title,
                "issue_body": evidence.issue_body,
                "diff": evidence.diff,
                "ci_status": evidence.ci_status,
                "prior_pr_review_verdict": evidence.prior_pr_review_verdict,
            },
            parse=parse_strict_review_verdict,
            descr="strict_review",
        )
        return JobRequest(job, on_done_state=EVAL)

    def _eval(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """EVAL [M]: GO publishes artifact + label (never arms); NOGO remediates."""
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
        """GO: publish the artifact BEFORE the label (doc contract), never arm."""
        pr_number = item.pr
        if pr_number is None:  # guarded by step(); narrowing
            return StageOutcome(Disposition.FAIL_BACK, "nogo")
        current_outcome = self._current_head_or_restart(
            item, ctx, head_sha, action="GO publication"
        )
        if current_outcome is not None:
            return current_outcome
        existing_go = bool(item.payload.pop("strict_review_existing_go", None))
        lease = self._lease_from_payload(item)
        if not existing_go and lease is None:
            # A restored or stale job cannot manufacture a global GO label
            # without its durable fencing token.
            return Continue(next_state=HEAD_CHECK)
        logger.info(
            "strict_review:%s: GO for PR #%d at head %s; %s artifact",
            item.issue,
            pr_number,
            head_sha[:12],
            "reconciling existing" if existing_go else "publishing fenced",
        )
        if not existing_go:
            if lease is None:  # guarded above; kept for type narrowing
                return Continue(next_state=HEAD_CHECK)
            try:
                published_by_us = ctx.github.publish_strict_review_artifact(
                    pr_number, head_sha, verdict_text, is_go=True, lease=lease
                )
            except Exception as e:
                logger.warning(
                    "strict_review: failed to publish GO artifact on PR #%d "
                    "(state:implementation-go NOT applied): %s",
                    pr_number,
                    e,
                )
                return StageOutcome(Disposition.FINISH_FAIL, "strict_go_artifact_failed")
            if not published_by_us:
                # Lost/expired lease: neither label nor feedback may be
                # written by this stale reviewer generation.
                self._drop_lease(item)
                return Continue(next_state=HEAD_CHECK)
        published = ctx.github.strict_review_artifact(pr_number, head_sha)
        if (
            published is None
            or not published.is_go
            or published.head_sha.lower() != head_sha.lower()
        ):
            logger.error(
                "strict_review: PR #%d GO artifact could not be authenticated on read-back",
                pr_number,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "strict_go_artifact_unverified")
        # A push can race the durable comment write.  Never attach a global
        # implementation-GO label to a newer, unreviewed head.
        after_publish_outcome = self._current_head_or_restart(
            item, ctx, head_sha, action="GO label"
        )
        if after_publish_outcome is not None:
            return after_publish_outcome
        try:
            ctx.github.mark_pr_implementation_go(pr_number)
        except Exception as e:
            logger.warning(
                "strict_review: failed to mark PR #%d implementation-go: %s",
                pr_number,
                e,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_label_failed")
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
        lease = self._lease_from_payload(item)
        if lease is None:
            return Continue(next_state=HEAD_CHECK)
        logger.info(
            "strict_review:%s: NOGO for PR #%d; publishing fenced invalidation",
            item.issue,
            pr_number,
        )
        try:
            # A stale reviewer must not even clear a label or post
            # remediation.  First atomically fence its NOGO as the elected
            # holder; only its successful immutable terminal write may cause
            # global containment mutations.
            published_by_us = ctx.github.publish_strict_review_artifact(
                pr_number, head_sha, verdict_text, is_go=False, lease=lease
            )
        except Exception as e:
            logger.error(
                "strict_review: failed to publish NOGO invalidation on PR #%d: %s",
                pr_number,
                e,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "strict_nogo_artifact_failed")
        if not published_by_us:
            self._drop_lease(item)
            return Continue(next_state=HEAD_CHECK)
        # Publishing the old-head invalidation is safe, but a push can race
        # that write.  Do not post old-review feedback or apply the global
        # NOGO label to the new head; contain it and restart instead.
        after_publish_outcome = self._current_head_or_restart(
            item, ctx, head_sha, action="NOGO containment"
        )
        if after_publish_outcome is not None:
            return after_publish_outcome
        if not self._clear_go_and_verify_disabled(pr_number, ctx):
            return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_disable_failed")
        if not self._post_nogo_remediation(pr_number, ctx, verdict_text):
            return StageOutcome(Disposition.FINISH_FAIL, "strict_nogo_feedback_failed")
        try:
            ctx.github.mark_pr_implementation_no_go(pr_number)
        except Exception as e:
            logger.error(
                "strict_review: failed to mark PR #%d implementation-no-go: %s",
                pr_number,
                e,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "strict_nogo_label_failed")
        item.payload["strict_review_feedback"] = verdict_text
        item.payload.pop("existing_pr_impl_go", None)
        return StageOutcome(Disposition.FAIL_BACK, "nogo")

    def _resume_terminal_nogo(
        self, item: WorkItem, ctx: StageContext, head_sha: str, verdict_text: str
    ) -> StepResult:
        """Resume durable NOGO containment without claiming or publishing a lease.

        A crash may occur after the elected reviewer appends its immutable
        NOGO but before it writes the PR-wide remediation label/comment.  The
        durable result is neither a live lease nor permission to rerun the
        reviewer.  Reconcile containment only after a fresh head check; a
        moved head restarts cleanly and never receives stale feedback.
        """
        pr_number = item.pr
        if pr_number is None:  # guarded by step(); narrowing
            return StageOutcome(Disposition.FAIL_BACK, "nogo")
        current_outcome = self._current_head_or_restart(
            item, ctx, head_sha, action="terminal NOGO recovery"
        )
        if current_outcome is not None:
            return current_outcome
        self._drop_lease(item)
        safe_verdict = verdict_text or "Independent strict review recorded a durable NOGO."
        if not self._clear_go_and_verify_disabled(pr_number, ctx):
            return StageOutcome(Disposition.FINISH_FAIL, "auto_merge_disable_failed")
        if not self._post_nogo_remediation(pr_number, ctx, safe_verdict):
            return StageOutcome(Disposition.FINISH_FAIL, "strict_nogo_feedback_failed")
        try:
            ctx.github.mark_pr_implementation_no_go(pr_number)
        except Exception as e:
            logger.error("strict_review: failed to restore NOGO label on PR #%d: %s", pr_number, e)
            return StageOutcome(Disposition.FINISH_FAIL, "strict_nogo_label_failed")
        item.payload["strict_review_feedback"] = safe_verdict
        item.payload.pop("existing_pr_impl_go", None)
        return StageOutcome(Disposition.FAIL_BACK, "nogo")

    @staticmethod
    def _lease_from_payload(item: WorkItem) -> StrictReviewLease | None:
        """Reconstruct a persisted lease only when it still binds this head."""
        head_sha = str(item.payload.get("strict_review_head") or "").lower()
        lease_id = item.payload.get("strict_review_lease_id")
        comment_id = item.payload.get("strict_review_lease_comment_id")
        if (
            not head_sha
            or not isinstance(lease_id, str)
            or not lease_id
            or not isinstance(comment_id, int)
            or comment_id < 1
        ):
            return None
        return StrictReviewLease(head_sha=head_sha, lease_id=lease_id, comment_id=comment_id)

    @staticmethod
    def _drop_lease(item: WorkItem) -> None:
        """Forget an exhausted/lost fence before attempting a fresh election."""
        item.payload.pop("strict_review_lease_id", None)
        item.payload.pop("strict_review_lease_comment_id", None)
        item.payload.pop("strict_review_existing_go", None)

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
                item.worktree = str(value.get("path") or "")
                if value.get("dirty"):
                    # The worker intentionally preserves dirty worktrees.
                    # They cannot safely represent an exact remote PR head.
                    item.payload["strict_review_worktree_failed"] = True
            elif isinstance(value, str):
                item.worktree = value
            if not item.worktree:
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
