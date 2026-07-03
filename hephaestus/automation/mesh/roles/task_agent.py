"""Task-agent role: implement one GitHub issue to a merged-ready PR.

Wraps :class:`hephaestus.automation.implementer.IssueImplementer` via its
``.run()`` API (never ``main()``), with ``enable_ui=False`` for headless
vessels. Advise-before and learn-after run inside the implementer
(``enable_advise``/``enable_learn`` default True), and the PR review gate
(``state:implementation-go``) is earned by the in-loop review cycle.
"""

from __future__ import annotations

import logging
from typing import Any

from hephaestus.automation.mesh.worker import RoleResult, TaskContext

logger = logging.getLogger(__name__)

#: Check conclusions that mean auto-merge can never fire without another fix.
_FAILED_CHECK_CONCLUSIONS = {"FAILURE", "TIMED_OUT"}


def _pr_merge_gate_state(data: dict[str, Any]) -> bool:
    """Pure merge-gate decision over a ``gh pr view --json`` payload.

    MERGED always passes. An open PR passes only when the review gate granted
    it (auto-merge armed or ``state:implementation-go`` label) AND no check has
    already concluded FAILURE/TIMED_OUT — an armed PR with a failed required
    check can never merge on its own, so completing the task would delegate
    children onto a base that never lands (observed live: ProjectOdyssey#5523
    armed, then lint/pre-commit failed after the task completed).
    Pending/queued checks are fine: armed + in-flight CI merges by itself.
    """
    if str(data.get("state", "")).upper() == "MERGED":
        return True
    for check in data.get("statusCheckRollup") or []:
        if str(check.get("conclusion", "")).upper() in _FAILED_CHECK_CONCLUSIONS:
            return False
    if data.get("autoMergeRequest"):
        return True
    labels = [label.get("name", "") for label in data.get("labels", [])]
    return "state:implementation-go" in labels


