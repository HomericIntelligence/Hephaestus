"""Pipeline chief-architect role: decompose an epic into an HMAS brief.

Reads the epic's task-list body, plans each child issue via
:class:`hephaestus.automation.planner.Planner` (``.run()``, headless), then
ingests the result into Agamemnon as a TaskBrief (``POST /v1/briefs``) so the
full L0–L3 HmasTask tree and TaskStateMachine drive execution (ADR-013 §10).

L3 ``impls`` entries are strings of the form ``"#123 (depends on: #456)"`` —
valid for today's Agamemnon brief parser, and carrying the GitHub issue ref
plus dependency edges for the extended parser to lift onto HmasTask fields.
"""

from __future__ import annotations

import logging
from typing import Any

from hephaestus.automation.mesh.epic import EpicChild, parse_task_list
from hephaestus.automation.mesh.worker import RoleResult, TaskContext

logger = logging.getLogger(__name__)


def impl_entry(child: EpicChild) -> str:
    """Render one L3 impl string for *child* (issue ref + dependency edges)."""
    entry = f"#{child.number}"
    if child.depends_on:
        deps = ", ".join(f"#{n}" for n in child.depends_on)
        entry += f" (depends on: {deps})"
    return entry


def build_brief(
    epic: dict[str, Any], title: str, body: str, children: list[EpicChild]
) -> dict[str, Any]:
    """Build the Agamemnon TaskBrief for an epic and its task-list children."""
    repo = str(epic.get("repo", ""))
    module = f"epic-{epic.get('issue', '0')}"
    description = body.strip().split("\n\n", 1)[0][:1000]
    return {
        "title": title,
        "description": description,
        "repos": [repo],
        "modules": {repo: [module]},
        "impls": {repo: {module: [impl_entry(child) for child in children]}},
    }


class ChiefArchitectHandler:
    """Plans an epic's children and submits the brief to Agamemnon."""

    def __init__(self, planner_factory: Any | None = None) -> None:
        """*planner_factory* overrides Planner construction in tests."""
        self._planner_factory = planner_factory

    def handle(self, ctx: TaskContext) -> RoleResult:
        """Decompose ``payload['epic']`` into a planned, ingested brief."""
        epic = ctx.payload.get("epic") or {}
        epic_issue = epic.get("issue")
        if not epic_issue:
            # An ingested brief's own L0 root is re-dispatched to this queue
            # (Agamemnon enqueues every delegated node to its layer's role
            # queue). Without an epic payload there is nothing to decompose —
            # it is a coordination node: acknowledge it so Agamemnon's
            # delegate_unblocked_children walk can delegate L1 (ADR-013 §10).
            if ctx.payload.get("brief_id"):
                from hephaestus.automation.mesh.roles.coordination import CoordinationHandler

                return CoordinationHandler().handle(ctx)
            return RoleResult(
                ok=False,
                error_kind="BadDispatch",
                error_message="chief-architect payload missing 'epic.issue'",
                retryable=False,
            )
        epic_issue = int(epic_issue)

        from hephaestus.automation.github_api.issues import gh_issue_json

        issue_data = gh_issue_json(epic_issue)
        title = str(issue_data.get("title", f"epic #{epic_issue}"))
        body = str(issue_data.get("body", ""))
        children = parse_task_list(body)
        if not children:
            return RoleResult(
                ok=False,
                error_kind="EmptyEpic",
                error_message=f"epic #{epic_issue} has no task-list children",
                retryable=False,
            )

        factory = self._planner_factory
        if factory is None:
            from hephaestus.automation.models import PlannerOptions
            from hephaestus.automation.planner import Planner

            def factory(issues: list[int]) -> Any:
                return Planner(PlannerOptions(issues=issues, issues_explicit=True, parallel=1))

        planner = factory([child.number for child in children])
        plan_results = planner.run()
        failed = [n for n, r in plan_results.items() if not r.success]
        if failed:
            return RoleResult(
                ok=False,
                error_kind="PlanFailed",
                error_message=f"planning failed for issues: {sorted(failed)}",
                retryable=True,
            )

        brief = build_brief(epic, title, body, children)
        plan = ctx.agamemnon.submit_brief(brief)
        brief_id = str(plan.get("brief", {}).get("id") or plan.get("brief_id") or "")
        ctx.progress(
            f"Epic decomposed: {len(children)} children planned, "
            f"brief `{brief_id or 'submitted'}` ingested into Agamemnon."
        )
        return RoleResult(
            ok=True,
            summary=f"epic #{epic_issue}: {len(children)} children planned, brief {brief_id}",
        )
