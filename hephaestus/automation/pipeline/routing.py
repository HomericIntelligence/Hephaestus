"""Declarative stage-routing table. Pure data, zero I/O (epic #1809).

The ROUTES table below is the code form of the normative table in
``docs/AUTOMATION_LOOP_ARCHITECTURE.md`` ("ROUTES table" section). Any change
here MUST be reflected there and vice versa;
``tests/unit/automation/pipeline/test_routing.py`` pins every row.

All budgets are per-item-lifetime counters (tracked in ``WorkItem.attempts``);
they are never reset when an item re-enters a stage, so cross-stage
regression cycles remain globally bounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

#: Default for the ``merge`` budget. Mirrors ``LoopConfig.drive_green_loops``
#: and the ``--drive-green-loops`` CLI default in ``loop_runner.py``; the
#: coordinator overrides it from config when the pipeline is wired up
#: (epic #1809 coordinator slice).
DEFAULT_DRIVE_GREEN_LOOPS = 5


class StageName(str, Enum):
    """Pipeline stage identifiers.

    Members are declared in pipeline order and MUST NOT be reordered.
    """

    REPO = "repo"
    PLANNING = "planning"
    PLAN_REVIEW = "plan_review"
    IMPLEMENTATION = "implementation"
    PR_REVIEW = "pr_review"
    MERGE_WAIT = "merge_wait"
    FINISHED = "finished"


#: Active loop order used for scope-contiguity validation. CI/CD intentionally
#: has no pipeline stage: normal review may collect its evidence for a binary
#: verdict, but the loop does not change CI/CD and it never independently
#: authorizes an approval.
PIPELINE_ORDER: tuple[StageName, ...] = tuple(StageName)


class Disposition(str, Enum):
    """Outcome classification for a stage execution."""

    ADVANCE = "advance"
    RETRY = "retry"
    FAIL_BACK = "fail_back"
    SKIP = "skip"
    BLOCKED = "blocked"
    # "PASS" trips ruff's hardcoded-password heuristic (S105); this is a
    # pipeline disposition, not a credential.
    FINISH_PASS = "finish_pass"  # noqa: S105
    # terminal fail; no S105 needed
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


# The rows below transcribe docs/AUTOMATION_LOOP_ARCHITECTURE.md "ROUTES
# table" exactly: named fail-route keys are the doc's reason vocabulary, "*"
# is the doc's default target. Budget provenance:
#   plan_review_iter=3, pr_review_iter=3  <- _review_phase.py:87 MAX_REVIEW_ITERATIONS
#   pr_review_hard=6                       <- _review_phase.py:95 (=3*2, progress-aware)
#   blocked_address=2                     <- review_thread_resolver.py _BLOCKED_ADDRESS_MAX_ATTEMPTS
#   clone=2, plan=2, plan_cycles=2,
#   implement=2, test_fix=1                <- architecture doc stage sections
#   merge=DEFAULT_DRIVE_GREEN_LOOPS        <- loop_runner.py LoopConfig.drive_green_loops
#                                             and --drive-green-loops defaults
ROUTES: dict[StageName, Route] = {
    # The repo item itself is terminal: it seeds discovered issues/PRs into
    # their classified entry queues and then advances to finished(pass).
    StageName.REPO: Route(
        next=StageName.FINISHED,
        fail_routes={"*": StageName.FINISHED},
        budgets={"clone": 2},
    ),
    StageName.PLANNING: Route(
        next=StageName.PLAN_REVIEW,
        fail_routes={"*": StageName.FINISHED},
        budgets={"plan": 2},
    ),
    StageName.PLAN_REVIEW: Route(
        next=StageName.IMPLEMENTATION,
        fail_routes={
            "nogo": StageName.PLANNING,
            "plan_cycles_exhausted": StageName.FINISHED,
            "*": StageName.PLANNING,
        },
        budgets={"plan_review_iter": 3, "plan_cycles": 2},
    ),
    StageName.IMPLEMENTATION: Route(
        next=StageName.PR_REVIEW,
        fail_routes={
            "plan_not_go": StageName.PLAN_REVIEW,
            "already_implementation_go_pr": StageName.MERGE_WAIT,
            "*": StageName.FINISHED,
        },
        budgets={"implement": 2, "test_fix": 1},
    ),
    StageName.PR_REVIEW: Route(
        next=StageName.MERGE_WAIT,
        fail_routes={
            "agent_error": StageName.IMPLEMENTATION,
            "human_blocked": StageName.FINISHED,
            "exhaustion": StageName.FINISHED,
            "*": StageName.PR_REVIEW,
        },
        budgets={"pr_review_iter": 3, "pr_review_hard": 6},
    ),
    StageName.MERGE_WAIT: Route(
        next=StageName.FINISHED,
        fail_routes={
            "closed": StageName.FINISHED,
            # A missing loop-owned approval label needs a fresh review, not
            # terminal abandonment. Other merge-wait failures are terminal;
            # the stage never reconciles a state owned by another run.
            "not_implementation_go": StageName.PR_REVIEW,
            "*": StageName.FINISHED,
        },
        budgets={},
    ),
    StageName.FINISHED: Route(next=StageName.FINISHED),
}


def budget_keys() -> frozenset[str]:
    """Return the union of all budget keys declared across ROUTES."""
    keys: set[str] = set()
    for route in ROUTES.values():
        keys.update(route.budgets)
    return frozenset(keys)


class PipelineScope:
    """Trim ROUTES to a contiguous stage subset for partial-pipeline runs.

    The last in-scope stage routes to FINISHED; any next/fail target that
    exits the scope is rewritten to FINISHED, so no route ever points outside
    scope ∪ {FINISHED}.

    Raises:
        ValueError: If ``stages`` is empty or not contiguous in pipeline
            order (FINISHED, being the universal sink, is allowed in any
            scope and does not break contiguity).

    """

    def __init__(self, stages: frozenset[StageName]) -> None:
        """Validate and store the in-scope stage set.

        Args:
            stages: Non-empty, contiguous (in pipeline order) set of stages.

        Raises:
            ValueError: On an empty or non-contiguous stage set.

        """
        if not stages:
            raise ValueError("PipelineScope requires at least one stage")
        ordered = [s for s in PIPELINE_ORDER if s in stages and s != StageName.FINISHED]
        if ordered:
            first = PIPELINE_ORDER.index(ordered[0])
            last = PIPELINE_ORDER.index(ordered[-1])
            if last - first + 1 != len(ordered):
                raise ValueError(
                    f"PipelineScope stages must be contiguous in pipeline order; "
                    f"got gaps in {sorted(s.value for s in stages)}"
                )
        self.stages = stages
        self._trimmed_routes: dict[StageName, Route] | None = None

    def trimmed_routes(self) -> dict[StageName, Route]:
        """Return a fresh copy of ROUTES with out-of-scope targets rewritten to FINISHED.

        Each call returns a new dict of new ``Route`` objects with copied
        ``fail_routes``/``budgets`` mappings, so callers can never mutate the
        module-global ``ROUTES`` (or this scope's cache) through the result.
        """
        if self._trimmed_routes is None:
            self._trimmed_routes = self._compute_trimmed_routes()
        return {
            stage: Route(
                next=route.next,
                fail_routes=dict(route.fail_routes),
                budgets=dict(route.budgets),
            )
            for stage, route in self._trimmed_routes.items()
        }

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
            result[stage] = Route(
                next=new_next,
                fail_routes=new_fail_routes,
                budgets=dict(route.budgets),
            )

        return result
