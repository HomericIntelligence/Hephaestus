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

    def __init__(self, implementer_factory: Any | None = None) -> None:
        """*implementer_factory* overrides IssueImplementer construction in tests."""
        self._implementer_factory = implementer_factory

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
        return RoleResult(
            ok=True,
            summary=f"issue #{issue} implemented",
            pr=pr,
        )
