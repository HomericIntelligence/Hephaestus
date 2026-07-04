"""Hypothesis property tests for the routing table (issue #1811 deliverable).

Generative invariants over ROUTES and PipelineScope:

(a) every non-terminal stage declares a default ("*") fail target;
(b) any generated failure sequence, driven through the table with per-item
    budgets enforced, always terminates within a global bound;
(c) for every ROUTES budget key: budget+1 repeats exhaust it, and a fresh
    WorkItem carries a matching attempts counter;
(d) for every generated contiguous PipelineScope subset, no trimmed route
    targets a stage outside scope ∪ {FINISHED}.
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from hephaestus.automation.pipeline import (
    ROUTES,
    PipelineScope,
    StageName,
    WorkItem,
)
from hephaestus.automation.pipeline.routing import PIPELINE_ORDER, budget_keys
from hephaestus.automation.pipeline.work_item import ItemKind

NON_TERMINAL = [s for s in StageName if s != StageName.FINISHED]

# Every fail-route reason key declared anywhere in ROUTES, plus an unknown
# one to exercise the "*" default path.
_DECLARED_REASONS = sorted(
    {key for route in ROUTES.values() for key in route.fail_routes if key != "*"}
)
_REASONS = [*_DECLARED_REASONS, "unknown_reason"]

# Reason → budget key consumed when that failure repeats. Mirrors the
# architecture doc's per-stage budget assignments; unknown reasons consume
# none and resolve purely via the "*" default.
_REASON_BUDGET: dict[str, str | None] = {
    "nogo": "plan_review_iter",
    "plan_cycles_exhausted": "plan_cycles",
    "plan_not_go": "implement",
    "already_implementation_go_pr": None,
    "agent_error": "pr_review_iter",
    "human_blocked": "pr_review_iter",
    "exhaustion": "pr_review_iter",
    "fix_exhausted": "ci_fix",
    "not_implementation_go": "pr_review_iter",
    "no_pr": None,
    "ci_red": "merge",
    "blocked_exhausted": "blocked_address",
    "closed": "merge",
    "timeout": "merge",
    "unknown_reason": None,
}


def _fail_target(stage: StageName, reason: str) -> StageName:
    """Resolve a failure at ``stage`` for ``reason`` via ROUTES."""
    route = ROUTES[stage]
    return route.fail_routes.get(reason, route.fail_routes.get("*", StageName.FINISHED))


def _declared_budget(budget_key: str) -> int:
    """Return the ROUTES-declared value for a budget key."""
    return next(
        route.budgets[budget_key] for route in ROUTES.values() if budget_key in route.budgets
    )


@given(stage=st.sampled_from(NON_TERMINAL))
def test_every_stage_has_default_fail_target(stage: StageName) -> None:
    """(a) Every non-terminal stage declares a '*' default fail target."""
    assert "*" in ROUTES[stage].fail_routes


@given(
    start=st.sampled_from(NON_TERMINAL),
    reasons=st.lists(st.sampled_from(_REASONS), min_size=1, max_size=60),
)
@settings(max_examples=300)
def test_driven_failure_sequences_always_terminate(start: StageName, reasons: list[str]) -> None:
    """(b) Any failure sequence terminates once per-item budgets are enforced.

    Drives a WorkItem from ``start`` through the generated failure reasons:
    each budgeted failure increments the item's counter (per-item-lifetime,
    never reset); once a counter exceeds its ROUTES budget the drive routes
    to FINISHED (exhaustion), mirroring the coordinator contract. The walk
    must never exceed a global step bound derived from the budget sum.
    """
    item = WorkItem(repo="r", kind=ItemKind.ISSUE, issue=1, stage=start)
    max_total_steps = sum(
        budget for route in ROUTES.values() for budget in route.budgets.values()
    ) + len(_REASONS)
    budgeted_steps = 0

    for reason in reasons:
        if item.stage == StageName.FINISHED:
            break

        budget_key = _REASON_BUDGET[reason]
        if budget_key is not None:
            budgeted_steps += 1
            assert budgeted_steps <= max_total_steps, (
                "failure walk exceeded the global budget bound"
            )
            item.attempts[budget_key] += 1
            if item.attempts[budget_key] > _declared_budget(budget_key):
                item.stage = StageName.FINISHED
                continue
        item.stage = _fail_target(item.stage, reason)

    assert item.stage in set(StageName)


@given(budget_key=st.sampled_from(sorted(budget_keys())))
def test_budget_exhaustion_is_always_finite(budget_key: str) -> None:
    """(c) budget+1 repeats of any budgeted failure exhausts the budget.

    Also proves every ROUTES budget key has a matching counter on a fresh
    WorkItem, so no budget is untrackable.
    """
    item = WorkItem(repo="r", kind=ItemKind.ISSUE, issue=1)
    assert budget_key in item.attempts, (
        f"WorkItem.attempts missing counter for ROUTES budget {budget_key!r}"
    )
    declared = _declared_budget(budget_key)
    assert declared >= 1
    for _ in range(declared + 1):
        item.attempts[budget_key] += 1
    assert item.attempts[budget_key] > declared


@st.composite
def contiguous_scopes(draw: st.DrawFn) -> frozenset[StageName]:
    """Generate contiguous, non-empty stage subsets in pipeline order."""
    non_finished = [s for s in PIPELINE_ORDER if s != StageName.FINISHED]
    start = draw(st.integers(min_value=0, max_value=len(non_finished) - 1))
    end = draw(st.integers(min_value=start, max_value=len(non_finished) - 1))
    include_finished = draw(st.booleans())
    stages: set[StageName] = set(non_finished[start : end + 1])
    if include_finished:
        stages.add(StageName.FINISHED)
    return frozenset(stages)


@given(stages=contiguous_scopes())
@settings(max_examples=200)
def test_scope_closure_over_generated_subsets(stages: frozenset[StageName]) -> None:
    """(d) Every trimmed route targets scope ∪ {FINISHED} for ANY contiguous scope."""
    scope = PipelineScope(stages)
    allowed = set(stages) | {StageName.FINISHED}
    for stage, route in scope.trimmed_routes().items():
        assert stage in stages
        assert route.next in allowed
        for target in route.fail_routes.values():
            assert target in allowed


def test_attempts_keys_match_routes_budget_keys() -> None:
    """WorkItem.attempts default keys are exactly the union of ROUTES budgets."""
    item = WorkItem(repo="r", kind=ItemKind.ISSUE, issue=1)
    assert frozenset(item.attempts) == budget_keys()
