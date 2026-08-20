"""Declarative stage-routing table. Pure data, zero I/O (epic #1809).

``ROUTES`` is the executable authority for stage order, success and failure
targets, and per-item budgets. Documentation describes its schema, while
tests generate structural and scoped-routing cases from the table.

All budgets are per-item-lifetime counters (tracked in ``WorkItem.attempts``);
they are never reset when an item re-enters a stage, so cross-stage
regression cycles remain globally bounded.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

#: Default for the ``merge`` budget. Mirrors ``LoopConfig.drive_green_loops``
#: and the ``--drive-green-loops`` CLI default in ``loop_runner.py``; the
#: coordinator overrides it from config when the pipeline is wired up
#: (epic #1809 coordinator slice).
DEFAULT_DRIVE_GREEN_LOOPS = 5


class StageName(StrEnum):
    """Pipeline stage identifiers.

    ``ROUTES`` insertion order, not enum declaration order, defines pipeline
    order for non-terminal stages. ``FINISHED`` is always the terminal sink.
    """

    REPO = "repo"
    PLANNING = "planning"
    PLAN_REVIEW = "plan_review"
    IMPLEMENTATION = "implementation"
    PR_REVIEW = "pr_review"
    MERGE_WAIT = "merge_wait"
    LEARNING = "learning"
    FINISHED = "finished"


class Disposition(StrEnum):
    """Outcome classification for a stage execution."""

    ADVANCE = "advance"
    RETRY = "retry"
    FAIL_BACK = "fail_back"
    SKIP = "skip"
    EJECT = "eject"
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


# Named fail-route keys are the stage reason vocabulary, "*" is the default
# target. Budget provenance:
#   plan_review_iter=3, pr_review_iter=3, pr_review_hard=6
#                                             <- architecture doc stage sections
#   clone=2, plan=2, plan_cycles=2,
#   implement=2, rebase_conflict=2, test_fix=1
#                                             <- architecture doc stage sections
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
            "plan_missing": StageName.PLANNING,
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
        budgets={"implement": 2, "rebase_conflict": 2, "test_fix": 1},
    ),
    StageName.PR_REVIEW: Route(
        next=StageName.MERGE_WAIT,
        fail_routes={
            "agent_error": StageName.IMPLEMENTATION,
            "empty_pr_diff": StageName.IMPLEMENTATION,
            "implementation_remediation": StageName.IMPLEMENTATION,
            "exhaustion": StageName.FINISHED,
            "*": StageName.PR_REVIEW,
        },
        budgets={"pr_review_iter": 3, "pr_review_hard": 6},
    ),
    StageName.MERGE_WAIT: Route(
        next=StageName.FINISHED,
        fail_routes={
            "closed": StageName.FINISHED,
            # A missing loop-owned approval label or a missing/drifted
            # process-local reviewed-head proof needs fresh review, not
            # terminal abandonment. Other merge-wait failures are terminal;
            # the stage never reconciles a state owned by another run.
            "not_implementation_go": StageName.PR_REVIEW,
            "reviewed_head_missing": StageName.PR_REVIEW,
            "reviewed_head_drift": StageName.PR_REVIEW,
            "merge_conflicting": StageName.IMPLEMENTATION,
            "post_review_rebase_required": StageName.IMPLEMENTATION,
            "*": StageName.FINISHED,
        },
        budgets={"merge": DEFAULT_DRIVE_GREEN_LOOPS},
    ),
    StageName.LEARNING: Route(
        next=StageName.FINISHED,
        fail_routes={
            "resume_implementation": StageName.IMPLEMENTATION,
            "resume_plan_review": StageName.PLAN_REVIEW,
            "*": StageName.FINISHED,
        },
        budgets={"learn": 2},
    ),
    StageName.FINISHED: Route(next=StageName.FINISHED),
}


# Lane membership remains an execution-capacity concern, while ordering within
# and across lanes comes only from ROUTES.
_AUXILIARY_STAGES = frozenset({StageName.LEARNING, StageName.FINISHED})


def _pipeline_order(routes: Mapping[StageName, Route]) -> tuple[StageName, ...]:
    """Return route order with the universal terminal sink last.

    The route table remains authoritative for every executable stage's
    relative order.  ``FINISHED`` is excluded from that ordering because its
    terminal-sink contract must not depend on an incidental dictionary
    position.
    """
    return (
        *(stage for stage in routes if stage is not StageName.FINISHED),
        StageName.FINISHED,
    )


#: Full stage order derived from the authoritative routing table. CI/CD
#: intentionally has no pipeline stage. ``FINISHED`` is always last.
PIPELINE_ORDER: tuple[StageName, ...] = _pipeline_order(ROUTES)
MAIN_PIPELINE_ORDER: tuple[StageName, ...] = tuple(
    stage for stage in PIPELINE_ORDER if stage not in _AUXILIARY_STAGES
)
AUXILIARY_PIPELINE_ORDER: tuple[StageName, ...] = tuple(
    stage for stage in PIPELINE_ORDER if stage in _AUXILIARY_STAGES
)


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
        ordered = [s for s in MAIN_PIPELINE_ORDER if s in stages]
        # An ALL-FINISHED scope is by-design: ``_compute_trimmed_routes`` reduces
        # it to ``{StageName.FINISHED: ROUTES[StageName.FINISHED]}`` (a no-op
        # trim).  ``test_pipeline_scope_finished_only`` pins this contract, so
        # do NOT reject it here -- callers rely on FINISHED-only as the
        # universal-sink sentinel.   re-analysis tried to reject and
        # was reverted.
        if ordered:
            first = MAIN_PIPELINE_ORDER.index(ordered[0])
            last = MAIN_PIPELINE_ORDER.index(ordered[-1])
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
        implicit = {StageName.LEARNING, StageName.FINISHED}
        available = self.stages | implicit
        for stage, route in ROUTES.items():
            if stage not in available:
                continue

            new_next = route.next if route.next in available else StageName.FINISHED
            new_fail_routes = {
                key: (target if target in available else StageName.FINISHED)
                for key, target in route.fail_routes.items()
            }
            result[stage] = Route(
                next=new_next,
                fail_routes=new_fail_routes,
                budgets=dict(route.budgets),
            )

        return result
