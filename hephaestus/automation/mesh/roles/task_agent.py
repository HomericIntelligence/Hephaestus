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


class TaskAgentHandler:
    """Implements the issue named in the dispatch payload."""

    def __init__(
        self,
        implementer_factory: Any | None = None,
        ci_driver_factory: Any | None = None,
        pr_state: Any | None = None,
    ) -> None:
        """Factories override IssueImplementer/CIDriver construction in tests."""
        self._implementer_factory = implementer_factory
        self._ci_driver_factory = ci_driver_factory
        self._pr_state = pr_state

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

        # Drive-green + merge phase: sequential children branch off main, so
        # the task is only complete once its PR is MERGED (ADR-013 §10 "Done"
        # row). CIDriver polls checks, fixes failures, and arms auto-merge.
        if pr:
            ctx.progress(
                f"Task-agent driving PR #{pr['number']} to green CI and merge "
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

            driver_factory(issue).run()
            state = self._merged_state(pr["number"])
            if state != "MERGED":
                return RoleResult(
                    ok=False,
                    error_kind="PRNotMerged",
                    error_message=(
                        f"PR #{pr['number']} for issue #{issue} is {state or 'unknown'} "
                        "after the drive-green phase"
                    ),
                    retryable=True,
                    pr=pr,
                )
            pr["merged"] = True

        return RoleResult(
            ok=True,
            summary=f"issue #{issue} implemented"
            + (f", PR #{pr['number']} merged" if pr else ""),
            pr=pr,
        )

    def _merged_state(self, pr_number: int) -> str:
        """Live-state check: the PR's actual GitHub state (never trust logs)."""
        if self._pr_state is not None:
            return str(self._pr_state(pr_number))
        import json

        from hephaestus.github.client import gh_call

        try:
            result = gh_call(["pr", "view", str(pr_number), "--json", "state"])
            return str(json.loads(result.stdout).get("state", ""))
        except Exception as exc:
            logger.warning("PR state check failed for #%s: %s", pr_number, exc)
            return ""
