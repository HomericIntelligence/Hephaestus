"""Implementation stage: gate plan GO, cut worktree, implement, test, push, PR.

Re-houses the implementation control flow from the legacy per-issue phase
runner (dispatch, plan-ready gate, existing-PR review, and the existing-PR
ownership check) and
``_pr_create_phase.PRCreatePhase._finalize_pr`` (:36) as a pipeline stage
(docs/architecture.md §5.4 "implementation" is the
binding contract):

- States: ENTER -> GATE -> WORKTREE_WAIT -> DIRTY_DECISION_WAIT ->
  ADVISE_WAIT -> IMPLEMENT_WAIT -> REBASE_CONTINUE_WAIT / TEST_WAIT -> TESTFIX_WAIT ->
  COMMIT_PUSH_WAIT -> PR_CREATE. The existing-PR fast path short-circuits
  WORKTREE_WAIT -> DIRTY_DECISION_WAIT -> ADOPTED (ADVANCE to pr_review).
- Budgets: ``implement`` = 2 (bounds ordinary implement attempts INCLUDING
  agent_error retries — the doc's "agent_error -> RETRY (consumes the
  implement budget)"), ``test_fix`` = 1 (one fix attempt on red pre-PR
  tests), ``rebase_conflict`` = 2 (bounds edit-only conflict-resolution
  turns independently), and ``test_fix`` = 1. All read from ROUTES via
  ``ctx.budget``, never hardcoded here.
- GATE [M]: ``state:skip`` check first (operator-only, absolute — #1835);
  skips the item regardless of plan-go/implementation-go, before either the
  existing-PR fast path or the fresh-implement plan-go gate below. Then the
  existing-PR fast path (``_review_existing_pr`` semantics): a PR already
  carrying ``state:implementation-go`` routes to ``merge_wait``; a PR without
  it adopts the PR's
  REAL head branch only after it confirms the PR is open and unarmed, cuts a
  worktree on the ADOPTED branch (``refresh_base=False`` +
  ``sync_to_remote`` — the anti-clobber reset of
  ``_prepare_worktree_for_existing_pr`` :649, so pushed commits are never
  discarded), runs the dirty-salvage decision if needed, and only then
  ADVANCEs to pr_review (ADOPTED). Otherwise the plan-review verdict gate:
  at-or-past ``state:plan-go`` (or already ``state:implementation-go``)
  proceeds; anything else fails back ``plan_not_go`` (routes to
  plan_review).
- agent_error ping-pong bound: when pr_review fails back ``agent_error``
  (flagged in ``payload["agent_error_failback"]``), the GATE's existing-PR
  adoption CONSUMES the ``implement`` budget — otherwise the
  fail-back -> adopt -> ADVANCE cycle would never move a counter and could
  loop forever. Exhaustion -> FINISH_FAIL(``agent_error_exhausted``): the
  reviewer/address infrastructure failed repeatedly and re-adopting the
  same PR again cannot fix it; a human should look at the PR.
- Transient git failures (worktree creation, commit+push) RETRY without
  burning the implement budget, but are bounded by
  :data:`GIT_ERROR_RETRY_CAP` consecutive failures (mirrors
  pr_review.REVIEW_ERROR_RETRY_CAP); at the cap the item finishes failed
  (``git_error``) instead of retrying a broken remote forever. The counter
  resets on any successful git job.
- Owned labels: none — PR creation is the journal entry (doc section 4).
  The only label this stage ever writes is ``state:skip`` on the legacy
  "no commits vs base" runtime error (re-housed from the legacy phase
  runner's runtime-error handler), non-fatally.
- PR_CREATE [M]: ``ctx.github.create_pr`` (idempotent ensure semantics)
  with a ``prompts/pr_review.py get_pr_description`` body [durable]. PR
  review owns implementation labels; this stage does not create merge eligibility.
- Prompt functions (imported, never re-authored):
  ``prompts/implementation.py get_implementation_prompt`` (composed with
  the advise-findings block by :func:`build_implementation_prompt`),
  ``get_dirty_reused_worktree_decision_prompt``,
  ``get_impl_resume_feedback_prompt`` (composed with the failing test
  output by :func:`build_test_fix_prompt`), and
  ``prompts/pr_review.py get_pr_description``.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import cast

from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionRequest,
    SessionLifecycle,
)
from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.address_review_core import (
    MAX_ADDRESS_REPLY_CHARS,
    _parse_addressed_block,
    parse_addressed_replies,
)
from hephaestus.automation.agent_config import (
    advise_claude_timeout,
    advise_model,
    implementer_claude_timeout,
    implementer_model,
)
from hephaestus.automation.commit_policy import normalize_strict_conventional_title
from hephaestus.automation.prompts.address_review import get_address_review_prompt
from hephaestus.automation.prompts.implementation import (
    get_dirty_reused_worktree_decision_prompt,
    get_impl_resume_feedback_prompt,
    get_implementation_prompt,
)
from hephaestus.automation.prompts.pr_review import get_pr_description
from hephaestus.automation.session_naming import (
    AGENT_IMPLEMENTER,
    issue_auto_impl_branch_name,
)
from hephaestus.automation.state_labels import (
    STATE_IMPLEMENTATION_GO,
    STATE_PLAN_BLOCKED,
    STATE_PLAN_GO,
    STATE_SKIP,
    is_implementation_go,
    is_plan_go,
    is_skipped,
)
from hephaestus.automation.worktree_manager import BRANCH_WORKTREE_OWNED
from hephaestus.prompts import PromptCatalog

from ..github_jobs import (
    AppendReplyJournalRequest,
    DeliverReplyHandoffRequest,
    FrozenJson,
    GitHubJob,
    RecoverReplyJournalRequest,
    ReplyHandoffAttempted,
    ReplyJournalAppended,
    ReplyJournalRecovered,
)
from ..jobs import WORKTREE_MATERIALIZED_KEY
from ..reply_handoff import (
    IMPLEMENTATION_REPLY_HANDOFF_JOURNAL_RETRY_CAP,
    IMPLEMENTATION_REPLY_HANDOFF_RETRY_CAP,
    PENDING_IMPLEMENTATION_REPLY_HANDOFF,
    PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL,
    PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL_RETRIES,
    PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES,
    PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES,
    implementation_reply_handoff,
    implementation_reply_handoff_journal_entry,
)
from ..scope_retraction import is_safe_scope_retraction_path, scope_retraction_paths_for_threads
from .base import (
    GIT_JOB_TIMEOUT_S,
    AgentJob,
    AthenaSkillJob,
    AthenaSkillRequest,
    AthenaSkillResult,
    BuildTestJob,
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
    _is_confirmed_open_unarmed,
    _require_issue_labels,
    _terminal_pr_outcome,
    _worktree_path,
    agent_provider,
    source_workspace_binding,
    stage_model,
    write_skip_label,
)
from .repo import (
    DIRECT_SCOPE_BASE_SHA_KEY,
    DIRECT_SCOPE_LOCAL_BRANCH_CLEANUP_KEY,
    DIRECT_SCOPE_RESERVATION_COLLISION_KEY,
    DIRECT_SCOPE_RESERVATION_KEY,
    DIRECT_SCOPE_WORKTREE_NONCE_KEY,
    is_direct_scope_worktree_nonce,
    is_full_commit_sha,
)

logger = logging.getLogger(__name__)

# In-memory mini-states (stage-local strings, never GitHub labels).
ENTER = "ENTER"
GATE = "GATE"
WORKTREE_WAIT = "WORKTREE_WAIT"
DIRTY_DECISION_WAIT = "DIRTY_DECISION_WAIT"
REBASE_WAIT = "REBASE_WAIT"
REBASE_CONFLICT_WAIT = "REBASE_CONFLICT_WAIT"
REBASE_CONTINUE_WAIT = "REBASE_CONTINUE_WAIT"
ADOPTED = "ADOPTED"
ADVISE_WAIT = "ADVISE_WAIT"
IMPLEMENT_WAIT = "IMPLEMENT_WAIT"
TEST_WAIT = "TEST_WAIT"
TESTFIX_WAIT = "TESTFIX_WAIT"
COMMIT_PUSH_WAIT = "COMMIT_PUSH_WAIT"
REPLY_JOURNAL_RECOVERY_WAIT = "REPLY_JOURNAL_RECOVERY_WAIT"
REPLY_JOURNAL_APPEND_WAIT = "REPLY_JOURNAL_APPEND_WAIT"
REPLY_HANDOFF_WAIT = "REPLY_HANDOFF_WAIT"
PR_CREATE = "PR_CREATE"

_STEP_HANDLER_NAMES: dict[str, str] = {
    ENTER: "_enter",
    GATE: "_gate",
    WORKTREE_WAIT: "_worktree_wait",
    DIRTY_DECISION_WAIT: "_dirty_decision_wait",
    REBASE_WAIT: "_rebase_wait",
    REBASE_CONFLICT_WAIT: "_rebase_conflict_wait",
    REBASE_CONTINUE_WAIT: "_rebase_continue_wait",
    ADOPTED: "_adopted",
    ADVISE_WAIT: "_advise_wait",
    IMPLEMENT_WAIT: "_implement_wait",
    TEST_WAIT: "_test_wait",
    TESTFIX_WAIT: "_testfix_wait",
    COMMIT_PUSH_WAIT: "_commit_push_wait",
    REPLY_JOURNAL_RECOVERY_WAIT: "_reply_journal_recovery_wait",
    REPLY_JOURNAL_APPEND_WAIT: "_reply_journal_append_wait",
    REPLY_HANDOFF_WAIT: "_reply_handoff_wait",
    PR_CREATE: "_create_pr",
}

_PENDING_GITHUB_REQUEST = "_pending_github_request"
_REPLY_JOURNAL_RECOVERY_RESULT = "_reply_journal_recovery_result"
_REPLY_JOURNAL_APPEND_RESULT = "_reply_journal_append_result"
_REPLY_HANDOFF_RESULT = "_reply_handoff_result"
_SYNC_RESTORED_WRITER_BEFORE_REBASE = "sync_restored_writer_before_rebase"
_REBASE_HEAD_DRIFT = "rebase_head_drift"


def _issue_number(item: WorkItem) -> int:
    """Return the issue number after the stage-level guard has run."""
    if item.issue is None:
        raise RuntimeError("implementation stage reached without an issue number")
    return item.issue


#: Max CONSECUTIVE transient git failures (worktree creation / commit+push)
#: tolerated before the stage finishes failed (``git_error``) instead of
#: RETRYing forever. Mirrors pr_review.REVIEW_ERROR_RETRY_CAP: transient
#: failures never burn the implement budget, but a persistently broken
#: remote must still terminate. Reset on any successful git job.
GIT_ERROR_RETRY_CAP = 2

#: A pending shared-branch holder waits for the in-flight creator's completion
#: instead of re-entering the implementation drain in a tight loop.
BRANCH_WORKTREE_OWNER_PENDING_DELAY_S = 0.1

#: Timeout for the optional pre-PR test run (mirrors the legacy
#: ``_pr_create_phase`` bound; the budget that matters — ``test_fix`` —
#: lives in ROUTES).
PRE_PR_TEST_TIMEOUT_S = 1800

#: Vetted pre-PR test command (BuildTestJob argv must never carry
#: issue-derived strings).
PRE_PR_TEST_ARGV: tuple[str, ...] = ("uv", "run", "pytest", "tests", "-q", "--tb=short")

#: Hephaestus owns a canonical local entry point for every source check that
#: can run before a PR exists.  Keep this fixed in trusted queue code: issue
#: content and programmatic generic-test overrides must not weaken the gate.
HEPHAESTUS_REQUIRED_CHECK_ARGV: tuple[str, ...] = (
    "env",
    "HEPHAESTUS_CI_REBUILD=1",
    "bash",
    "scripts/run_ci_local.sh",
    "all",
)

#: The required suite runs CI's formerly parallel jobs serially on a local
#: host, so it needs a wider bound than one generic pytest invocation.
HEPHAESTUS_REQUIRED_CHECK_TIMEOUT_S = 7200

NO_COMMIT_REPLY_WARNING = "[auto-msg] reply has no corresponding commit, review thoroughly"
_TRUNCATED_REPLY_WARNING = "[auto-msg] reply truncated to fit review limit"


def _append_no_commit_reply_warning(reply: str) -> str:
    """Append the reviewer warning while preserving the reply-size contract."""
    suffix = f"\n\n{NO_COMMIT_REPLY_WARNING}"
    if len(reply) + len(suffix) <= MAX_ADDRESS_REPLY_CHARS:
        return f"{reply}{suffix}"
    bounded_suffix = f"\n\n{_TRUNCATED_REPLY_WARNING}{suffix}"
    content_budget = MAX_ADDRESS_REPLY_CHARS - len(bounded_suffix)
    return f"{reply[:content_budget].rstrip()}{bounded_suffix}"


def _remediation_reply_head(
    receipt: dict[str, object], snapshots: list[object]
) -> tuple[str | None, bool]:
    """Return the pushed or unchanged snapshotted head for a reply handoff."""
    pushed = receipt.get("pushed") is True
    receipt_head = receipt.get("head_sha")
    if is_full_commit_sha(receipt_head):
        return receipt_head, pushed
    if pushed:
        return None, True

    snapshot_heads: set[str] = set()
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        pr_state = snapshot.get("pr_state")
        snapshot_head = pr_state.get("headRefOid") if isinstance(pr_state, dict) else None
        if not is_full_commit_sha(snapshot_head):
            snapshot_head = snapshot.get("review_commit_sha")
        if is_full_commit_sha(snapshot_head):
            snapshot_heads.add(snapshot_head)
    return (snapshot_heads.pop() if len(snapshot_heads) == 1 else None), False


def build_implementation_prompt(
    issue_number: int,
    issue_title: str = "",
    issue_body: str = "",
    branch_name: str = "",
    worktree_path: str = "",
    advise_findings: str = "",
    rebase_conflict: bool = False,
    rebase_conflict_paths: tuple[str, ...] = (),
) -> str:
    """Compose the implementation prompt with the advise-findings block.

    Module-level composed builder (NOT a closure): :class:`AgentJob` is
    frozen and prompt builders run in-worker, so the builder must be a
    top-level function receiving everything via ``prompt_kwargs``. The base
    prompt is reused verbatim via :func:`get_implementation_prompt`; the
    findings block mirrors :func:`..planning.build_plan_prompt`.

    Args:
        issue_number: GitHub issue number to implement.
        issue_title: Issue title.
        issue_body: Issue body (fenced as untrusted by the base builder).
        branch_name: Feature branch the worktree is on.
        worktree_path: Worktree the implementer works in.
        advise_findings: Advise-step findings; empty string means no block.
        rebase_conflict: Whether the host's mechanical rebase found conflicts
            whose file contents the implementation agent must resolve.
        rebase_conflict_paths: Host-validated paths the agent may edit.

    Returns:
        The full implementer prompt, with the findings block appended when
        ``advise_findings`` is non-empty.

    """
    prompt = get_implementation_prompt(
        issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        branch_name=branch_name,
        worktree_path=worktree_path,
    )
    if not advise_findings and not rebase_conflict:
        return prompt
    blocks: list[str] = [prompt]
    if advise_findings:
        blocks.append(
            PromptCatalog.current().render(
                "implementation/advise_append.j2", advise_findings=advise_findings
            )
        )
    if rebase_conflict:
        blocks.append(
            PromptCatalog.current().render(
                "implementation/rebase_conflict_append.j2",
                conflict_paths=rebase_conflict_paths,
            )
        )
    return "".join(blocks)


def build_test_fix_prompt(issue_number: int, prev_iteration: int, test_output: str) -> str:
    """Compose the resume prompt that feeds failing pre-PR test output back.

    Reuses :func:`get_impl_resume_feedback_prompt` verbatim (doc section 4
    step 7: "resume with test-failure feedback"), with the test failure
    framed as supplemental implementation feedback.

    Args:
        issue_number: GitHub issue number being implemented.
        prev_iteration: 0-based index of the failed test round.
        test_output: Captured pytest output tail from the failing run.

    Returns:
        The resume prompt carrying the test-failure feedback block.

    """
    review_feedback = PromptCatalog.current().render(
        "implementation/test_failure_review.j2", test_output=test_output
    )
    return get_impl_resume_feedback_prompt(
        issue_number=issue_number,
        prev_iteration=prev_iteration,
        review_feedback=review_feedback,
    )


class ImplementationStage(Stage):
    """Stage: gate plan GO, worktree, advise, implement, test, commit, PR."""

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Proceed with no durable writes; all entry checks live in GATE.

        The doc's entry step (verify plan GO at-or-past + existing-PR fast
        path) is the GATE mini-state, an [M] step of this stage — so a
        restart re-runs it idempotently via step(). Nothing is written here.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            None (always proceed to step()), or FINISH_FAIL when the item
            has no issue number.

        """
        if not item.issue:
            logger.warning("implementation: work item has no issue number")
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Execute the next implementation action for the item's current state.

        Args:
            item: The work item with current state.
            ctx: Stage context.

        Returns:
            Continue, JobRequest, or StageOutcome.

        """
        if not item.issue:
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
        handler_name = _STEP_HANDLER_NAMES.get(item.state)
        if handler_name is not None:
            handler = cast(
                Callable[[WorkItem, StageContext], StepResult],
                getattr(self, handler_name),
            )
            return handler(item, ctx)

        logger.warning("implementation:%d: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    def _enter(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """ENTER advances to GATE."""
        return Continue(next_state=GATE)

    def _worktree_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """WORKTREE_WAIT submits the create-worktree git job."""
        issue = _issue_number(item)
        if (
            (
                item.payload.get("implementation_remediation")
                or item.payload.get("post_review_rebase_required")
            )
            and item.payload.pop("implementation_writer_restored", False)
            and item.worktree
        ):
            # pr_review stowed this known branch writer while it created a
            # detached, read-only snapshot.  Reuse it for remediation: trying
            # to create a second worktree for the same PR branch would either
            # fail because the branch is already checked out or tempt review to write there.
            if item.payload.get("post_review_rebase_required"):
                item.payload[_SYNC_RESTORED_WRITER_BEFORE_REBASE] = True
            item.payload["worktree_dirty"] = False
            return Continue(next_state=DIRTY_DECISION_WAIT)
        logger.info("implementation:%d: requesting worktree job", issue)
        adopted = bool(item.payload.get("existing_pr"))
        direct_base_sha = item.payload.get(DIRECT_SCOPE_BASE_SHA_KEY)
        if not adopted and direct_base_sha is not None and not is_full_commit_sha(direct_base_sha):
            return StageOutcome(Disposition.FINISH_FAIL, "direct_scope_base_pin_invalid")
        kwargs: dict[str, object] = {
            "issue_number": issue,
            "branch_name": item.branch,
            # Fresh branch: cut from a freshly refreshed trunk (doc step
            # 2: worktree_manager.create_worktree(refresh_base=True)).
            # ADOPTED branch: never reset to trunk — sync to the PR's
            # remote head instead (the anti-clobber reset of
            # _prepare_worktree_for_existing_pr :649/:693, so re-running
            # never discards pushed commits). Values coordinator-vetted.
            "refresh_base": not adopted and direct_base_sha is None,
            "repo_root": str(ctx.paths.repo_root),
            "source_lane": "impl",
        }
        direct_worktree_nonce = item.payload.get(DIRECT_SCOPE_WORKTREE_NONCE_KEY)
        direct_branch_prefix = f"{issue}-auto-impl-direct-"
        direct_branch_nonce = (
            item.branch.removeprefix(direct_branch_prefix)
            if item.branch.startswith(direct_branch_prefix)
            else None
        )
        if direct_branch_nonce is not None and not is_direct_scope_worktree_nonce(
            direct_branch_nonce
        ):
            return StageOutcome(
                Disposition.FINISH_FAIL,
                "direct_scope_worktree_nonce_invalid",
            )
        if adopted and direct_branch_nonce is not None:
            # A direct source receives a new cursor nonce on every invocation,
            # but an already-open PR retains the nonce that identifies its
            # original managed writer. Recover that writer by the immutable
            # branch identity, not by the new source cursor.
            kwargs["direct_worktree_nonce"] = direct_branch_nonce
        elif not adopted and direct_base_sha is not None:
            kwargs["base_sha"] = direct_base_sha
            if direct_branch_nonce is not None:
                if direct_worktree_nonce != direct_branch_nonce:
                    return StageOutcome(
                        Disposition.FINISH_FAIL,
                        "direct_scope_worktree_nonce_invalid",
                    )
                kwargs["direct_worktree_nonce"] = direct_branch_nonce
            elif direct_worktree_nonce is not None:
                return StageOutcome(
                    Disposition.FINISH_FAIL,
                    "direct_scope_worktree_nonce_invalid",
                )
        if adopted:
            kwargs["sync_to_remote"] = True
            kwargs["pr_number"] = item.pr
        worktree_job = GitJob(
            repo=item.repo,
            op="create_worktree",
            timeout_s=GIT_JOB_TIMEOUT_S,
            kwargs=kwargs,
            descr="create_worktree",
        )
        return JobRequest(worktree_job, on_done_state=DIRTY_DECISION_WAIT)

    def _dirty_decision_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """DIRTY_DECISION_WAIT routes either to retry or to the dirty-decision job."""
        issue = _issue_number(item)
        if (ownership := item.payload.get("branch_worktree_owner")) is not None:
            branch = ownership.get("branch") if isinstance(ownership, dict) else None
            owner_path = ownership.get("owner_path") if isinstance(ownership, dict) else None
            owner_status = "unverified"
            if (
                isinstance(branch, str)
                and branch == item.branch
                and isinstance(owner_path, str)
                and bool(owner_path)
                and ctx.branch_worktree_owner_status is not None
            ):
                owner_status = ctx.branch_worktree_owner_status(item, branch, owner_path)
            if owner_status == "pending":
                # A same-branch pipeline allocation already observed the
                # holder but its success/failure completion has not reached
                # the coordinator. Keep the collision receipt intact and
                # timer-park until that completion; no Git/agent budget is
                # spent and the queue cannot busy-spin while it is pending.
                item.payload["retry_delay_s"] = BRANCH_WORKTREE_OWNER_PENDING_DELAY_S
                return StageOutcome(Disposition.RETRY, "branch_worktree_owner_pending")
            item.payload.pop("branch_worktree_owner", None)
            if owner_status != "verified":
                logger.warning(
                    "implementation:%d: branch-worktree holder for %r at %r is not a "
                    "verified pipeline sibling; refusing to supersede",
                    issue,
                    branch,
                    owner_path,
                )
                return StageOutcome(Disposition.FINISH_FAIL, "branch_worktree_owner_unverified")
            return StageOutcome(
                Disposition.FINISH_PASS,
                f"branch {branch!r} already owned at {owner_path}; "
                "redundant implementation superseded",
            )
        if item.payload.pop(DIRECT_SCOPE_RESERVATION_COLLISION_KEY, None):
            # A worker-side remote probe proved this direct branch already
            # exists.  Retrying an absent-only reservation would only rerun
            # pre-agent work and must never be interpreted as permission to
            # overwrite the other owner.
            return StageOutcome(Disposition.FINISH_FAIL, "direct_scope_reservation_collision")
        if item.payload.pop("git_error", None):
            # Worktree creation failed: transient infrastructure, not an
            # implement outcome. If the retry budget remains, retry the
            # worktree job itself; do not let adopted-PR state fall through
            # to ADOPTED without a valid synced worktree.
            outcome = self._git_retry(item, "worktree creation failed")
            if outcome.disposition is Disposition.RETRY:
                item.state = WORKTREE_WAIT
            return outcome
        # Reviewers never rebase. A reviewed head that merge-wait finds behind
        # or conflicting returns here for implementation-owned rebasing, then
        # passes through a fresh review of the rewritten head.
        if item.payload.get("post_review_rebase_required"):
            adopted_next = REBASE_WAIT
        elif item.payload.get("existing_pr_impl_go"):
            adopted_next = ADOPTED
        else:
            adopted_next = REBASE_WAIT if item.payload.get("existing_pr") else ADVISE_WAIT
        if not item.payload.get("worktree_dirty"):
            return Continue(next_state=adopted_next)
        logger.info("implementation:%d: requesting dirty-worktree decision", issue)
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "implementer", implementer_model),
            prompt_builder=get_dirty_reused_worktree_decision_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=implementer_claude_timeout(),
            sandbox="read-only",
            allowed_tools="Read,Glob,Grep",
            session_agent=AGENT_IMPLEMENTER,
            resume_session_id=item.session_ids.get(AGENT_IMPLEMENTER),
            execution_request=ExecutionRequest(
                AgentRole.IMPLEMENTER,
                AgentOperation.IMPLEMENT_INSPECT,
                (
                    SessionLifecycle.RESUME_REQUIRED
                    if AGENT_IMPLEMENTER in item.session_bindings
                    else SessionLifecycle.START_NEW
                ),
            ),
            resume_binding=item.session_bindings.get(AGENT_IMPLEMENTER),
            prompt_kwargs={
                "branch_name": item.branch,
                "status_text": item.payload.get("worktree_status", ""),
                "diff_text": item.payload.get("worktree_diff", ""),
            },
            descr="dirty_decision",
        )
        return JobRequest(job, on_done_state=adopted_next)

    def _rebase_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Rebase an adopted writer branch before implementation or review.

        The worker performs the deterministic, policy-preserving rebase and
        lease-publishes the resulting head.  A reviewer therefore never
        reuses, rebases, or pushes a writer checkout.
        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "rebase_pr_unavailable")
        if item.payload.get("rebase_conflict"):
            return Continue(next_state=REBASE_CONFLICT_WAIT)
        if item.payload.pop(_REBASE_HEAD_DRIFT, None):
            item.payload.pop("post_review_rebase_required", None)
            item.payload.pop("rebase_conflict", None)
            item.payload.pop(_SYNC_RESTORED_WRITER_BEFORE_REBASE, None)
            return Continue(next_state=ADOPTED)
        if item.payload.pop("rebase_error", None):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_rebase_failed")
        if item.payload.pop("rebase_complete", None):
            item.payload.pop("post_review_rebase_required", None)
            item.payload.pop("rebase_conflict", None)
            item.payload.pop(_SYNC_RESTORED_WRITER_BEFORE_REBASE, None)
            return Continue(
                next_state=(
                    IMPLEMENT_WAIT if item.payload.get("implementation_remediation") else ADOPTED
                )
            )
        state = ctx.github.gh_pr_state(item.pr)
        if not _is_confirmed_open_unarmed(state):
            return StageOutcome(Disposition.FINISH_FAIL, "rebase_pr_state_unverified")
        expected_head = state.get("headRefOid") if isinstance(state, dict) else None
        if not is_full_commit_sha(expected_head):
            return StageOutcome(Disposition.FINISH_FAIL, "rebase_pr_head_unavailable")
        kwargs: dict[str, object] = {
            "cwd": _worktree_path(item, ctx),
            "base_branch": "main",
            "remote": "origin",
            "publish_rebased_head": True,
            "branch": item.branch,
            "expected_remote_sha": expected_head,
        }
        if item.payload.get(_SYNC_RESTORED_WRITER_BEFORE_REBASE):
            kwargs["sync_to_expected_remote_head"] = True
            kwargs["pr_number"] = item.pr
        job = GitJob(
            repo=item.repo,
            op="rebase",
            timeout_s=GIT_JOB_TIMEOUT_S,
            kwargs=kwargs,
            descr="rebase_writer_before_review",
        )
        return JobRequest(job, on_done_state=REBASE_WAIT)

    def _rebase_continue_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Let the host validate, complete, sign, and lease-publish a paused rebase."""
        if item.payload.pop("rebase_error", None):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_rebase_failed")
        if item.payload.pop("rebase_complete", None):
            item.payload.pop("post_review_rebase_required", None)
            item.payload.pop("rebase_conflict", None)
            item.payload.pop("rebase_conflict_paths", None)
            item.payload.pop("rebase_conflict_snapshot", None)
            item.payload.pop("rebase_conflict_index_snapshot", None)
            item.payload.pop("rebase_paused_head_sha", None)
            item.payload.pop("rebase_base_sha", None)
            item.payload.pop("rebase_expected_remote_sha", None)
            return Continue(next_state=ADOPTED)
        if item.payload.pop("rebase_conflict_agent_error", None):
            return Continue(next_state=REBASE_CONFLICT_WAIT)
        if not item.payload.pop("rebase_conflict_agent_complete", False):
            return Continue(next_state=REBASE_CONFLICT_WAIT)
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "rebase_pr_unavailable")
        job = GitJob(
            repo=item.repo,
            op="continue_rebase",
            timeout_s=GIT_JOB_TIMEOUT_S,
            kwargs={
                "cwd": _worktree_path(item, ctx),
                "base_sha": item.payload.get("rebase_base_sha"),
                "remote": "origin",
                "branch": item.branch,
                "expected_remote_sha": item.payload.get("rebase_expected_remote_sha"),
                "conflict_paths": item.payload.get("rebase_conflict_paths"),
                "conflict_snapshot": item.payload.get("rebase_conflict_snapshot"),
                "conflict_index_snapshot": item.payload.get("rebase_conflict_index_snapshot"),
                "paused_head_sha": item.payload.get("rebase_paused_head_sha"),
            },
            descr="complete_host_owned_rebase",
        )
        return JobRequest(job, on_done_state=REBASE_CONTINUE_WAIT)

    def _adopted(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """ADOPTED advances to pr_review after the adopted worktree is ready."""
        issue = _issue_number(item)
        if item.payload.pop("empty_diff_reimplementation", False):
            logger.info(
                "implementation:%d: adopted PR #%s has an empty diff; "
                "running a substantive implementation pass",
                issue,
                item.pr,
            )
            return Continue(next_state=ADVISE_WAIT)
        # Existing-PR fast path complete: worktree ready on the PR's real
        # head branch — hand the PR to pr_review (doc step 1 "skip to
        # step 8": nothing to implement, commit, or create).
        logger.info(
            "implementation:%d: adopted PR #%s (branch %r); advancing to pr_review",
            issue,
            item.pr,
            item.branch,
        )
        return StageOutcome(Disposition.ADVANCE, f"existing PR #{item.pr}")

    def _advise_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """ADVISE_WAIT either skips advice or submits the advise job."""
        issue = _issue_number(item)
        if not ctx.config.enable_advise:
            logger.info("implementation:%d: advise disabled; skipping", issue)
            return Continue(next_state=IMPLEMENT_WAIT)
        logger.info("implementation:%d: requesting advise job", issue)
        workspace = source_workspace_binding(
            item,
            ctx,
            SourceLane.IMPLEMENTATION,
            revision=str(
                item.payload.get("_worktree_cleanup_head_sha")
                or item.payload.get("_impl_source_revision")
                or item.payload.get("_synced_default_branch_sha")
                or ""
            ),
            branch=item.branch or None,
        )
        job = AthenaSkillJob(
            request=AthenaSkillRequest(
                kind="advise",
                repo=item.repo,
                issue=issue,
                agent=agent_provider(ctx),
                model=stage_model(ctx, "advise", advise_model),
                cwd=workspace.cwd if workspace else _worktree_path(item, ctx),
                timeout_s=advise_claude_timeout(),
                workspace=workspace,
                payload={
                    "issue_number": item.issue,
                    "issue_title": item.payload.get("issue_title", ""),
                    "issue_body": item.payload.get("issue_body", ""),
                },
            ),
            descr="advise",
        )
        return JobRequest(job, on_done_state=IMPLEMENT_WAIT)

    def _implement_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:  # noqa: C901
        """IMPLEMENT_WAIT submits the implementation job when budget remains."""
        issue = _issue_number(item)
        if item.payload.get("athena_advise_error"):
            return StageOutcome(Disposition.FINISH_FAIL, "athena_advise_failed")
        entry_outcome = self._implementation_agent_turn_entry_outcome(item, ctx, issue)
        if entry_outcome is not None:
            return entry_outcome
        # Clear stale results at submission so a failed later attempt can
        # never replay an earlier attempt's output downstream.
        item.payload.pop("implement_error", None)
        item.payload.pop("implement_summary", None)
        if item.payload.get("implementation_remediation"):
            remediation_threads = item.payload.get("remediation_threads")
            if (
                item.pr is None
                or not isinstance(remediation_threads, list)
                or not remediation_threads
            ):
                return StageOutcome(Disposition.FINISH_FAIL, "remediation_threads_invalid")
            logger.info(
                "implementation:%d: addressing %d review thread(s)",
                issue,
                len(remediation_threads),
            )
            workspace = source_workspace_binding(
                item,
                ctx,
                SourceLane.IMPLEMENTATION,
                revision=str(
                    item.payload.get("_impl_source_revision")
                    or item.payload.get("_worktree_cleanup_head_sha")
                    or item.payload.get("reviewed_pr_head_sha")
                    or ""
                ),
                branch=item.branch or None,
            )
            scope_retraction_paths = scope_retraction_paths_for_threads(remediation_threads)
            if scope_retraction_paths is None:
                return StageOutcome(Disposition.FINISH_FAIL, "scope_retraction_path_invalid")
            if scope_retraction_paths:
                base_sha = item.payload.get("reviewed_pr_base_sha")
                if not is_full_commit_sha(base_sha):
                    return StageOutcome(
                        Disposition.FINISH_FAIL,
                        "scope_retraction_base_unavailable",
                    )
                item.payload["scope_retraction_paths"] = scope_retraction_paths
            else:
                item.payload.pop("scope_retraction_paths", None)
            snapshots = item.payload.get("remediation_thread_snapshots")
            if isinstance(snapshots, list):
                if item.payload.get(PENDING_IMPLEMENTATION_REPLY_HANDOFF) is not None:
                    return Continue(next_state=PR_CREATE)
                recovery_result = item.payload.pop(_REPLY_JOURNAL_RECOVERY_RESULT, None)
                if recovery_result == "retry":
                    item.state = REPLY_JOURNAL_RECOVERY_WAIT
                    return StageOutcome(
                        Disposition.RETRY,
                        "implementation_reply_handoff_journal_read",
                    )
                if recovery_result == "invalid":
                    return StageOutcome(
                        Disposition.FINISH_FAIL,
                        "implementation_reply_handoff_journal_invalid",
                    )
                if not item.payload.pop("_reply_journal_recovery_complete", False):
                    return Continue(next_state=REPLY_JOURNAL_RECOVERY_WAIT)
            job = AgentJob(
                repo=item.repo,
                issue=issue,
                agent=agent_provider(ctx),
                model=stage_model(ctx, "implementer", implementer_model),
                prompt_builder=get_address_review_prompt,
                cwd=workspace.cwd if workspace else _worktree_path(item, ctx),
                timeout_s=implementer_claude_timeout(),
                workspace=workspace,
                allowed_tools="Read,Write,Edit,Glob,Grep,Bash,Task,Skill",
                session_agent=AGENT_IMPLEMENTER,
                resume_session_id=item.session_ids.get(AGENT_IMPLEMENTER),
                execution_request=ExecutionRequest(
                    AgentRole.IMPLEMENTER,
                    AgentOperation.ADDRESS_REVIEW,
                    (
                        SessionLifecycle.RESUME_REQUIRED
                        if AGENT_IMPLEMENTER in item.session_bindings
                        else SessionLifecycle.START_NEW
                    ),
                ),
                resume_binding=item.session_bindings.get(AGENT_IMPLEMENTER),
                prompt_kwargs={
                    "pr_number": item.pr,
                    "issue_number": issue,
                    "worktree_path": item.worktree,
                    "threads_json": json.dumps(remediation_threads),
                    "task_block": "\n\n".join(
                        part
                        for part in (
                            f"Linked issue #{issue}: {item.payload.get('issue_title', '')}".strip(),
                            str(item.payload.get("issue_body", "")),
                            str(item.payload.get("pr_description", "")),
                        )
                        if part
                    ),
                    "diff_text": str(item.payload.get("pr_diff", "")),
                    "scope_retraction_paths": scope_retraction_paths or (),
                },
                parse=_parse_addressed_block,
                descr="address_review",
            )
            return JobRequest(job, on_done_state=TEST_WAIT)
        logger.info("implementation:%d: requesting implement job", issue)
        workspace = source_workspace_binding(
            item,
            ctx,
            SourceLane.IMPLEMENTATION,
            revision=str(
                item.payload.get("_worktree_cleanup_head_sha")
                or item.payload.get("_impl_source_revision")
                or item.payload.get("_synced_default_branch_sha")
                or ""
            ),
            branch=item.branch or None,
        )
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "implementer", implementer_model),
            prompt_builder=build_implementation_prompt,
            cwd=workspace.cwd if workspace else _worktree_path(item, ctx),
            timeout_s=implementer_claude_timeout(),
            workspace=workspace,
            allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
            session_agent=AGENT_IMPLEMENTER,
            resume_session_id=item.session_ids.get(AGENT_IMPLEMENTER),
            execution_request=ExecutionRequest(
                AgentRole.IMPLEMENTER,
                AgentOperation.IMPLEMENT,
                (
                    SessionLifecycle.RESUME_REQUIRED
                    if AGENT_IMPLEMENTER in item.session_bindings
                    else SessionLifecycle.START_NEW
                ),
            ),
            resume_binding=item.session_bindings.get(AGENT_IMPLEMENTER),
            prompt_kwargs={
                "issue_number": item.issue,
                "issue_title": item.payload.get("issue_title", ""),
                "issue_body": item.payload.get("issue_body", ""),
                "branch_name": item.branch,
                "worktree_path": item.worktree,
                "advise_findings": item.payload.get("advise_findings", ""),
                "rebase_conflict": bool(item.payload.get("rebase_conflict")),
                "rebase_conflict_paths": tuple(item.payload.get("rebase_conflict_paths") or ()),
            },
            descr="implement",
        )
        return JobRequest(job, on_done_state=TEST_WAIT)

    @staticmethod
    def _implementation_agent_turn_entry_outcome(
        item: WorkItem, ctx: StageContext, issue: int
    ) -> StepResult | None:
        """Return any outcome that must happen before an ordinary implement turn."""
        if item.payload.get("rebase_conflict"):
            return Continue(next_state=REBASE_CONFLICT_WAIT)
        return ImplementationStage._ordinary_implement_budget_outcome(item, ctx, issue)

    @staticmethod
    def _ordinary_implement_budget_outcome(
        item: WorkItem, ctx: StageContext, issue: int
    ) -> StageOutcome | None:
        """Return the ordinary implementation exhaustion outcome, if reached."""
        budget = ctx.budget("implement")
        attempts = item.attempts.get("implement", 0)
        if attempts < budget:
            return None
        logger.error(
            "implementation:%d: implement budget exhausted (%d/%d)",
            issue,
            attempts,
            budget,
        )
        return StageOutcome(Disposition.FINISH_FAIL, "implement_exhausted")

    def _rebase_conflict_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Build one separately-budgeted edit-only conflict-resolution turn."""
        issue = _issue_number(item)
        if not item.payload.get("rebase_conflict"):
            return StageOutcome(Disposition.FINISH_FAIL, "rebase_conflict_receipt_missing")
        if item.attempts.get("rebase_conflict", 0) >= ctx.budget("rebase_conflict"):
            return StageOutcome(Disposition.FINISH_FAIL, "rebase_conflict_exhausted")
        logger.info("implementation:%d: requesting edit-only rebase resolution", issue)
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "implementer", implementer_model),
            prompt_builder=build_implementation_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=implementer_claude_timeout(),
            allowed_tools="Read,Write,Edit,Glob,Grep",
            session_agent=AGENT_IMPLEMENTER,
            resume_session_id=item.session_ids.get(AGENT_IMPLEMENTER),
            execution_request=ExecutionRequest(
                AgentRole.IMPLEMENTER,
                AgentOperation.IMPLEMENT,
                (
                    SessionLifecycle.RESUME_REQUIRED
                    if AGENT_IMPLEMENTER in item.session_bindings
                    else SessionLifecycle.START_NEW
                ),
            ),
            resume_binding=item.session_bindings.get(AGENT_IMPLEMENTER),
            prompt_kwargs={
                "issue_number": item.issue,
                "issue_title": item.payload.get("issue_title", ""),
                "issue_body": item.payload.get("issue_body", ""),
                "branch_name": item.branch,
                "worktree_path": item.worktree,
                "advise_findings": "",
                "rebase_conflict": True,
                "rebase_conflict_paths": tuple(item.payload.get("rebase_conflict_paths") or ()),
            },
            descr="resolve_rebase_conflict",
        )
        return JobRequest(job, on_done_state=REBASE_CONTINUE_WAIT)

    def _test_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """TEST_WAIT either retries the implementer or runs the pre-PR tests."""
        issue = _issue_number(item)
        if item.payload.pop("implement_error", None):
            # The implement job hard-failed. The attempt was counted in
            # on_job_done (doc: agent_error consumes the implement
            # budget); RETRY re-enters the stage for the next attempt.
            return StageOutcome(Disposition.RETRY, "agent_error")
        is_hephaestus = (ctx.org.casefold(), item.repo.casefold()) == (
            "homericintelligence",
            "hephaestus",
        )
        run_hephaestus_pre_pr_checks = (
            is_hephaestus and item.pr is None and not bool(item.payload.get("existing_pr"))
        )
        run_configured_pre_pr_checks = not is_hephaestus and bool(
            getattr(ctx.config, "run_pre_pr_tests", False)
        )
        if not (run_hephaestus_pre_pr_checks or run_configured_pre_pr_checks):
            return Continue(next_state=COMMIT_PUSH_WAIT)
        item.payload.pop("tests_failed", None)
        item.payload.pop("test_output", None)
        item.payload.pop("test_receipt", None)
        logger.info("implementation:%d: requesting pre-PR test job", issue)
        test_argv = (
            HEPHAESTUS_REQUIRED_CHECK_ARGV
            if run_hephaestus_pre_pr_checks
            else tuple(getattr(ctx.config, "pre_pr_test_argv", PRE_PR_TEST_ARGV))
        )
        item.payload["test_command"] = shlex.join(test_argv)
        test_job = BuildTestJob(
            repo=item.repo,
            cwd=_worktree_path(item, ctx),
            argv=test_argv,
            timeout_s=(
                HEPHAESTUS_REQUIRED_CHECK_TIMEOUT_S
                if run_hephaestus_pre_pr_checks
                else PRE_PR_TEST_TIMEOUT_S
            ),
            descr="pre_pr_tests",
        )
        return JobRequest(test_job, on_done_state=COMMIT_PUSH_WAIT)

    def _testfix_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """TESTFIX_WAIT submits the test-fix job while budget remains."""
        issue = _issue_number(item)
        budget = ctx.budget("test_fix")
        if item.attempts.get("test_fix", 0) >= budget:
            logger.error(
                "implementation:%d: tests still red after %d fix attempt(s)",
                issue,
                budget,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "tests_red")
        logger.info("implementation:%d: requesting test-fix job", issue)
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "implementer", implementer_model),
            prompt_builder=build_test_fix_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=implementer_claude_timeout(),
            allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
            session_agent=AGENT_IMPLEMENTER,
            resume_session_id=item.session_ids.get(AGENT_IMPLEMENTER),
            execution_request=ExecutionRequest(
                AgentRole.IMPLEMENTER,
                AgentOperation.TEST_FIX,
                SessionLifecycle.RESUME_REQUIRED,
            ),
            resume_binding=item.session_bindings.get(AGENT_IMPLEMENTER),
            prompt_kwargs={
                "issue_number": item.issue,
                "prev_iteration": item.attempts.get("test_fix", 0),
                "test_output": item.payload.get("test_output", ""),
            },
            descr="test_fix",
        )
        return JobRequest(job, on_done_state=TEST_WAIT)

    def _commit_push_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """COMMIT_PUSH_WAIT either re-enters test-fix or submits commit+push."""
        issue = _issue_number(item)
        if item.payload.get("tests_failed"):
            return Continue(next_state=TESTFIX_WAIT)
        logger.info("implementation:%d: requesting commit+push job", issue)
        agent = agent_provider(ctx)
        kwargs: dict[str, object] = {
            "issue_number": issue,
            "worktree_path": item.worktree,
            "branch": item.branch,
            "agent": agent,
            "agent_model": stage_model(ctx, "implementer", implementer_model, provider=agent),
        }
        direct_base_sha = item.payload.get(DIRECT_SCOPE_BASE_SHA_KEY)
        # A direct cursor's bootstrap pin reserves a newly created writer
        # branch.  Once an existing PR is adopted, its remote branch is the
        # writer authority; carrying the cursor pin forward must neither
        # require a new receipt nor lease-push against the unrelated trunk
        # SHA.
        requires_fresh_direct_reservation = (
            not bool(item.payload.get("existing_pr")) and direct_base_sha is not None
        )
        if requires_fresh_direct_reservation:
            if not is_full_commit_sha(direct_base_sha):
                return StageOutcome(Disposition.FINISH_FAIL, "direct_scope_base_pin_invalid")
            kwargs["expected_remote_sha"] = direct_base_sha
        scope_retraction_paths = item.payload.get("scope_retraction_paths")
        if scope_retraction_paths is not None:
            if not isinstance(scope_retraction_paths, tuple) or not all(
                is_safe_scope_retraction_path(path) for path in scope_retraction_paths
            ):
                return StageOutcome(Disposition.FINISH_FAIL, "scope_retraction_path_invalid")
            base_sha = item.payload.get("reviewed_pr_base_sha")
            if not is_full_commit_sha(base_sha):
                return StageOutcome(Disposition.FINISH_FAIL, "scope_retraction_base_unavailable")
            kwargs["scope_retraction_paths"] = scope_retraction_paths
            kwargs["scope_retraction_base_sha"] = base_sha
        push_job = GitJob(
            repo=item.repo,
            op="commit_push",
            timeout_s=GIT_JOB_TIMEOUT_S,
            kwargs=kwargs,
            descr="commit_push",
        )
        return JobRequest(push_job, on_done_state=PR_CREATE)

    @staticmethod
    def _github_job(
        item: WorkItem,
        ctx: StageContext,
        request: RecoverReplyJournalRequest
        | AppendReplyJournalRequest
        | DeliverReplyHandoffRequest,
        descr: str,
    ) -> GitHubJob:
        """Build one repository-scoped closed GitHub job."""
        return GitHubJob(
            repo=item.repo,
            repo_root=Path(str(ctx.paths.repo_root)).resolve(),
            request=request,
            descr=descr,
        )

    def _reply_journal_recovery_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Dispatch a detached exact-thread journal recovery read."""
        if item.issue is None or item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
        snapshots = item.payload.get("remediation_thread_snapshots")
        if not isinstance(snapshots, list):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
        pending = item.payload.get(_PENDING_GITHUB_REQUEST)
        if pending is None:
            pending = RecoverReplyJournalRequest(
                # Pull requests share the issue-comments REST channel. Keep
                # this transient recovery record on the PR, never its linked
                # issue, so the issue has only the canonical plan and review.
                issue_number=item.pr,
                pr_number=item.pr,
                threads=FrozenJson.snapshot(snapshots),
            )
            item.payload[_PENDING_GITHUB_REQUEST] = pending
        if not isinstance(pending, RecoverReplyJournalRequest):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
        return JobRequest(
            self._github_job(item, ctx, pending, "recover_implementation_reply_journal"),
            on_done_state=IMPLEMENT_WAIT,
        )

    def _reply_journal_append_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Dispatch the exact prepared journal append without GitHub I/O inline."""
        if item.issue is None or item.pr is None:
            return StageOutcome(
                Disposition.FINISH_FAIL,
                "implementation_reply_handoff_journal_invalid",
            )
        pending_journal = item.payload.get(PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL)
        handoff = item.payload.get(PENDING_IMPLEMENTATION_REPLY_HANDOFF)
        if not isinstance(pending_journal, dict):
            return StageOutcome(
                Disposition.FINISH_FAIL,
                "implementation_reply_handoff_journal_invalid",
            )
        expected = implementation_reply_handoff_journal_entry(item.pr, handoff)
        marker = pending_journal.get("marker")
        body = pending_journal.get("body")
        if (
            expected is None
            or not isinstance(marker, str)
            or not isinstance(body, str)
            or (marker, body) != expected
        ):
            return StageOutcome(
                Disposition.FINISH_FAIL,
                "implementation_reply_handoff_journal_invalid",
            )
        pending = item.payload.get(_PENDING_GITHUB_REQUEST)
        if pending is None:
            pending = AppendReplyJournalRequest(
                issue_number=item.pr,
                marker=marker,
                body=body,
            )
            item.payload[_PENDING_GITHUB_REQUEST] = pending
        if not isinstance(pending, AppendReplyJournalRequest) or pending != (
            AppendReplyJournalRequest(item.pr, marker, body)
        ):
            return StageOutcome(
                Disposition.FINISH_FAIL,
                "implementation_reply_handoff_journal_invalid",
            )
        return JobRequest(
            self._github_job(item, ctx, pending, "append_implementation_reply_journal"),
            on_done_state=PR_CREATE,
        )

    def _reply_handoff_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Dispatch an exact already-journaled reply handoff."""
        if item.issue is None or item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
        handoff = item.payload.get(PENDING_IMPLEMENTATION_REPLY_HANDOFF)
        visibility_retries = item.payload.get(
            PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES,
            0,
        )
        if (
            not isinstance(handoff, dict)
            or isinstance(visibility_retries, bool)
            or not isinstance(visibility_retries, int)
            or visibility_retries < 0
        ):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
        pending = item.payload.get(_PENDING_GITHUB_REQUEST)
        if pending is None:
            pending = DeliverReplyHandoffRequest(
                issue_number=item.issue,
                pr_number=item.pr,
                handoff=FrozenJson.snapshot(handoff),
                visibility_retries=visibility_retries,
            )
            item.payload[_PENDING_GITHUB_REQUEST] = pending
        if not isinstance(pending, DeliverReplyHandoffRequest):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
        return JobRequest(
            self._github_job(item, ctx, pending, "deliver_implementation_reply_handoff"),
            on_done_state=PR_CREATE,
        )

    def on_job_done(  # noqa: C901
        self, item: WorkItem, result: JobResult, ctx: StageContext
    ) -> None:
        """Store job results on the item payload (state is still the WAIT state).

        The implement attempt is counted HERE, on job completion (success or
        hard failure alike — doc: "agent_error -> RETRY (consumes the
        implement budget)"). Interrupted results never reach this method, so
        an interrupt can never burn budget.

        Args:
            item: The work item to update.
            result: The job result from the worker pool.
            ctx: Stage context.

        """
        if item.state == WORKTREE_WAIT:
            self._on_worktree_done(item, result)
            return

        if item.state == DIRTY_DECISION_WAIT:
            if result.ok and result.value:
                # COMMIT/STASH decision; the git worker acts on it (#1817).
                item.payload["dirty_decision"] = str(result.value)
            return

        if item.state == REBASE_WAIT:
            if result.ok:
                value = result.value if isinstance(result.value, dict) else {}
                if value.get("head_drift"):
                    item.payload[_REBASE_HEAD_DRIFT] = True
                else:
                    head_sha = value.get("head_sha")
                    if is_full_commit_sha(head_sha):
                        item.payload["_impl_source_revision"] = head_sha
                    item.payload["rebase_complete"] = True
            elif result.error == "mechanical rebase hit conflicts; resolution required":
                logger.warning(
                    "implementation:%s: writer rebase paused for host-owned conflict resolution",
                    item.issue,
                )
                self._record_rebase_conflict(item, result)
            else:
                logger.warning(
                    "implementation:%s: writer rebase failed: %s", item.issue, result.error
                )
                item.payload["rebase_error"] = True
            return

        if item.state == REBASE_CONTINUE_WAIT:
            if result.ok:
                value = result.value if isinstance(result.value, dict) else {}
                head_sha = value.get("head_sha")
                if is_full_commit_sha(head_sha):
                    item.payload["_impl_source_revision"] = head_sha
                item.payload["rebase_complete"] = True
            elif (result.error or "").startswith("rebase conflict resolution required"):
                self._record_rebase_conflict(item, result)
            else:
                logger.warning(
                    "implementation:%s: host rebase completion failed: %s",
                    item.issue,
                    result.error,
                )
                diagnostic = _rebase_failure_diagnostic(result)
                if diagnostic is not None:
                    item.payload["rebase_failure_diagnostic"] = diagnostic
                item.payload["rebase_error"] = True
            return

        if item.state == ADVISE_WAIT:
            if not result.ok:
                item.payload["athena_advise_error"] = result.error or "advise failed"
                return
            if result.value:
                if not isinstance(result.value, AthenaSkillResult) or not result.value.ok:
                    item.payload["athena_advise_error"] = "invalid Athena advise result"
                    return
                item.payload["advise_findings"] = result.value.context
                item.payload["athena_advise_receipt"] = result.value.receipt
            return

        if item.state == IMPLEMENT_WAIT:
            self._on_implement_done(item, result)
            return

        if item.state == REBASE_CONFLICT_WAIT:
            self._on_rebase_conflict_agent_done(item, result)
            return

        if item.state == TEST_WAIT:
            self._on_tests_done(item, result)
            return

        if item.state == TESTFIX_WAIT:
            item.attempts["test_fix"] = item.attempts.get("test_fix", 0) + 1
            return

        if item.state == REPLY_JOURNAL_RECOVERY_WAIT:
            self._on_reply_journal_recovery_done(item, result)
            return

        if item.state == REPLY_JOURNAL_APPEND_WAIT:
            self._on_reply_journal_append_done(item, result)
            return

        if item.state == REPLY_HANDOFF_WAIT:
            self._on_reply_handoff_done(item, result)
            return

        if item.state == COMMIT_PUSH_WAIT:
            self._on_commit_push_done(item, result)

    @staticmethod
    def _matching_receipt_request(item: WorkItem, receipt: object) -> bool:
        """Return whether *receipt* belongs to the item's exact pending request."""
        pending = item.payload.get(_PENDING_GITHUB_REQUEST)
        return getattr(receipt, "request", None) == pending

    @staticmethod
    def _on_reply_journal_recovery_done(item: WorkItem, result: JobResult) -> None:
        """Apply a journal recovery receipt before the coordinator advances state."""
        if not result.ok:
            item.payload[_REPLY_JOURNAL_RECOVERY_RESULT] = "retry"
            return
        receipt = result.value
        if not isinstance(receipt, ReplyJournalRecovered) or not (
            ImplementationStage._matching_receipt_request(item, receipt)
        ):
            item.payload[_REPLY_JOURNAL_RECOVERY_RESULT] = "invalid"
            return
        item.payload.pop(_PENDING_GITHUB_REQUEST, None)
        item.payload["_reply_journal_recovery_complete"] = True
        if receipt.handoff is None:
            return
        handoff = receipt.handoff.thaw()
        snapshots = item.payload.get("remediation_thread_snapshots")
        current_head, _pushed = _remediation_reply_head(
            {},
            snapshots if isinstance(snapshots, list) else [],
        )
        if isinstance(handoff, dict) and handoff.get("head_sha") == current_head:
            handoff["reconciliation_only"] = True
            item.payload[PENDING_IMPLEMENTATION_REPLY_HANDOFF] = handoff
            item.payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
            logger.info(
                "implementation:%s: recovered exact GitHub-journaled review reply handoff",
                item.issue,
            )

    @staticmethod
    def _on_reply_journal_append_done(item: WorkItem, result: JobResult) -> None:
        """Apply a correlated append receipt or record a bounded host-only retry."""
        if not result.ok:
            retries = item.payload.get(PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL_RETRIES, 0)
            if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
                item.payload[_REPLY_JOURNAL_APPEND_RESULT] = "invalid"
                return
            retries += 1
            item.payload[PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL_RETRIES] = retries
            item.payload[_REPLY_JOURNAL_APPEND_RESULT] = (
                "retry" if retries <= IMPLEMENTATION_REPLY_HANDOFF_JOURNAL_RETRY_CAP else "failed"
            )
            return
        receipt = result.value
        if not isinstance(receipt, ReplyJournalAppended) or not (
            ImplementationStage._matching_receipt_request(item, receipt)
        ):
            item.payload[_REPLY_JOURNAL_APPEND_RESULT] = "invalid"
            return
        item.payload.pop(_PENDING_GITHUB_REQUEST, None)
        item.payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL, None)
        item.payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL_RETRIES, None)
        item.payload[_REPLY_JOURNAL_APPEND_RESULT] = "completed"

    @staticmethod
    def _on_reply_handoff_done(item: WorkItem, result: JobResult) -> None:
        """Apply a detached exact-reply receipt without retaining mutable state."""
        receipt = result.value
        if not result.ok:
            status = "blocked"
        elif not isinstance(receipt, ReplyHandoffAttempted) or not (
            ImplementationStage._matching_receipt_request(item, receipt)
        ):
            item.payload[_REPLY_HANDOFF_RESULT] = "invalid"
            return
        else:
            status = receipt.status
            item.payload.pop(_PENDING_GITHUB_REQUEST, None)
            if receipt.remaining_handoff is None:
                item.payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
            else:
                remaining = receipt.remaining_handoff.thaw()
                if not isinstance(remaining, dict):
                    item.payload[_REPLY_HANDOFF_RESULT] = "invalid"
                    return
                item.payload[PENDING_IMPLEMENTATION_REPLY_HANDOFF] = remaining
            if receipt.visibility_retries:
                item.payload[PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES] = (
                    receipt.visibility_retries
                )
            else:
                item.payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES, None)
            if receipt.retry_delay_s is not None:
                item.payload["retry_delay_s"] = receipt.retry_delay_s

        if status == "retry":
            retries = item.payload.get(PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, 0)
            if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
                item.payload[_REPLY_HANDOFF_RESULT] = "invalid"
                return
            retries += 1
            item.payload[PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES] = retries
            status = "retry" if retries <= IMPLEMENTATION_REPLY_HANDOFF_RETRY_CAP else "failed"
        item.payload[_REPLY_HANDOFF_RESULT] = status

    @staticmethod
    def _on_commit_push_done(item: WorkItem, result: JobResult) -> None:
        """Record commit+push success, no-commit skip, or git failure."""
        if result.ok:
            receipt = result.value if isinstance(result.value, dict) else {}
            receipt_head = receipt.get("head_sha")
            if is_full_commit_sha(receipt_head):
                item.payload["_worktree_cleanup_head_sha"] = receipt_head
            pushed = receipt.get("pushed") is True if receipt else bool(result.value)
            if not pushed:
                item.payload["no_commits"] = True
                # The worker's no-commit path conditionally released the
                # remote branch.  Keep the exact receipt so Finished can
                # remove the now-unused local branch after its worktree is
                # detached; otherwise the next direct run would fail closed
                # on that stale local ref.
                reservation = item.payload.pop(DIRECT_SCOPE_RESERVATION_KEY, None)
                if isinstance(reservation, dict):
                    item.payload[DIRECT_SCOPE_LOCAL_BRANCH_CLEANUP_KEY] = reservation
            else:
                # A published branch has real commits and must not be
                # released by terminal cleanup.
                item.payload.pop(DIRECT_SCOPE_RESERVATION_KEY, None)
            ImplementationStage._post_remediation_replies_after_push(item, result)
            # A successful worker result ends the consecutive-git-failure
            # streak even when no commit was produced; PR_CREATE handles skip.
            item.payload.pop("git_error_retries", None)
            return
        error_text = (result.error or "").lower()
        if "no commits" in error_text:
            # Legacy _handle_runtime_error (:348): "no commits between
            # base and branch" maps to state:skip, not a hard failure.
            item.payload["no_commits"] = True
            return
        logger.warning("implementation:%s: commit+push failed: %s", item.issue, result.error)
        item.payload["git_error"] = True

    @staticmethod
    def _post_remediation_replies_after_push(item: WorkItem, result: JobResult) -> None:
        """Prepare one head-gated reply handoff after the writer runs.

        GitHub can briefly expose the previous PR head immediately after a
        successful push.  The PR_CREATE state owns the bounded host-only
        replay, so the exact agent responses survive that visibility lag
        without another implementation turn or commit. Before that retry can
        begin, this method records the exact batch in GitHub's immutable
        journal, allowing an interrupted loop to recover the original writer
        response. A successful no-commit remediation is posted against the
        unchanged reviewed head with an explicit warning so the reviewer—not
        the implementation stage—decides whether the reply is sufficient.
        """
        if not item.payload.get("implementation_remediation"):
            return
        receipt = result.value if isinstance(result.value, dict) else {}
        snapshots = item.payload.get("remediation_thread_snapshots")
        if not isinstance(snapshots, list):
            item.payload["remediation_reply_error"] = True
            return

        head_sha, pushed = _remediation_reply_head(receipt, snapshots)
        if not is_full_commit_sha(head_sha):
            item.payload["remediation_reply_error"] = True
            return
        replies = parse_addressed_replies(
            item.payload.get("remediation_output"),
            snapshots,
        )
        if replies is None:
            if pushed and item.pr is not None:
                logger.warning(
                    "implementation:%d: pushed remediation %s returned an invalid reply "
                    "mapping; posting no replies and returning PR #%d for fresh review",
                    item.issue,
                    head_sha,
                    item.pr,
                )
                item.payload.pop("implementation_remediation", None)
                item.payload.pop("remediation_output", None)
                return
            item.payload["remediation_reply_error"] = True
            return
        if item.pr is None:
            item.payload["remediation_reply_error"] = True
            return
        if not pushed:
            replies = {
                thread_id: _append_no_commit_reply_warning(reply)
                for thread_id, reply in replies.items()
            }
            item.payload.pop("no_commits", None)
        handoff = implementation_reply_handoff(
            head_sha,
            snapshots,
            replies,
            secrets.token_hex(16),
        )
        if handoff is None:
            item.payload["remediation_reply_error"] = True
            return
        journal_entry = implementation_reply_handoff_journal_entry(item.pr, handoff)
        if journal_entry is None or item.issue is None:
            item.payload["remediation_reply_error"] = True
            return
        marker, body = journal_entry
        # Persist the deterministic batch locally before the GitHub append.
        # A transient append failure must retry this host-only write, never
        # rerun the writer or create a second remediation commit.
        item.payload[PENDING_IMPLEMENTATION_REPLY_HANDOFF] = handoff
        item.payload[PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL] = {
            "marker": marker,
            "body": body,
        }
        item.payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL_RETRIES, None)
        item.payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)

    @staticmethod
    def _on_implement_done(item: WorkItem, result: JobResult) -> None:
        """Count the implement attempt and record its outcome.

        The attempt is counted on completion, success or hard failure alike
        (doc: "agent_error -> RETRY (consumes the implement budget)").

        Args:
            item: The work item to update.
            result: The implement job result.

        """
        item.attempts["implement"] = item.attempts.get("implement", 0) + 1
        if not result.ok:
            logger.warning("implementation:%s: implement job failed: %s", item.issue, result.error)
            item.payload["implement_error"] = True
            return
        item.payload.pop("post_review_rebase_required", None)
        item.payload.pop("rebase_conflict", None)
        if result.value:
            if item.payload.get("implementation_remediation"):
                item.payload["remediation_output"] = result.value
            else:
                item.payload["implement_summary"] = str(result.value)

    @staticmethod
    def _on_rebase_conflict_agent_done(item: WorkItem, result: JobResult) -> None:
        """Count one conflict-only turn without consuming implementation budget."""
        item.attempts["rebase_conflict"] = item.attempts.get("rebase_conflict", 0) + 1
        if result.ok:
            item.payload["rebase_conflict_agent_complete"] = True
        else:
            item.payload["rebase_conflict_agent_error"] = True

    @staticmethod
    def _record_rebase_conflict(item: WorkItem, result: JobResult) -> None:
        """Retain only a complete host-produced conflict receipt on the item."""
        value = result.value if isinstance(result.value, dict) else {}
        paths = value.get("conflict_paths")
        snapshot = value.get("conflict_snapshot")
        index_snapshot = value.get("conflict_index_snapshot")
        paused_head_sha = value.get("paused_head_sha")
        base_sha = value.get("base_sha")
        expected_remote_sha = value.get("expected_remote_sha")
        if (
            not isinstance(paths, (list, tuple))
            or not paths
            or not all(isinstance(path, str) and path for path in paths)
            or not isinstance(snapshot, dict)
            or not isinstance(index_snapshot, str)
            or re.fullmatch(r"[0-9a-f]{64}", index_snapshot) is None
            or not is_full_commit_sha(paused_head_sha)
            or not is_full_commit_sha(base_sha)
            or not is_full_commit_sha(expected_remote_sha)
        ):
            item.payload["rebase_error"] = True
            return
        item.payload["rebase_conflict"] = True
        item.payload["rebase_conflict_paths"] = tuple(paths)
        item.payload["rebase_conflict_snapshot"] = snapshot
        item.payload["rebase_conflict_index_snapshot"] = index_snapshot
        item.payload["rebase_paused_head_sha"] = paused_head_sha
        item.payload["rebase_base_sha"] = base_sha
        item.payload["rebase_expected_remote_sha"] = expected_remote_sha

    @staticmethod
    def _on_worktree_done(item: WorkItem, result: JobResult) -> None:
        """Record the created worktree's path and dirty snapshot.

        A failed worktree job flags ``git_error`` (transient — the
        DIRTY_DECISION_WAIT step RETRYs without burning the implement
        budget).

        Args:
            item: The work item to update.
            result: The create_worktree job result.

        """
        if not result.ok:
            logger.warning("implementation:%s: worktree job failed: %s", item.issue, result.error)
            if result.error == BRANCH_WORKTREE_OWNED:
                ownership = result.value if isinstance(result.value, dict) else {}
                item.payload["branch_worktree_owner"] = {
                    "branch": ownership.get("branch"),
                    "owner_path": ownership.get("owner_path"),
                }
                item.worktree = ""
                return
            collision = (
                result.value.get("direct_scope_reservation_collision")
                if result.error == "direct_scope_reservation_collision"
                and isinstance(result.value, dict)
                else None
            )
            if isinstance(collision, dict) and collision.get("branch") == item.branch:
                item.payload[DIRECT_SCOPE_RESERVATION_COLLISION_KEY] = True
                item.worktree = ""
                return
            materialized_path = (
                result.value.get("path")
                if isinstance(result.value, dict)
                and result.value.get(WORKTREE_MATERIALIZED_KEY) is True
                else None
            )
            if isinstance(materialized_path, str) and materialized_path:
                item.worktree = materialized_path
                item.payload[WORKTREE_MATERIALIZED_KEY] = True
            else:
                item.worktree = ""
            direct_base_sha = item.payload.get(DIRECT_SCOPE_BASE_SHA_KEY)
            requires_fresh_direct_reservation = (
                not bool(item.payload.get("existing_pr")) and direct_base_sha is not None
            )
            reservation = (
                result.value.get("direct_scope_reservation")
                if isinstance(result.value, dict)
                else None
            )
            if (
                requires_fresh_direct_reservation
                and is_full_commit_sha(direct_base_sha)
                and isinstance(reservation, dict)
                and reservation.get("branch") == item.branch
                and reservation.get("base_sha") == direct_base_sha
            ):
                # The worktree did not materialize, but a failed rollback
                # left our server-side reservation at its base. Preserve the
                # receipt so Finished can use its bounded, conditional release
                # protocol if the retriable worktree job is exhausted.
                item.payload[DIRECT_SCOPE_RESERVATION_KEY] = {
                    "branch": item.branch,
                    "base_sha": direct_base_sha,
                }
            item.payload.pop("worktree_dirty", None)
            item.payload.pop("worktree_status", None)
            item.payload.pop("worktree_diff", None)
            item.payload["git_error"] = True
            return
        # A successful worktree job ends the consecutive-git-failure streak.
        item.payload.pop(WORKTREE_MATERIALIZED_KEY, None)
        item.payload.pop("git_error_retries", None)
        value = result.value
        if isinstance(value, dict):
            item.worktree = str(value.get("path", item.worktree))
            item.payload["worktree_dirty"] = bool(value.get("dirty"))
            item.payload["worktree_status"] = str(value.get("status", ""))
            item.payload["worktree_diff"] = str(value.get("diff", ""))
            direct_base_sha = item.payload.get(DIRECT_SCOPE_BASE_SHA_KEY)
            requires_fresh_direct_reservation = (
                not bool(item.payload.get("existing_pr")) and direct_base_sha is not None
            )
            if requires_fresh_direct_reservation:
                reservation = value.get("direct_scope_reservation")
                if (
                    not is_full_commit_sha(direct_base_sha)
                    or not isinstance(reservation, dict)
                    or reservation.get("branch") != item.branch
                    or reservation.get("base_sha") != direct_base_sha
                ):
                    logger.warning(
                        "implementation:%s: direct worktree result omitted or corrupted its "
                        "remote reservation receipt",
                        item.issue,
                    )
                    item.worktree = ""
                    item.payload["git_error"] = True
                    return
                item.payload[DIRECT_SCOPE_RESERVATION_KEY] = {
                    "branch": item.branch,
                    "base_sha": direct_base_sha,
                }
        elif isinstance(value, str) and value:
            item.worktree = value

    @staticmethod
    def _on_tests_done(item: WorkItem, result: JobResult) -> None:
        """Record the pre-PR test outcome (output tail travels to the fixer).

        Args:
            item: The work item to update.
            result: The pre-PR test job result.

        """
        if result.ok and result.value in (0, None, True):
            item.payload.pop("tests_failed", None)
            item.payload.pop("test_output", None)
            command = item.payload.pop("test_command", None)
            if isinstance(command, str) and command:
                item.payload["test_receipt"] = f"`{command}` — passed"
            return
        item.payload["tests_failed"] = True
        item.payload["test_output"] = "\n".join(
            part for part in (result.stdout_tail, result.stderr_tail, result.error) if part
        )

    @staticmethod
    def _skip_gate(issue: int, labels: list[str]) -> StageOutcome | None:
        """Operator override: state:skip -> SKIP, warning on a GO contradiction.

        Split out of :meth:`_gate` so the top-of-GATE check (#1835) stays a
        single readable branch regardless of the existing-PR/fresh-implement
        logic below it.

        Args:
            issue: The GitHub issue number (for log messages).
            labels: The issue's current labels (already refreshed by caller).

        Returns:
            A SKIP outcome when ``state:skip`` is present, else None.

        """
        if not is_skipped(labels):
            return None
        if is_plan_go(labels) or is_implementation_go(labels):
            contradicting = (
                STATE_IMPLEMENTATION_GO if is_implementation_go(labels) else STATE_PLAN_GO
            )
            logger.warning(
                "implementation:%d: state:skip AND %s both present — "
                "skip wins; see docs/runbooks/state-skip-revival.md if "
                "this issue should be revived",
                issue,
                contradicting,
            )
        logger.info("implementation:%d: state:skip; skipping", issue)
        return StageOutcome(Disposition.SKIP, "state:skip")

    @staticmethod
    def _external_arm_gate(pr_number: int, ctx: StageContext) -> StageOutcome | None:
        """Block adoption when the live PR arm is external or ambiguous.

        Split out of :meth:`_gate` for the same readability reason as
        :meth:`_skip_gate`. Existing PRs may have been armed by the
        a previous auto-merge configuration; a failed read-back must stop
        adoption before worktree preparation or review routing.

        Args:
            issue: The GitHub issue number (for log messages).
            pr_number: The adopted PR's number.
            ctx: Stage context carrying the GitHub accessor.

        Returns:
            A terminal or blocked outcome unless the read proves OPEN and
            unarmed, else None.

        """
        pr_state = ctx.github.gh_pr_state(pr_number)
        if pr_state is None:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
        if pr_state.get("autoMergeRequest") is not None:
            return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        if not _is_confirmed_open_unarmed(pr_state):
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified")
        return None

    @staticmethod
    def _impl_go_route(
        item: WorkItem,
        existing_pr: int,
        pr_implementation_state: tuple[bool, bool],
    ) -> StepResult | None:
        """Route an adopted PR that already carries ``state:implementation-go``.

        Split out of :meth:`_gate` for the same readability reason as
        :meth:`_skip_gate`. The loop-owned label is the durable authorization,
        so both fresh and adopted entries route directly to ``merge_wait``.

        Args:
            item: The work item under evaluation (``item.pr``/``item.branch``
                are already set by the caller).
            existing_pr: The adopted PR's number.
            pr_implementation_state: Fresh ``(GO, NOGO)`` PR-label state
                already read by the admission gate.

        Returns:
            A routing result when the PR already carries
            ``state:implementation-go``, else None (not this PR's route).

        """
        has_go, _has_no_go = pr_implementation_state
        if not has_go:
            return None
        if item.payload.get("post_review_rebase_required"):
            return None
        logger.info(
            "implementation:%d: PR #%d already implementation-go; routing to merge-wait",
            item.issue,
            existing_pr,
        )
        return StageOutcome(Disposition.FAIL_BACK, "already_implementation_go_pr")

    @staticmethod
    def _writable_head_guard(
        item: WorkItem, ctx: StageContext, existing_pr: int
    ) -> StageOutcome | None:
        """Fail closed when an existing PR head belongs to a fork.

        Fork heads can be fetched for review, but implementation must never
        address them by creating a same-named branch on the base repository's
        origin.
        """
        if ctx.github.pr_head_is_writable(existing_pr):
            return None
        logger.warning(
            "implementation:%d: PR #%d head is not writable through this repository; "
            "refusing to address a fork from the base origin",
            item.issue,
            existing_pr,
        )
        return StageOutcome(Disposition.FINISH_FAIL, "pr_head_not_writable")

    def _adopt_existing_pr(
        self,
        item: WorkItem,
        ctx: StageContext,
        existing_pr: int,
        *,
        agent_error_reentry: bool,
        pr_implementation_state: tuple[bool, bool],
    ) -> StepResult:
        """Validate and adopt an existing writable PR for normal review."""
        item.pr = existing_pr
        terminal = _terminal_pr_outcome(ctx.github.gh_pr_state(existing_pr), existing_pr)
        if terminal is not None:
            return terminal
        external_arm = self._external_arm_gate(existing_pr, ctx)
        if external_arm is not None:
            return external_arm
        head_branch = ctx.github.get_pr_head_branch(existing_pr)
        if not isinstance(head_branch, str) or not head_branch.strip():
            return StageOutcome(Disposition.FINISH_FAIL, "pr_head_branch_unavailable")
        head_branch = head_branch.strip()
        item.branch = head_branch
        impl_go_route = self._impl_go_route(item, existing_pr, pr_implementation_state)
        if impl_go_route is not None:
            return impl_go_route
        writable_head = self._writable_head_guard(item, ctx, existing_pr)
        if writable_head is not None:
            return writable_head
        if agent_error_reentry:
            # M1: consume the implement budget at GATE-adoption so the
            # pr_review agent_error -> re-adopt cycle is bounded.
            attempts = item.attempts.get("implement", 0) + 1
            item.attempts["implement"] = attempts
            budget = ctx.budget("implement")
            if attempts >= budget:
                logger.error(
                    "implementation:%d: agent_error fail-backs exhausted the "
                    "implement budget (%d/%d) re-adopting PR #%d — stopping; "
                    "the review/address infrastructure failed repeatedly and "
                    "re-adopting the same PR cannot fix it (manual look needed)",
                    item.issue,
                    attempts,
                    budget,
                    existing_pr,
                )
                return StageOutcome(Disposition.FINISH_FAIL, "agent_error_exhausted")
        # Adopt the PR's REAL head branch — never assume {issue}-auto-impl.
        item.payload["existing_pr"] = True
        logger.info(
            "implementation:%d: existing PR #%d (branch %r); preparing adopted worktree",
            item.issue,
            existing_pr,
            item.branch,
        )
        return Continue(next_state=WORKTREE_WAIT)

    def _gate(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """GATE [M]: existing-PR fast path, then the plan-review verdict gate.

        Re-houses ``_review_existing_pr`` (:750) and ``_ensure_plan_ready``
        (:429). All checks are at-or-past reads; PR adoption is read-only.

        agent_error bound (M1): a re-entry from a pr_review ``agent_error``
        fail-back (``payload["agent_error_failback"]``) that adopts an
        existing PR CONSUMES the ``implement`` budget — the adoption produces
        no implement job whose completion would otherwise count it, and
        without a moving counter the fail-back -> adopt -> ADVANCE cycle
        would ping-pong forever. Exhaustion terminates with
        ``agent_error_exhausted``.
        """
        if item.issue is None:  # guarded by step(); kept for type narrowing
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")

        # Operator override: state:skip -> SKIP, checked before either the
        # existing-PR fast path or the fresh-implement plan-go gate (#1835 —
        # the existing-PR path previously adopted PRs unconditionally with no
        # label read at all; closes the reachable gap even though the 11
        # incidents that raised #1835 were all confirmed skip-after-PR races,
        # not this chokepoint).
        gate_labels = _require_issue_labels(item, ctx)
        skip_outcome = self._skip_gate(item.issue, gate_labels)
        if skip_outcome is not None:
            return skip_outcome
        if STATE_PLAN_BLOCKED in gate_labels:
            return StageOutcome(
                Disposition.BLOCKED,
                "plan is blocked pending external intervention",
            )

        # Pop the fail-back marker unconditionally: on the fresh-implement
        # path below the budget is consumed by the implement job itself, so
        # the marker must never survive into a later GATE pass.
        agent_error_reentry = bool(item.payload.pop("agent_error_failback", None))

        existing_pr = item.pr or ctx.github.find_pr_for_issue(item.issue)
        if existing_pr:
            terminal = _terminal_pr_outcome(ctx.github.gh_pr_state(existing_pr), existing_pr)
            if terminal is not None:
                return terminal
            pr_implementation_state = ctx.github.pr_has_implementation_state_label(existing_pr)
            has_impl_go, has_impl_no_go = pr_implementation_state
            if has_impl_go and has_impl_no_go:
                return StageOutcome(
                    Disposition.FINISH_FAIL,
                    "contradictory_implementation_state",
                )
            if not (is_plan_go(gate_labels) or has_impl_go or has_impl_no_go):
                logger.info(
                    "implementation:%d: existing PR #%d lacks an authoritative "
                    "plan/implementation label; failing back",
                    item.issue,
                    existing_pr,
                )
                return StageOutcome(Disposition.FAIL_BACK, "plan_not_go")
            return self._adopt_existing_pr(
                item,
                ctx,
                existing_pr,
                agent_error_reentry=agent_error_reentry,
                pr_implementation_state=pr_implementation_state,
            )

        # At-or-past (never equality): plan-go OR already implementation-go
        # both satisfy the gate; anything earlier fails back to plan_review.
        if not (is_plan_go(gate_labels) or is_implementation_go(gate_labels)):
            logger.info("implementation:%d: plan not GO; failing back", item.issue)
            return StageOutcome(Disposition.FAIL_BACK, "plan_not_go")

        if not item.branch:
            item.branch = issue_auto_impl_branch_name(item.issue)
        return Continue(next_state=WORKTREE_WAIT)

    def _create_pr(  # noqa: C901
        self, item: WorkItem, ctx: StageContext
    ) -> StepResult:
        """PR_CREATE [M]: create the durable PR journal entry for review.

        The ``create_pr`` write is the stage's journal entry and happens
        BEFORE the advancing outcome (durable write precedes the queue push).
        """
        if item.issue is None:  # guarded by step(); kept for type narrowing
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
        if item.payload.pop("remediation_reply_error", None):
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_failed")

        append_result = item.payload.pop(_REPLY_JOURNAL_APPEND_RESULT, None)
        if append_result == "retry":
            item.state = REPLY_JOURNAL_APPEND_WAIT
            return StageOutcome(
                Disposition.RETRY,
                "implementation_reply_handoff_journal_retry",
            )
        if append_result in {"failed", "invalid"}:
            return StageOutcome(
                Disposition.FINISH_FAIL,
                "implementation_reply_handoff_journal_failed"
                if append_result == "failed"
                else "implementation_reply_handoff_journal_invalid",
            )
        if item.payload.get(PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL) is not None:
            return Continue(next_state=REPLY_JOURNAL_APPEND_WAIT)

        handoff_result = item.payload.pop(_REPLY_HANDOFF_RESULT, None)
        if handoff_result == "visibility_wait":
            item.state = REPLY_HANDOFF_WAIT
            return StageOutcome(
                Disposition.RETRY,
                "implementation_reply_handoff_visibility_wait",
            )
        if handoff_result == "retry":
            item.state = REPLY_HANDOFF_WAIT
            return StageOutcome(Disposition.RETRY, "implementation_reply_handoff_retry")
        if handoff_result == "blocked":
            item.payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
            item.payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
            item.payload.pop("implementation_remediation", None)
            item.payload.pop("remediation_output", None)
            return StageOutcome(Disposition.ADVANCE, "implementation_reply_handoff_blocked")
        if handoff_result in {"failed", "invalid"}:
            return StageOutcome(
                Disposition.FINISH_FAIL,
                "implementation_reply_handoff_failed"
                if handoff_result == "failed"
                else "implementation_reply_handoff_invalid",
            )
        if handoff_result == "stale":
            item.payload.pop("implementation_remediation", None)
            item.payload.pop("remediation_output", None)
            return StageOutcome(
                Disposition.ADVANCE,
                f"PR #{item.pr} ready for fresh review after stale reply handoff",
            )
        if handoff_result == "completed":
            item.payload.pop("implementation_remediation", None)
            item.payload.pop("remediation_output", None)
        if item.payload.get(PENDING_IMPLEMENTATION_REPLY_HANDOFF) is not None:
            return Continue(next_state=REPLY_HANDOFF_WAIT)

        if item.payload.get("no_commits"):
            # An item can retain a PR after an interrupted or re-entered
            # implementation attempt.  Do not add an issue-level skip label
            # if that live PR is now externally armed (or if its state cannot
            # be proved complete and unarmed): the label would otherwise
            # mutate workflow state owned by another actor.
            if item.pr is not None:
                external_arm = self._external_arm_gate(item.pr, ctx)
                if external_arm is not None:
                    return external_arm
            item.payload.pop("no_commits", None)
            logger.warning(
                "implementation:%d: no commits vs base; applying %s", item.issue, STATE_SKIP
            )
            write_skip_label(
                item.issue,
                ctx,
                "the implementation session ended with no commits versus the "
                "base branch — there is nothing to open a PR from. Re-scope "
                "the issue or implement manually, then remove this label to "
                "re-enter the loop.",
            )
            return StageOutcome(Disposition.SKIP, "no commits vs base")
        if item.payload.pop("git_error", None):
            # Push failed: transient git/network trouble — RETRY the stage
            # without burning the implement budget, bounded by
            # GIT_ERROR_RETRY_CAP (M5).
            outcome = self._git_retry(item, "commit_push failed")
            if outcome.disposition is Disposition.RETRY:
                item.state = COMMIT_PUSH_WAIT
            return outcome

        if item.pr is None:
            raw_title = item.payload.get("issue_title") or f"Implement issue #{item.issue}"
            title = normalize_strict_conventional_title(str(raw_title))
            body = get_pr_description(
                item.issue,
                # Agent summaries may contain local paths, stale working-tree
                # state, or unbound test totals. The durable PR journal must
                # be deterministic; the reviewed diff carries the detail.
                summary=f"Implements the requested changes for issue #{item.issue}.",
                changes="See the PR diff for the full change set.",
                testing=item.payload.get("test_receipt") or "Not run by the automation pipeline.",
            )
            pr_number = ctx.github.create_pr(item.issue, item.branch, title, body)
            item.pr = pr_number
            logger.info("implementation:%d: created PR #%d", item.issue, pr_number)
        return StageOutcome(Disposition.ADVANCE, f"PR #{item.pr} ready for review")

    @staticmethod
    def _git_retry(item: WorkItem, note: str) -> StageOutcome:
        """RETRY a transient git failure, bounded by GIT_ERROR_RETRY_CAP (M5).

        Transient worktree/push failures never burn the implement budget,
        but a persistently failing remote must still terminate: at the cap
        the item finishes failed (``git_error``). The consecutive-failure
        counter lives in ``payload["git_error_retries"]`` and is reset by
        any successful git job (see ``on_job_done``).

        Args:
            item: The work item whose git job failed.
            note: Human-readable failure note for the RETRY outcome.

        Returns:
            RETRY below the cap; FINISH_FAIL(``git_error``) at the cap.

        """
        retries = item.payload.get("git_error_retries", 0) + 1
        item.payload["git_error_retries"] = retries
        if retries > GIT_ERROR_RETRY_CAP:
            logger.error(
                "implementation:%s: %s; %d consecutive git failures (cap %d) — "
                "finishing failed (git_error): the remote/worktree is persistently "
                "broken and needs a manual look",
                item.issue,
                note,
                retries,
                GIT_ERROR_RETRY_CAP,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "git_error")
        logger.warning(
            "implementation:%s: %s; git retry %d/%d (implement budget untouched)",
            item.issue,
            note,
            retries,
            GIT_ERROR_RETRY_CAP,
        )
        return StageOutcome(Disposition.RETRY, note)


def _rebase_failure_diagnostic(result: JobResult) -> dict[str, object] | None:
    """Extract bounded rebase-recovery evidence from a host Git result."""
    value = result.value
    if not isinstance(value, dict):
        return None
    if value.get("failure_kind") not in {"signing", "continuation"}:
        return None
    if value.get("phase") not in {"stage_conflicts", "validate_index", "rebase_continue"}:
        return None
    return {
        "failure_kind": value["failure_kind"],
        "phase": value["phase"],
        "returncode": value.get("returncode"),
        "receipt_error": value.get("receipt_error"),
        "stdout_tail": result.stdout_tail,
        "stderr_tail": result.stderr_tail,
    }
