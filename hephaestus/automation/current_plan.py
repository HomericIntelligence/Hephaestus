"""Read current plan authority for implementation scope checks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from subprocess import SubprocessError
from typing import TYPE_CHECKING

from .comment_identity import CommentAliasConflictError
from .requirements_recovery import has_finalized_plan_candidate, verified_finalized_plan
from .review_journal import PlanDiscoveryResult
from .state_labels import (
    ALL_IMPLEMENTATION_STATE_LABELS,
    ATHENA_FINALIZED_PLAN_LABEL,
    STATE_PLAN_GO,
    STATE_SKIP,
    is_exclusive_plan_state,
)

if TYPE_CHECKING:
    from .pipeline.stages.base import StageGitHub


@dataclass(frozen=True)
class CurrentPlanRead:
    """Keep the plan result and its issue-body origin in one local value."""

    plan: PlanDiscoveryResult
    finalized_body: bool = False


def _labels(snapshot: dict[str, object]) -> set[str]:
    """Return the label names supplied by an issue snapshot."""
    raw = snapshot.get("labels")
    if not isinstance(raw, list):
        return set()
    names = [label.get("name") if isinstance(label, dict) else label for label in raw]
    return {name for name in names if isinstance(name, str)}


def _matches_finalized_snapshot(snapshot: object, issue_number: int, body: str) -> bool:
    """Require the same open issue, complete body, and finalized state."""
    if not isinstance(snapshot, dict):
        return False
    labels = _labels(snapshot)
    return (
        type(snapshot.get("number")) is int
        and snapshot["number"] == issue_number
        and str(snapshot.get("state", "")).upper() == "OPEN"
        and snapshot.get("authoritySanitized") is not True
        and snapshot.get("body") == body
        and snapshot.get("bodyDigest") == hashlib.sha256(body.encode("utf-8")).hexdigest()
        and is_exclusive_plan_state(labels, STATE_PLAN_GO)
        and ATHENA_FINALIZED_PLAN_LABEL in labels
        and STATE_SKIP not in labels
        and not labels.intersection(ALL_IMPLEMENTATION_STATE_LABELS)
    )


def _read_finalized_plan(
    issue_number: int, github: StageGitHub, snapshot: dict[str, object], body: str
) -> PlanDiscoveryResult:
    """Authenticate one sealed issue body and confirm it after the editor read."""
    if verified_finalized_plan(body) is None:
        return PlanDiscoveryResult.identity_conflict("finalized plan seal is invalid")
    if not _matches_finalized_snapshot(snapshot, issue_number, body):
        return PlanDiscoveryResult.read_error("finalized plan state is unavailable")
    if not github.issue_body_edited_by_viewer(issue_number):
        return PlanDiscoveryResult.identity_conflict("finalized plan editor is not the viewer")
    confirmed = github.gh_issue_json(issue_number)
    if not _matches_finalized_snapshot(confirmed, issue_number, body):
        return PlanDiscoveryResult.read_error("finalized plan changed during readback")
    return PlanDiscoveryResult.found(body)


def read_current_plan(issue_number: int, github: StageGitHub) -> CurrentPlanRead:
    """Read a sealed body or retain the existing comment-backed plan lookup."""
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
        if not has_finalized_plan_candidate(body) and ATHENA_FINALIZED_PLAN_LABEL not in _labels(
            snapshot
        ):
            return CurrentPlanRead(github.discover_plan(issue_number))
        return CurrentPlanRead(_read_finalized_plan(issue_number, github, snapshot, body), True)
    except CommentAliasConflictError:
        raise
    except (OSError, RuntimeError, SubprocessError, TypeError, ValueError) as exc:
        return CurrentPlanRead(PlanDiscoveryResult.read_error(exc))
