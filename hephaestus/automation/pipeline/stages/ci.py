"""CI drive-green stage: discover, rebase, poll non-blocking, fix, push.

Re-houses the CI half of ``ci_driver._drive_issue`` (:710) — minus the
merge-wait, which is :mod:`.merge_wait` — as a pipeline stage
(docs/AUTOMATION_LOOP_ARCHITECTURE.md section "6. ci" is the binding
contract):

- States: ENTER -> DISCOVER -> REBASE_WAIT -> POLL -> FIX_WAIT ->
  PUSH_WAIT -> (POLL). Budgets: ``ci_fix`` = 1 (max fix attempts; one
  extra escalation via force_engagement), ``rebase`` = 2. Both read from
  ROUTES via ``ctx.budget``, never hardcoded here.
- DISCOVER [M]: resolve the PR when unset (``ctx.github.find_pr_for_issue``,
  the ``pr_discovery`` semantics) — none found finishes failed ``no_pr``;
  adopt the PR's REAL head branch; capture the PR's real base ref
  (``baseRefName`` from the same ``gh_pr_state`` read) into
  ``payload["base_branch"]`` for REBASE_WAIT, mirroring merge_wait's
  ``_route_dirty``; verify the PR carries ``state:implementation-go``
  (``ctx.github.pr_has_implementation_state_label``, the legacy
  ``_pr_has_implementation_go`` gate) — a PR without it fails back
  ``not_implementation_go`` (routes to pr_review).
- REBASE_WAIT [W:G]: optional mechanical rebase (re-housed
  ``_attempt_mechanical_rebase`` :763 — the worker pool's ``op="rebase"``
  is ``git_utils.rebase_worktree_onto``), best-effort: skipped on dry-run,
  when ``enable_mechanical_rebase`` is off, or once the ``rebase`` budget
  (consumed in ``on_job_done``) is spent. POLL absorbs either outcome.
- POLL [M], non-blocking: one ``ctx.github.pr_checks`` read classified by
  the pure :func:`~hephaestus.automation.ci_run_coordinator.classify_ci_state`
  (the sleep-free extraction of ``poll_ci_until_concluded``'s conclusion
  logic). PENDING timer-parks: the backoff delay (legacy ``min(2**n, 60)``)
  is recorded in ``payload["retry_delay_s"]`` and the stage returns
  ``StageOutcome(RETRY)`` — the coordinator (#1817) consumes the delay from
  the payload (see the base.py coordinator convention; ``StageOutcome`` has
  no delay field). The PENDING window is capped by elapsed wall-clock via
  ``ctx.now()`` against ``payload["ci_poll_started_at"]`` and the configured
  ``poll_max_wait`` budget -> FINISH_FAIL(``timeout``). GREEN / NO_CHECKS
  ADVANCE (no CI configured is the legacy success case); FAILING enters the
  fix leg while the ``ci_fix`` budget remains, else fails back ``fix_exhausted`` (routes to
  implementation).
- FIX_WAIT [W:A]: the CI-fix agent session. The prompt is composed
  in-worker by :func:`build_ci_fix_prompt`, which reuses
  ``CIFixOrchestrator.build_ci_fix_prompt`` (:498) verbatim; the
  no-commit escalation retry reuses
  ``CIFixOrchestrator.force_engagement_prompt`` (:148) verbatim via
  :func:`build_force_engagement_prompt` (legacy ``_retry_no_commit_once``
  semantics: ONE forced re-engagement after a turn that produced no
  commit, then exhaustion). CI logs / review-thread block / failing check
  names are seeded into ``item.payload`` by the coordinator (#1817), which
  owns those gh reads.
- PUSH_WAIT [W:G]: commit+push the fix (worker ``op="commit_push"`` —
  the push contract owns commit+lease). ``on_job_done`` inspects the
  result: a hard push failure fails back ``fix_exhausted`` at the next
  POLL; a push that produced NO commit triggers the single
  force-engagement escalation; a real commit restarts the POLL backoff
  window (fresh CI run, mirroring the legacy post-fix re-poll).
- on_job_done consumes the budgets (``rebase``, ``ci_fix`` — counted on
  completion, success or hard failure alike, the sibling implementation
  pattern) and records result flags on ``item.payload``; it never writes
  ``item.state`` (the coordinator advances to ``on_done_state`` after it
  returns).
- Owned labels: none (ci state is reflected in the PR check conclusion).
- Zero ``time.sleep`` / ``import time`` in this module (AC1) — enforced by
  ``tests/unit/automation/pipeline/test_pipeline_architecture.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from hephaestus.automation.agent_config import (
    DEFAULT_CI_POLL_MAX_WAIT,
    ci_driver_claude_timeout,
    implementer_model,
)
from hephaestus.automation.ci_fix_orchestrator import CIFixOrchestrator
from hephaestus.automation.ci_run_coordinator import CiConclusion, classify_ci_state
from hephaestus.automation.session_naming import AGENT_CI_DRIVER

from .base import (
    BACKOFF_CAP_S as _BACKOFF_CAP_S,
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
    _build_rebase_job,
    _require_item_worktree,
    _terminal_pr_outcome,
    _worktree_path,
    agent_provider,
    stage_model,
)

logger = logging.getLogger(__name__)

BACKOFF_CAP_S = _BACKOFF_CAP_S

# In-memory mini-states (stage-local strings, never GitHub labels).
ENTER = "ENTER"
DISCOVER = "DISCOVER"
REBASE_WAIT = "REBASE_WAIT"
POLL = "POLL"
FIX_WAIT = "FIX_WAIT"
PUSH_WAIT = "PUSH_WAIT"

#: Historical number of capped-backoff parks that approximately consumed the
#: default 1200s CI poll budget. Kept as a compatibility constant for callers
#: and tests; timeout enforcement is wall-clock based via ``ctx.now()``.
POLL_DEADLINE = 25

CI_POLL_STARTED_AT = "ci_poll_started_at"


def build_ci_fix_prompt(
    issue_number: int,
    pr_number: int,
    worktree_path: str,
    ci_logs: str,
    pr_head_branch: str,
    advise_findings: str = "",
    review_threads_block: str = "",
    failing_check_names: tuple[str, ...] = (),
) -> str:
    """Compose the CI-fix agent prompt, reusing the orchestrator template verbatim.

    Module-level composed builder (NOT a closure): :class:`AgentJob` is frozen
    and prompt builders run in-worker, so the builder must be a top-level
    function receiving everything via ``prompt_kwargs`` (mirrors
    :func:`..planning.build_plan_prompt` / :func:`..implementation.build_implementation_prompt`).

    :meth:`CIFixOrchestrator.build_ci_fix_prompt` is reused verbatim rather than
    re-authored. Its only orchestrator-state reads are the two gh-derived blocks
    (``_format_review_threads_block`` and ``_failing_required_check_names``);
    the coordinator (#1817) owns those gh reads and seeds them into
    ``item.payload``, so this wrapper injects them as inert providers and leaves
    the remaining providers unused (the method never touches them).

    Args:
        issue_number: GitHub issue number under CI fix.
        pr_number: GitHub PR number whose CI is failing.
        worktree_path: Worktree the CI-fix agent works in.
        ci_logs: Combined CI failure log text.
        pr_head_branch: The PR's real head branch (the agent must not switch off it).
        advise_findings: Prior learnings prepended to the prompt; empty skips the block.
        review_threads_block: Pre-rendered unresolved-review-threads block (coordinator-seeded).
        failing_check_names: Names of the failing required checks (coordinator-seeded).

    Returns:
        The full CI-fix prompt string.

    """
    return _inert_orchestrator(
        worktree_path, pr_head_branch, review_threads_block, failing_check_names
    ).build_ci_fix_prompt(
        issue_number=issue_number,
        pr_number=pr_number,
        worktree_path=Path(worktree_path),
        ci_logs=ci_logs,
        pr_head_branch=pr_head_branch,
        advise_findings=advise_findings,
    )


def build_force_engagement_prompt(
    issue_number: int,
    pr_number: int,
    worktree_path: str,
    pr_head_branch: str,
    review_threads_block: str = "",
    failing_check_names: tuple[str, ...] = (),
) -> str:
    """Compose the no-commit escalation prompt, reusing the orchestrator verbatim.

    Re-houses the legacy ``_retry_no_commit_once`` escalation (#846):
    when a CI-fix turn returns WITHOUT committing, the ONE retry re-engages
    the agent with :meth:`CIFixOrchestrator.force_engagement_prompt` — the
    failing checks named verbatim, the branch invariant re-emphasised, and
    the ``BLOCKED:`` escape hatch. Module-level composed builder for the
    same frozen-``AgentJob`` reason as :func:`build_ci_fix_prompt`.

    Args:
        issue_number: GitHub issue number under CI fix.
        pr_number: GitHub PR number whose CI fix produced no commit.
        worktree_path: Worktree the CI-fix agent works in.
        pr_head_branch: The PR's real head branch (the agent must not switch off it).
        review_threads_block: Pre-rendered unresolved-review-threads block (coordinator-seeded).
        failing_check_names: Names of the failing required checks (coordinator-seeded).

    Returns:
        The full force-engagement retry prompt string.

    """
    return _inert_orchestrator(
        worktree_path, pr_head_branch, review_threads_block, failing_check_names
    ).force_engagement_prompt(
        issue_number=issue_number,
        pr_number=pr_number,
        worktree_path=Path(worktree_path),
        pr_head_branch=pr_head_branch,
        failing_check_names=list(failing_check_names),
        review_threads_block=review_threads_block,
    )


def _inert_orchestrator(
    worktree_path: str,
    pr_head_branch: str,
    review_threads_block: str,
    failing_check_names: tuple[str, ...],
) -> CIFixOrchestrator:
    """Build a CIFixOrchestrator whose providers replay coordinator-seeded data.

    The two prompt methods reused above only consult the gh-derived
    providers (``format_review_threads_block`` /
    ``failing_required_check_names``) and the worktree/branch resolvers;
    everything else is inert and never touched by prompt composition.
    """
    checks = tuple(failing_check_names)
    return CIFixOrchestrator(
        options_provider=lambda: None,
        repo_root_provider=Path.cwd,
        state_dir_provider=Path.cwd,
        status_tracker_provider=lambda: None,
        get_pr_branch=lambda _pr: pr_head_branch,
        get_worktree_path=lambda _issue, _pr: Path(worktree_path),
        format_review_threads_block=lambda _pr: review_threads_block,
        failing_required_check_names=lambda _pr: list(checks),
    )


class CiStage(Stage):
    """Stage: discover PR, mechanical rebase, poll, agent fix, push, re-poll."""

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Initialize the mini-state; all entry checks live in DISCOVER.

        The doc's entry step (PR discovery + the implementation-go verify)
        is the DISCOVER mini-state, an [M] step of this stage — so a restart
        re-runs it idempotently via step(). Nothing durable is written here.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            None (always proceed to step()).

        """
        if not item.state:
            item.state = ENTER
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Execute the next CI action for the item's current state.

        Args:
            item: The work item with current state.
            ctx: Stage context.

        Returns:
            Continue, JobRequest, or StageOutcome.

        """
        if item.state == ENTER:
            return Continue(next_state=DISCOVER)
        if item.state == DISCOVER:
            return self._discover(item, ctx)
        if item.state == REBASE_WAIT:
            return self._request_rebase(item, ctx)
        if item.state == POLL:
            return self._poll(item, ctx)
        if item.state == FIX_WAIT:
            return self._request_fix(item, ctx)
        if item.state == PUSH_WAIT:
            return self._request_push(item, ctx)
        logger.warning("ci:%s: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    def _discover(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """DISCOVER [M]: resolve the PR, adopt its branch, verify implementation-go.

        Re-houses ``_drive_issue``'s entry facts: the drive needs an open PR
        (``pr_discovery`` semantics via ``ctx.github.find_pr_for_issue``) and
        the PR must already carry ``state:implementation-go`` (the legacy
        ``_pr_has_implementation_go`` gate) — a PR that lost or never had it
        regresses to pr_review (``not_implementation_go``), never arms. The
        PR's real base ref is captured into ``payload["base_branch"]`` from
        the same ``gh_pr_state`` read (mirrors ``merge_wait._route_dirty``'s
        ``baseRefName`` capture), so REBASE_WAIT targets the PR's actual
        base instead of a hardcoded ``"main"``.
        """
        if item.pr is None:
            if item.issue is None:
                logger.warning("ci: item has neither PR nor issue; finishing failed")
                return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
            discovered = ctx.github.find_pr_for_issue(item.issue)
            if discovered is None:
                logger.info("ci:%d: no open PR found; finishing failed", item.issue)
                return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
            item.pr = discovered
        gh_state = ctx.github.gh_pr_state(item.pr)
        terminal = _terminal_pr_outcome(gh_state, item.pr)
        if terminal is not None:
            return terminal
        if not item.branch:
            # Adopt the PR's REAL head branch — never assume {issue}-auto-impl
            # (the _review_existing_pr branch-assumption bug).
            head_branch = ctx.github.get_pr_head_branch(item.pr)
            if head_branch:
                item.branch = head_branch
        # Capture the PR's real base ref for the mechanical rebase target
        # (mirrors merge_wait._route_dirty's baseRefName read).
        item.payload["base_branch"] = str((gh_state or {}).get("baseRefName") or "main")
        has_go, _has_no_go = ctx.github.pr_has_implementation_state_label(item.pr)
        if not has_go:
            logger.info(
                "ci:%s: PR #%d lacks state:implementation-go; regressing to pr_review",
                item.issue,
                item.pr,
            )
            return StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")
        return Continue(next_state=REBASE_WAIT)

    def _request_rebase(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """REBASE_WAIT [W:G]: best-effort mechanical rebase, then POLL.

        Mirrors the ``_drive_issue`` gate (``enable_mechanical_rebase and not
        dry_run``); the ``rebase`` budget (consumed in ``on_job_done``) bounds
        the attempts across the item's lifetime. Skipping (option off,
        dry-run, or budget spent) is never an error — POLL classifies the PR
        as it stands.
        """
        if ctx.dry_run or not getattr(ctx.config, "enable_mechanical_rebase", True):
            return Continue(next_state=POLL)
        if item.attempts.get("rebase", 0) >= ctx.budget("rebase"):
            logger.info("ci:%s: rebase budget spent; polling as-is", item.issue)
            return Continue(next_state=POLL)
        missing_worktree = _require_item_worktree(item, "ci", "mechanical rebase")
        if missing_worktree is not None:
            return missing_worktree
        rebase_job = _build_rebase_job(item, ctx, descr="mechanical_rebase")
        return JobRequest(rebase_job, on_done_state=POLL)

    def _poll(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """POLL [M]: one non-blocking check read -> classify -> route.

        POLL is also the ``on_done_state`` of both worker legs, so it first
        consumes the flags ``on_job_done`` recorded:

        - ``push_failed`` (hard push failure — lost lease / broken remote):
          the head never advanced, so re-polling would spin on the same red
          checks; fail back ``fix_exhausted`` (the legacy terminal for a fix
          that could not land).
        - ``push_no_commit`` (the fix turn produced NO commit): the ONE
          force-engagement escalation re-enters FIX_WAIT (legacy
          ``_retry_no_commit_once`` #846); a second no-commit turn is
          exhaustion.

        Then the pure classifier routes: PENDING timer-parks (RETRY with the
        backoff delay in ``payload["retry_delay_s"]``, elapsed wall-clock
        capped by ``poll_max_wait``); GREEN / NO_CHECKS ADVANCE; FAILING
        enters the fix leg while the ``ci_fix`` budget remains.
        """
        if item.payload.pop("push_failed", None):
            logger.warning("ci:%s: fix push failed; failing back", item.issue)
            return StageOutcome(Disposition.FAIL_BACK, "fix_exhausted")
        if item.payload.pop("push_no_commit", None):
            if not item.payload.get("force_engagement_done"):
                # ONE escalation: re-engage the agent head-on (#846).
                item.payload["force_engagement_done"] = True
                item.payload["force_engagement"] = True
                logger.warning(
                    "ci:%s: fix turn produced no commit; force-engagement retry",
                    item.issue,
                )
                return Continue(next_state=FIX_WAIT)
            logger.warning(
                "ci:%s: force-engagement retry still produced no commit; failing back",
                item.issue,
            )
            return StageOutcome(Disposition.FAIL_BACK, "fix_exhausted")
        if item.pr is None:  # guarded by DISCOVER; kept for restart safety
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")

        conclusion = classify_ci_state(ctx.github.pr_checks(item.pr))
        if conclusion is CiConclusion.PENDING:
            now = ctx.now()
            started = item.payload.get(CI_POLL_STARTED_AT)
            if started is None:
                started = now
                item.payload[CI_POLL_STARTED_AT] = started
            max_wait = _ci_poll_max_wait(ctx)
            elapsed = now - float(started)
            if elapsed > max_wait:
                logger.warning(
                    "ci:%s: CI still pending after %ds (limit %ds); timing out",
                    item.issue,
                    int(elapsed),
                    max_wait,
                )
                return StageOutcome(Disposition.FINISH_FAIL, "timeout")
            polls = item.payload.get("ci_poll_count", 0)
            delay = min(2**polls, BACKOFF_CAP_S)
            item.payload["ci_poll_count"] = polls + 1
            # Timer-park contract (base.py): the coordinator (#1817) reads
            # the delay from the payload — StageOutcome has no delay field.
            item.payload["retry_delay_s"] = delay
            return StageOutcome(Disposition.RETRY, "ci_pending")
        if conclusion in (CiConclusion.GREEN, CiConclusion.NO_CHECKS):
            # NO_CHECKS is the legacy "no CI configured" success case.
            return StageOutcome(Disposition.ADVANCE, conclusion.value)
        # FAILING: enter the fix leg while budget remains.
        if item.attempts.get("ci_fix", 0) < ctx.budget("ci_fix"):
            return Continue(next_state=FIX_WAIT)
        logger.warning("ci:%s: ci_fix budget exhausted; failing back", item.issue)
        return StageOutcome(Disposition.FAIL_BACK, "fix_exhausted")

    def _request_fix(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """FIX_WAIT [W:A]: dispatch the CI-fix (or force-engagement) session.

        The prompt is composed in-worker by :func:`build_ci_fix_prompt` — or
        :func:`build_force_engagement_prompt` on the one no-commit escalation
        (``payload["force_engagement"]``, set by POLL). CI logs, the rendered
        review-threads block, and the failing-check names are seeded into
        ``item.payload`` by the coordinator (#1817), which owns those gh
        reads. Stale result flags are cleared at submission so a failed later
        attempt can never replay an earlier attempt's outcome.
        """
        item.payload.pop("ci_fix_failed", None)
        missing_worktree = _require_item_worktree(item, "ci", "CI fix")
        if missing_worktree is not None:
            return missing_worktree
        escalate = bool(item.payload.pop("force_engagement", None))
        prompt_builder: Callable[..., str]
        common_kwargs = {
            "issue_number": item.issue if item.issue is not None else 0,
            "pr_number": item.pr,
            "worktree_path": str(_worktree_path(item, ctx)),
            "pr_head_branch": item.branch,
            "review_threads_block": item.payload.get("review_threads_block", ""),
            "failing_check_names": tuple(item.payload.get("failing_check_names") or ()),
        }
        if escalate:
            prompt_builder = build_force_engagement_prompt
            prompt_kwargs = common_kwargs
        else:
            prompt_builder = build_ci_fix_prompt
            prompt_kwargs = {
                **common_kwargs,
                "ci_logs": item.payload.get("ci_logs", ""),
                "advise_findings": item.payload.get("advise_findings", ""),
            }
        job = AgentJob(
            repo=item.repo,
            issue=item.issue if item.issue is not None else 0,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "implementer", implementer_model),
            prompt_builder=prompt_builder,
            cwd=_worktree_path(item, ctx),
            timeout_s=ci_driver_claude_timeout(),
            session_agent=AGENT_CI_DRIVER,
            prompt_kwargs=prompt_kwargs,
            descr="force_engagement" if escalate else "ci_fix",
        )
        return JobRequest(job, on_done_state=PUSH_WAIT)

    def _request_push(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """PUSH_WAIT [W:G]: commit+push the fix, or reroute a dead fix turn.

        PUSH_WAIT is ``FIX_WAIT``'s ``on_done_state``: a hard-failed fix job
        (``payload["ci_fix_failed"]``) means there is nothing to push, so
        reroute to POLL — its ``ci_fix`` budget gate (the attempt WAS counted
        in ``on_job_done``) classifies FAIL_BACK once the budget is spent.
        Otherwise the worker ``commit_push`` op commits whatever the agent
        left and pushes the branch; its result value reports whether a real
        commit was produced (the #1575 real-commit gate).
        """
        if item.payload.pop("ci_fix_failed", None):
            return Continue(next_state=POLL)
        missing_worktree = _require_item_worktree(item, "ci", "CI fix push")
        if missing_worktree is not None:
            return missing_worktree
        push_job = GitJob(
            repo=item.repo,
            op="commit_push",
            timeout_s=GIT_JOB_TIMEOUT_S,
            kwargs={
                "issue_number": item.issue if item.issue is not None else 0,
                "worktree_path": _worktree_path(item, ctx),
                "branch": item.branch,
                "agent": AGENT_CI_DRIVER,
            },
            descr="push_ci_fix",
        )
        return JobRequest(push_job, on_done_state=POLL)

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Consume budgets and record result flags (state is still the WAIT state).

        The coordinator contract (:mod:`.base`): ``item.state`` is still the
        WAIT state that submitted the job, and the coordinator advances it to
        ``on_done_state`` AFTER this returns — so routing decisions are
        recorded as ``item.payload`` flags the target state's ``step()``
        consumes, never as ``item.state`` writes. Budgets are consumed HERE,
        on completion, success or hard failure alike (the sibling
        implementation pattern: an interrupt never burns budget because
        interrupted results never reach this method).

        - ``REBASE_WAIT``: count ``rebase``; best-effort — POLL re-classifies
          unconditionally (a clean rebase re-triggers CI; a conflicting one
          shows up as FAILING/DIRTY downstream).
        - ``FIX_WAIT``: count ``ci_fix``; a hard job failure flags
          ``ci_fix_failed`` so PUSH_WAIT reroutes to POLL instead of pushing
          a fix that never landed.
        - ``PUSH_WAIT``: a hard failure flags ``push_failed`` (POLL fails
          back — the head never advanced); a no-commit push flags
          ``push_no_commit`` (POLL runs the one force-engagement
          escalation); a real commit resets the POLL backoff window (fresh
          CI run, mirroring the legacy post-fix re-poll).

        Args:
            item: The work item whose job completed.
            result: The job result from the worker pool.
            ctx: Stage context.

        """
        if item.state == REBASE_WAIT:
            item.attempts["rebase"] = item.attempts.get("rebase", 0) + 1
            if not result.ok:
                logger.warning(
                    "ci:%s: mechanical rebase failed (non-fatal): %s", item.issue, result.error
                )
            return
        if item.state == FIX_WAIT:
            item.attempts["ci_fix"] = item.attempts.get("ci_fix", 0) + 1
            if not result.ok:
                logger.warning("ci:%s: CI-fix job failed: %s", item.issue, result.error)
                item.payload["ci_fix_failed"] = True
            return
        if item.state == PUSH_WAIT:
            if not result.ok:
                logger.warning("ci:%s: fix push failed: %s", item.issue, result.error)
                item.payload["push_failed"] = True
            elif not result.value:
                # commit_push reports whether a commit was actually produced.
                item.payload["push_no_commit"] = True
            else:
                # Real commit pushed: CI restarts, so the poll backoff does too.
                item.payload.pop(CI_POLL_STARTED_AT, None)
                item.payload.pop("ci_poll_count", None)
                item.payload.pop("retry_delay_s", None)
            return


def _ci_poll_max_wait(ctx: StageContext) -> int:
    """Return the wall-clock CI poll budget in seconds."""
    value = getattr(ctx.config, "poll_max_wait", DEFAULT_CI_POLL_MAX_WAIT)
    if value is None:
        return DEFAULT_CI_POLL_MAX_WAIT
    return int(value)
