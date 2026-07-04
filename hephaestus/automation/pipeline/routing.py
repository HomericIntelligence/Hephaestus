"""Declarative stage-routing table. Pure data, zero I/O (epic #1809)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class StageName(str, Enum):
    """Pipeline stage identifiers."""

    REPO = "repo"
    PLANNING = "planning"
    PLAN_REVIEW = "plan_review"
    IMPLEMENTATION = "implementation"
    PR_REVIEW = "pr_review"
    CI = "ci"
    MERGE_WAIT = "merge_wait"
    FINISHED = "finished"


class Disposition(str, Enum):
    """Outcome classification for a stage execution."""

    ADVANCE = "advance"
    RETRY = "retry"
    FAIL_BACK = "fail_back"
    SKIP = "skip"
    BLOCKED = "blocked"
    FINISH_PASS = "finish_pass"  # noqa: S105
    FINISH_FAIL = "finish_fail"


@dataclass(frozen=True)
class StageOutcome:
    """Result of a stage execution."""

    disposition: Disposition
    note: str = ""


@dataclass(frozen=True)
class Route:
    """Next stage and failure-routing rules for a stage."""

    next: StageName
    fail_routes: dict[str, StageName] = field(default_factory=dict)
    budgets: dict[str, int] = field(default_factory=dict)


# Budget values restated as pure data (cannot import the omitted orchestration
# modules that shell out). Sources:
#   plan_review_iter=3, pr_review_iter=3  <- _review_phase.py:87 MAX_REVIEW_ITERATIONS
#   pr_review_hard=6                       <- _review_phase.py:95 (=3*2)
#   blocked_address=2                      <- ci_driver.py:104 _BLOCKED_ADDRESS_MAX_ATTEMPTS
#   plan_cycles=2, ci_fix=1, rebase=2      <- issue #1811 caps
#   merge=1                                <- loop_runner.py:373 max_merge_attempts
ROUTES: dict[StageName, Route] = {
    StageName.REPO: Route(
        next=StageName.PLANNING,
        fail_routes={"*": StageName.FINISHED},
    ),
    StageName.PLANNING: Route(
        next=StageName.PLAN_REVIEW,
        fail_routes={"*": StageName.FINISHED},
        budgets={"plan": 1, "plan_cycles": 2},
    ),
    StageName.PLAN_REVIEW: Route(
        next=StageName.IMPLEMENTATION,
        fail_routes={"*": StageName.PLANNING},
        budgets={"plan_review_iter": 3},
    ),
    StageName.IMPLEMENTATION: Route(
        next=StageName.PR_REVIEW,
        fail_routes={"*": StageName.FINISHED},
        budgets={"test_fix": 1},
    ),
    StageName.PR_REVIEW: Route(
        next=StageName.CI,
        fail_routes={"*": StageName.IMPLEMENTATION},
        budgets={"pr_review_iter": 3, "pr_review_hard": 6, "blocked_address": 2},
    ),
    StageName.CI: Route(
        next=StageName.MERGE_WAIT,
        fail_routes={"*": StageName.IMPLEMENTATION},
        budgets={"ci_fix": 1},
    ),
    StageName.MERGE_WAIT: Route(
        next=StageName.FINISHED,
        fail_routes={"*": StageName.FINISHED},
        budgets={"rebase": 2, "merge": 1},
    ),
    StageName.FINISHED: Route(next=StageName.FINISHED),
}


class PipelineScope:
    """Trim ROUTES to a contiguous stage subset for partial-pipeline runs.

    The last in-scope stage routes to FINISHED; any next/fail target that exits
    the scope is rewritten to FINISHED, so no route ever points outside
    scope ∪ {FINISHED}.
    """

    def __init__(self, stages: frozenset[StageName]) -> None:
        """Initialize with an in-scope stage set."""
        self.stages = stages
        self._trimmed_routes: dict[StageName, Route] | None = None

    def trimmed_routes(self) -> dict[StageName, Route]:
        """Return a copy of ROUTES with all out-of-scope targets rewritten to FINISHED."""
        if self._trimmed_routes is None:
            self._trimmed_routes = self._compute_trimmed_routes()
        return self._trimmed_routes

    def _compute_trimmed_routes(self) -> dict[StageName, Route]:
        result = {}
        for stage, route in ROUTES.items():
            if stage not in self.stages:
                continue

            new_next = (
                route.next
                if route.next in self.stages or route.next == StageName.FINISHED
                else StageName.FINISHED
            )
            new_fail_routes = {
                key: (
                    target
                    if target in self.stages or target == StageName.FINISHED
                    else StageName.FINISHED
                )
                for key, target in route.fail_routes.items()
            }
            result[stage] = Route(next=new_next, fail_routes=new_fail_routes, budgets=route.budgets)

        return result
