"""Read current plan authority for implementation scope checks."""

from __future__ import annotations

from dataclasses import dataclass
from subprocess import SubprocessError
from typing import TYPE_CHECKING

from .comment_identity import CommentAliasConflictError
from .requirements_recovery import has_finalized_plan_candidate
from .review_journal import PlanDiscoveryResult

if TYPE_CHECKING:
    from .pipeline.stages.base import StageGitHub


@dataclass(frozen=True)
class CurrentPlanRead:
    """Keep the plan result and its issue-body origin in one local value."""

    plan: PlanDiscoveryResult
    finalized_body: bool = False


def read_current_plan(issue_number: int, github: StageGitHub) -> CurrentPlanRead:
    """Read a finalized issue body or retain the comment-backed plan lookup."""
    try:
        snapshot = github.gh_issue_json(issue_number)
        if not isinstance(snapshot, dict):
            return CurrentPlanRead(
                PlanDiscoveryResult.read_error("current issue snapshot is unavailable")
            )
        body = snapshot.get("body")
        if not isinstance(body, str):
            return CurrentPlanRead(
                PlanDiscoveryResult.read_error("current issue body is unavailable")
            )
        if not has_finalized_plan_candidate(body):
            return CurrentPlanRead(github.discover_plan(issue_number))
        return CurrentPlanRead(PlanDiscoveryResult.found(body), True)
    except CommentAliasConflictError:
        raise
    except (OSError, RuntimeError, SubprocessError, TypeError, ValueError) as exc:
        return CurrentPlanRead(PlanDiscoveryResult.read_error(exc))
