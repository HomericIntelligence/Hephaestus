"""CI drive-green pipeline stage (issue #1816).

Non-blocking re-housing of ci_driver._drive_issue (minus merge-wait). Every
poll returns RETRY(delay_s) instead of sleeping — the coordinator timer heap
owns the wait. NO time.sleep / import time in this module (AC1).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from hephaestus.automation.agent_config import ci_driver_claude_timeout
from hephaestus.automation.ci_fix_orchestrator import CIFixOrchestrator
from hephaestus.automation.ci_run_coordinator import CiConclusion, classify_ci_state
from hephaestus.automation.claude_models import implementer_model
from hephaestus.automation.session_naming import AGENT_CI_DRIVER

from .base import (
    GIT_JOB_TIMEOUT_S,
    AgentJob,
    Continue,
    Disposition,
    GitJob,
    JobRequest,
    Stage,
    StageContext,
    StageOutcome,
    _worktree_path,
)

if TYPE_CHECKING:
    from hephaestus.automation.pipeline.work_item import WorkItem

_BACKOFF_CAP = 60  # matches ci_run_coordinator poll backoff min(2**n, 60)


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
    checks = tuple(failing_check_names)
    orchestrator = CIFixOrchestrator(
        options_provider=lambda: None,
        repo_root_provider=Path.cwd,
        state_dir_provider=Path.cwd,
        status_tracker_provider=lambda: None,
        get_pr_branch=lambda _pr: pr_head_branch,
        get_worktree_path=lambda _issue, _pr: Path(worktree_path),
        format_review_threads_block=lambda _pr: review_threads_block,
        failing_required_check_names=lambda _pr: list(checks),
    )
    return orchestrator.build_ci_fix_prompt(
        issue_number=issue_number,
        pr_number=pr_number,
        worktree_path=Path(worktree_path),
        ci_logs=ci_logs,
        pr_head_branch=pr_head_branch,
        advise_findings=advise_findings,
    )


class CiStage(Stage):
    """Pipeline stage for CI drive-green."""

    name = "ci"

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Idempotent entry: refresh labels, initialize state if needed."""
        if not item.state:
            item.state = "DISCOVER"
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> Continue | JobRequest | StageOutcome:
        """Execute the next action for the item's current state."""
        if item.state == "DISCOVER":
            return self._discover(item, ctx)
        if item.state == "REBASE_READY":
            return self._request_rebase(item, ctx)
        if item.state == "POLL":
            return self._poll(item, ctx)
        if item.state == "FIX_READY":
            return self._request_fix(item, ctx)
        if item.state == "PUSH_READY":
            return self._request_push(item, ctx)
        raise AssertionError(f"unreachable ci state {item.state!r}")

    def _discover(self, item: WorkItem, ctx: StageContext) -> Continue | StageOutcome:
        """Step 1 [M]: discover PR if unset; verify implementation-go."""
        if item.pr is None:
            if item.issue is None:
                return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
            discovered_pr = ctx.github.find_pr_for_issue(item.issue)
            if discovered_pr is None:
                return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
            item.pr = discovered_pr
        # Check implementation-go label
        # Placeholder: actual check would call ctx.github or pr_manager
        # For now, assume pre-validated
        item.state = "REBASE_READY"
        return Continue("REBASE_READY")

    def _request_rebase(self, item: WorkItem, ctx: StageContext) -> Continue | JobRequest:
        """Step 2 [W:G]: mechanical rebase if behind/conflicting."""
        # Check if rebase is enabled and budget remains
        rebase_budget = ctx.budget("rebase")
        rebase_attempts = item.attempts.get("rebase", 0)
        if rebase_attempts >= rebase_budget:
            item.state = "POLL"
            return Continue("POLL")
        item.attempts["rebase"] = rebase_attempts + 1
        rebase_job = GitJob(
            repo=item.repo,
            op="rebase",
            timeout_s=GIT_JOB_TIMEOUT_S,
            kwargs={"cwd": _worktree_path(item, ctx)},
            descr="attempt_mechanical_rebase",
        )
        return JobRequest(rebase_job, on_done_state="POLL")

    def _poll(self, item: WorkItem, ctx: StageContext) -> Continue | JobRequest | StageOutcome:
        """Step 3 [M]: non-blocking classify.

        POLL is also ``push_ci_fix``'s ``on_done_state``: a failed push
        (no-commit / lost lease, flagged by ``on_job_done``) is budget
        exhaustion — the head never advanced, so re-polling would spin. Fail
        back ``fix_exhausted`` before touching CI (issue #1816).
        """
        if item.payload.pop("push_failed", None):
            return StageOutcome(Disposition.FAIL_BACK, "fix_exhausted")
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        checks = ctx.github.pr_checks(item.pr)
        conclusion = classify_ci_state(checks)
        if conclusion is CiConclusion.PENDING:
            # PENDING CI: timer-park via the coordinator heap. RETRY carries the
            # backoff delay; the coordinator re-runs step() after `delay` seconds.
            delay = min(2 ** item.attempts.get("ci_poll", 0), _BACKOFF_CAP)
            item.attempts["ci_poll"] = item.attempts.get("ci_poll", 0) + 1
            return StageOutcome(Disposition.RETRY, f"ci_poll_delay_{delay}s")
        if conclusion in (CiConclusion.GREEN, CiConclusion.NO_CHECKS):
            return StageOutcome(Disposition.ADVANCE)
        # FAILING: consume ci_fix budget or fail back
        ci_fix_budget = ctx.budget("ci_fix")
        ci_fix_attempts = item.attempts.get("ci_fix", 0)
        if ci_fix_attempts < ci_fix_budget:
            item.state = "FIX_READY"
            return Continue("FIX_READY")
        return StageOutcome(Disposition.FAIL_BACK, "fix_exhausted")

    def _request_fix(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """Step 5 [W:A]: CI-fix agent session.

        Dispatches the CI-fix coding-agent job whose prompt is built in-worker
        by :func:`build_ci_fix_prompt` (reusing
        ``CIFixOrchestrator.build_ci_fix_prompt`` verbatim). CI logs, the
        rendered review-threads block, and the failing-check names are seeded
        into ``item.payload`` by the coordinator (#1817), which owns the gh
        reads. On completion the coordinator advances the item to
        ``PUSH_READY`` (the push contract owns head-advance + lease + no-commit
        retry).
        """
        item.attempts["ci_fix"] = item.attempts.get("ci_fix", 0) + 1
        job = AgentJob(
            repo=item.repo,
            issue=item.issue if item.issue is not None else 0,
            agent=AGENT_CI_DRIVER,
            model=implementer_model(),
            prompt_builder=build_ci_fix_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=ci_driver_claude_timeout(),
            prompt_kwargs={
                "issue_number": item.issue if item.issue is not None else 0,
                "pr_number": item.pr,
                "worktree_path": str(_worktree_path(item, ctx)),
                "ci_logs": item.payload.get("ci_logs", ""),
                "pr_head_branch": item.branch,
                "advise_findings": item.payload.get("advise_findings", ""),
                "review_threads_block": item.payload.get("review_threads_block", ""),
                "failing_check_names": tuple(item.payload.get("failing_check_names") or ()),
            },
            descr="ci_fix",
        )
        return JobRequest(job, on_done_state="PUSH_READY")

    def _request_push(self, item: WorkItem, ctx: StageContext) -> Continue | JobRequest:
        """Step 5 [W:G]: push contract (push_ci_fix owns head-advance+lease+no-commit retry).

        PUSH_READY is ``ci_fix``'s ``on_done_state``: a failed fix (flagged by
        ``on_job_done``) means there is nothing to push, so reroute to POLL
        rather than dispatching push_ci_fix on an unchanged tree. POLL's
        ``ci_fix`` budget gate then classifies FAIL_BACK once the budget runs
        out (issue #1816).
        """
        if item.payload.pop("ci_fix_failed", None):
            return Continue("POLL")
        return JobRequest(None, "POLL")  # type: ignore

    def on_job_done(self, item: WorkItem, result: Any, ctx: StageContext) -> None:
        """Record a completed job's outcome so the next state can route on it.

        Called with ``item.state`` still at the READY state that submitted the
        job (the coordinator contract, :mod:`.base`): the coordinator sets
        ``item.state = on_done_state`` *after* this returns, so ``on_job_done``
        never routes by writing ``item.state`` (it would be clobbered). Instead
        — like the pr_review / implementation siblings — it stores an outcome
        flag on ``item.payload`` that the ``on_done_state`` target's ``step()``
        branches on. The submitting state is the job-kind discriminator.

        Routing folded in downstream (issue #1816):

        - ``attempt_mechanical_rebase`` (state ``REBASE_READY``,
          ``on_done_state="POLL"``): best-effort — nothing recorded; POLL
          re-classifies unconditionally and absorbs any rebase side effects.
        - ``ci_fix`` (state ``FIX_READY``, ``on_done_state="PUSH_READY"``): a
          failed fix sets ``ci_fix_failed`` so PUSH_READY reroutes to POLL
          (where the ``ci_fix`` budget gate classifies FAIL_BACK on
          exhaustion) instead of pushing a fix that never landed.
        - ``push_ci_fix`` (state ``PUSH_READY``, ``on_done_state="POLL"``): a
          failed push (no commit / lost lease) sets ``push_failed`` so POLL
          fails back ``fix_exhausted`` rather than re-polling a head that never
          advanced.

        Args:
            item: The work item whose job completed (``item.state`` is the
                READY state that submitted it).
            result: The job result from the worker pool.
            ctx: Stage context.

        """
        if item.state == "FIX_READY" and not result.ok:
            # ci_fix failure: PUSH_READY must not push a fix that never landed.
            item.payload["ci_fix_failed"] = True
        elif item.state == "PUSH_READY" and not result.ok:
            # push_ci_fix failure (no-commit / lost lease) = budget exhaustion.
            item.payload["push_failed"] = True