class TaskAgentHandler:
    """Implements the issue named in the dispatch payload."""

    def __init__(
        self,
        implementer_factory: Any | None = None,
        ci_driver_factory: Any | None = None,
        label_ops: Any | None = None,
        merge_gate: Any | None = None,
    ) -> None:
        """Factories override IssueImplementer/CIDriver construction in tests."""
        self._implementer_factory = implementer_factory
        self._ci_driver_factory = ci_driver_factory
        self._label_ops = label_ops
        self._merge_gate = merge_gate

    def handle(self, ctx: TaskContext) -> RoleResult:
        """Implement ``payload['issue']`` and report the PR."""
        issue = ctx.payload.get("issue")
        if issue is None:
            return RoleResult(
                ok=False,
                error_kind="BadDispatch",
                error_message="task-agent payload missing 'issue'",
                retryable=False,
            )
        issue = int(issue)

        # Progress comment = resume anchor (ADR-013 §4). The implementer's own
        # state manager plus the existing branch/PR make redelivery a resume.
        ctx.progress(
            f"Task-agent myrmidon `{ctx.config.agent_id}` on `{ctx.config.exec_host}` "
            f"claimed this issue (task {ctx.task_id}, attempt {ctx.attempt})."
        )

        # A prior attempt's NOGO-exhausted review loop applies ``state:skip``,
        # which makes the implementer skip the issue entirely — every
        # redelivery would then burn in seconds as ``NoResult`` (#1780). The
        # mesh owns the retry here: strip the stale skip so this attempt
        # re-enters the review loop with a fresh budget.
        if ctx.is_redelivery:
            self._strip_stale_skip(issue)

        factory = self._implementer_factory
        if factory is None:
            from hephaestus.automation.implementer import IssueImplementer
            from hephaestus.automation.models import ImplementerOptions

            def factory(issue_number: int, resume: bool) -> Any:
                return IssueImplementer(
                    ImplementerOptions(
                        issues=[issue_number],
                        max_workers=1,
                        enable_ui=False,
                        resume=resume,
                    )
                )

        implementer = factory(issue, ctx.is_redelivery)
        results = implementer.run()
        result = results.get(issue)
        if result is None:
            return RoleResult(
                ok=False,
                error_kind="NoResult",
                error_message=f"implementer returned no result for #{issue}",
                retryable=True,
            )

        if getattr(result, "plan_review_not_go", False):
            return RoleResult(
                ok=False,
                error_kind="PlanNotGo",
                error_message=f"issue #{issue} lacks a plan-GO verdict; re-plan first",
                retryable=True,
            )
        if not result.success:
            return RoleResult(
                ok=False,
                error_kind="ImplementFailed",
                error_message=str(result.error or "implementer failed"),
                retryable=True,
            )

        pr: dict[str, Any] | None = None
        if getattr(result, "pr_number", None):
            pr = {"number": result.pr_number}

        if pr:
            drive_failure = self._drive_pr_to_merge_ready(issue, pr, ctx)
            if drive_failure is not None:
                return drive_failure

        return RoleResult(
            ok=True,
            summary=f"issue #{issue} implemented"
            + (f", PR #{pr['number']} merge-ready" if pr else ""),
            pr=pr,
        )

    def _strip_stale_skip(self, issue: int) -> None:
        """Remove a prior attempt's ``state:skip`` from *issue* (best-effort)."""
        ops = self._label_ops
        if ops is None:
            from hephaestus.automation.github_api.issues import gh_issue_json
            from hephaestus.automation.github_api.labels import gh_issue_remove_labels
            from hephaestus.automation.state_labels import STATE_SKIP, is_skipped

            ops = (
                lambda n: [label.get("name", "") for label in gh_issue_json(n).get("labels", [])],
                lambda n: gh_issue_remove_labels(n, [STATE_SKIP]),
                is_skipped,
            )
        get_labels, remove_skip, skipped = ops
        try:
            if skipped(get_labels(issue)):
                logger.info("issue #%s: removing stale state:skip before mesh retry", issue)
                remove_skip(issue)
        except Exception as exc:
            logger.warning("issue #%s: could not strip state:skip: %s", issue, exc)

    def _drive_pr_to_merge_ready(
        self,
        issue: int,
        pr: dict[str, Any],
        ctx: TaskContext,
    ) -> RoleResult | None:
        """Run CIDriver and mark the PR as ready for mesh handoff."""
        # CIDriver owns the distinction between fixable failures and successful
        # waiting states (armed/pending review/BLOCKED on branch protection).
        # Do not require GitHub state MERGED here; that turns normal armed
        # handoffs into retryable mesh redeliveries.
        ctx.progress(
            f"Task-agent driving PR #{pr['number']} to green CI and merge-ready "
            f"(task {ctx.task_id})."
        )
        driver_factory = self._ci_driver_factory
        if driver_factory is None:
            from hephaestus.automation.ci_driver import CIDriver
            from hephaestus.automation.models import CIDriverOptions

            def driver_factory(issue_number: int) -> Any:
                return CIDriver(
                    CIDriverOptions(
                        issues=[issue_number],
                        max_workers=1,
                        enable_ui=False,
                        include_bot_prs=False,
                    )
                )

        driver = driver_factory(issue)
        drive_results = driver.run()
        drive_result = drive_results.get(issue)
        if drive_result is None:
            return RoleResult(
                ok=False,
                error_kind="NoCIDriveResult",
                error_message=(
                    f"CI driver returned no result for issue #{issue} / PR #{pr['number']}"
                ),
                retryable=True,
                pr=pr,
            )
        if not self._ci_drive_succeeded(issue, driver, drive_results):
            return RoleResult(
                ok=False,
                error_kind="CIDriveFailed",
                error_message=str(
                    getattr(drive_result, "error", None)
                    or f"CI driver left PR #{pr['number']} needing action"
                ),
                retryable=True,
                pr=pr,
            )
        if not self._merge_gate_passed(pr["number"]):
            # _evaluate_run_result deliberately excuses an un-armed PR that
            # lacks ``state:implementation-go`` ("pending review", #1576) —
            # right for the loop runner, where a later review labels it. In
            # the mesh, the review loop that would grant that label has
            # ALREADY run and ended NOGO, so nothing will ever arm the PR:
            # completing here would delegate dependent children on top of an
            # unmerged base. Fail retryably instead; redelivery strips the
            # stale ``state:skip`` and re-enters review with a fresh budget.
            return RoleResult(
                ok=False,
                error_kind="ReviewNotGo",
                error_message=(
                    f"PR #{pr['number']} for issue #{issue} has no implementation-GO "
                    "label and auto-merge is not armed after the review loop"
                ),
                retryable=True,
                pr=pr,
            )
        pr["merge_ready"] = True
        return None

    def _merge_gate_passed(self, pr_number: int) -> bool:
        """Return whether the PR cleared the review gate (merged, armed, or GO-labeled)."""
        if self._merge_gate is not None:
            return bool(self._merge_gate(pr_number))
        import json

        from hephaestus.github.client import _gh_call

        try:
            result = _gh_call(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--json",
                    "state,labels,autoMergeRequest,statusCheckRollup",
                ]
            )
            data = json.loads(result.stdout)
        except Exception as exc:
            logger.warning("PR #%s: merge-gate check failed: %s", pr_number, exc)
            return False
        return _pr_merge_gate_state(data)

    def _ci_drive_succeeded(
        self,
        issue: int,
        driver: Any,
        drive_results: dict[int, Any],
    ) -> bool:
        """Return whether CIDriver classified this issue as complete enough to hand off."""
        from hephaestus.automation.ci_driver import _evaluate_run_result

        open_prs_remaining = getattr(driver, "open_prs_remaining", []) or []
        return (
            _evaluate_run_result(
                drive_results,
                open_prs_remaining,
                issues=[issue],
                as_json=False,
            )
            == 0
        )
