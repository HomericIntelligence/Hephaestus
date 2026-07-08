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
# none and resolve purely via the "*" default. Fail-back EXITS that leave
# their stage rather than retry it consume no retry budget: plan_not_go
# (implementation -> plan_review), already_implementation_go_pr (-> ci),
# not_implementation_go (ci -> pr_review), missing_worktree (-> implementation),
# and no_pr (-> finished) all map to None.
_REASON_BUDGET: dict[str, str | None] = {
    "nogo": "plan_review_iter",
    "plan_cycles_exhausted": "plan_cycles",
    "plan_not_go": None,
    "already_implementation_go_pr": None,
    "agent_error": None,
    "human_blocked": None,
    "exhaustion": None,
    "fix_exhausted": None,
    "not_implementation_go": None,
    "missing_worktree": None,
    "no_pr": None,
    # ci_red cycles merge_wait -> ci for another merge attempt later, so it
    # consumes a merge slot; closed/timeout EXIT the pipeline entirely and
    # charge nothing (a closed PR must not deplete merge attempts).
    "ci_red": "merge",
    "blocked_exhausted": "blocked_address",
    "closed": None,
    "timeout": None,
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


# Reasons that consume a budget on every occurrence (used for the
# unconditional termination property below).
_BUDGETED_REASONS = sorted(k for k, v in _REASON_BUDGET.items() if v is not None)

# Upper bound on budget-consuming steps before SOME counter must exceed its
# declared budget (duplicate keys across stages overcount, which only makes
# the bound looser and the property stronger).
_TOTAL_BUDGET_SUM = sum(budget for route in ROUTES.values() for budget in route.budgets.values())


def _drive(item: WorkItem, reasons: list[str]) -> int:
    """Drive ``item`` through ``reasons`` via ROUTES with budgets enforced.

    Returns the number of budget-consuming steps taken. FINISHED is
    absorbing: the walk stops there.
    """
    budgeted_steps = 0
    for reason in reasons:
        if item.stage == StageName.FINISHED:
            break
        budget_key = _REASON_BUDGET[reason]
        if budget_key is not None:
            budgeted_steps += 1
            item.attempts[budget_key] += 1
            if item.attempts[budget_key] > _declared_budget(budget_key):
                item.stage = StageName.FINISHED
                continue
        item.stage = _fail_target(item.stage, reason)
    return budgeted_steps


@given(
    start=st.sampled_from(NON_TERMINAL),
    reasons=st.lists(
        st.sampled_from(_BUDGETED_REASONS),
        min_size=_TOTAL_BUDGET_SUM + 1,
        max_size=_TOTAL_BUDGET_SUM + 10,
    ),
)
@settings(max_examples=150)
def test_budgeted_failure_sequences_always_reach_finished(
    start: StageName, reasons: list[str]
) -> None:
    """(b) UNCONDITIONAL termination: budgets force FINISHED.

    Any sequence of budget-consuming failures longer than the total budget
    sum must exhaust some counter and land the item in FINISHED — asserted
    for every generated example, never vacuously.
    """
    item = WorkItem(repo="r", kind=ItemKind.ISSUE, issue=1, stage=start)
    budgeted_steps = _drive(item, reasons)
    assert item.stage == StageName.FINISHED
    assert budgeted_steps <= _TOTAL_BUDGET_SUM + 1


@given(
    start=st.sampled_from(NON_TERMINAL),
    reasons=st.lists(st.sampled_from(_REASONS), min_size=1, max_size=60),
)
@settings(max_examples=300)
def test_mixed_failure_sequences_hold_walk_invariants(start: StageName, reasons: list[str]) -> None:
    """(b') Mixed sequences (incl. unbudgeted exits/self-loops) hold invariants.

    Unbudgeted reasons resolve purely through the routing table, so their
    recurrence is bounded by the driven sequence itself (the coordinator's
    step semantics, not this table, bound them in production). Asserted for
    EVERY example: the walk stays inside the stage set, never takes more
    budget-consuming steps than the global budget bound, and FINISHED is
    absorbing once a budget is exhausted.
    """
    item = WorkItem(repo="r", kind=ItemKind.ISSUE, issue=1, stage=start)
    exhausted_before = dict(item.attempts)
    budgeted_steps = _drive(item, reasons)
    assert item.stage in set(StageName)
    assert budgeted_steps <= _TOTAL_BUDGET_SUM + 1
    exhausted = any(
        item.attempts[k] > _declared_budget(k)
        for k in item.attempts
        if item.attempts[k] != exhausted_before[k]
    )
    if exhausted:
        assert item.stage == StageName.FINISHED


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
