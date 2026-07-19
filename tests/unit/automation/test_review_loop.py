"""Behavioural regression tests for the pure implementation-review loop."""

from __future__ import annotations

from typing import Any

import pytest

from hephaestus.automation._review_loop import (
    AddressOutcome,
    ReviewIterationOutcome,
    ReviewLoopCoordinator,
    ReviewLoopOperations,
    ReviewLoopResult,
    ReviewLoopState,
)


def _outcome(
    *,
    verdict: str = "NOGO",
    posted_thread_ids: tuple[str, ...] = ("thread",),
    reopened: tuple[str, ...] = (),
    should_break: bool = False,
    reopened_keys: frozenset[str] = frozenset(),
    validator_clean: bool = True,
) -> ReviewIterationOutcome:
    """Build a review outcome with the normal non-GO defaults."""
    return ReviewIterationOutcome(
        verdict=verdict,
        grade="B",
        review_text=f"review-{verdict}",
        posted_thread_ids=posted_thread_ids,
        go_blocked_by_automation=False,
        reopened=reopened,
        should_break=should_break,
        reopened_keys=reopened_keys,
        validator_clean=validator_clean,
    )


def _coordinator(
    *,
    resolve_conflict: Any = lambda: True,
    review_iteration: Any = lambda _state: _outcome(),
    address_iteration: Any = lambda _state: AddressOutcome((), True),
    finalize: Any = lambda _result: None,
    budget_extended: Any = lambda _budget: None,
    soft_limit: int = 3,
    hard_limit: int = 6,
) -> ReviewLoopCoordinator:
    """Build a coordinator from deterministic callback fakes."""
    return ReviewLoopCoordinator(
        soft_limit=soft_limit,
        hard_limit=hard_limit,
        operations=ReviewLoopOperations(
            resolve_conflict=resolve_conflict,
            review_iteration=review_iteration,
            address_iteration=address_iteration,
            finalize=finalize,
            budget_extended=budget_extended,
        ),
    )


def test_conflict_resolution_precedes_iteration_zero() -> None:
    """A PR is conflict-checked before its first reviewer invocation."""
    calls: list[str] = []

    def _resolve_conflict() -> bool:
        calls.append("conflict")
        return True

    def _review_iteration(_state: ReviewLoopState) -> ReviewIterationOutcome:
        calls.append("review")
        return _outcome(verdict="GO", should_break=True)

    coordinator = _coordinator(
        resolve_conflict=_resolve_conflict,
        review_iteration=_review_iteration,
    )

    coordinator.run(has_pr=True)

    assert calls == ["conflict", "review"]


def test_unresolved_conflict_finalizes_nogo_without_reviewer() -> None:
    """An unresolved conflict consumes no review iteration."""
    reviews: list[ReviewLoopState] = []
    finalizations: list[ReviewLoopResult] = []
    coordinator = _coordinator(
        resolve_conflict=lambda: False,
        review_iteration=reviews.append,
        finalize=finalizations.append,
    )

    result = coordinator.run(has_pr=True)

    assert result == ReviewLoopResult(iterations_run=0, verdict="NOGO", grade=None)
    assert reviews == []
    assert finalizations == [result]


def test_clean_go_terminates_without_addressing() -> None:
    """A clean GO stops the loop immediately."""
    addressed: list[ReviewLoopState] = []
    coordinator = _coordinator(
        review_iteration=lambda _state: _outcome(verdict="GO", should_break=True),
        address_iteration=addressed.append,
    )

    result = coordinator.run(has_pr=True)

    assert result == ReviewLoopResult(iterations_run=1, verdict="GO", grade="B")
    assert addressed == []


def test_zero_thread_clean_nogo_retries_without_addressing() -> None:
    """A clean no-thread NOGO is re-reviewed instead of sent to an agent."""
    outcomes = iter(
        [
            _outcome(posted_thread_ids=()),
            _outcome(verdict="GO", posted_thread_ids=(), should_break=True),
        ]
    )
    addressed: list[ReviewLoopState] = []
    coordinator = _coordinator(
        review_iteration=lambda _state: next(outcomes),
        address_iteration=addressed.append,
    )

    result = coordinator.run(has_pr=True)

    assert result.iterations_run == 2
    assert addressed == []


def test_validator_reopening_forces_address_path() -> None:
    """Validator reopenings remain actionable even without reviewer threads."""
    outcomes = iter(
        [
            _outcome(
                posted_thread_ids=(),
                reopened=("reopened",),
                reopened_keys=frozenset({"key"}),
                validator_clean=False,
            ),
            _outcome(verdict="GO", should_break=True),
        ]
    )
    addressed: list[ReviewLoopState] = []

    def _address_iteration(state: ReviewLoopState) -> AddressOutcome:
        addressed.append(state)
        return AddressOutcome(({"id": "thread"},), True)

    coordinator = _coordinator(
        review_iteration=lambda _state: next(outcomes),
        address_iteration=_address_iteration,
    )

    coordinator.run(has_pr=True)

    assert len(addressed) == 1
    assert addressed[0].reopened_keys == frozenset({"key"})


def test_prior_threads_and_reopened_keys_flow_to_next_iteration() -> None:
    """Each re-review receives the previous address and validation state."""
    observed: list[ReviewLoopState] = []
    outcomes = iter(
        [
            _outcome(reopened_keys=frozenset({"key"})),
            _outcome(verdict="GO", should_break=True),
        ]
    )

    def _review_iteration(state: ReviewLoopState) -> ReviewIterationOutcome:
        observed.append(state)
        return next(outcomes)

    coordinator = _coordinator(
        review_iteration=_review_iteration,
        address_iteration=lambda _state: AddressOutcome(({"id": "thread"},), True),
    )

    coordinator.run(has_pr=True)

    assert observed[1].prior_addressed_threads == ({"id": "thread"},)
    assert observed[1].reopened_keys == frozenset({"key"})


def test_no_commit_turn_carries_findings_into_one_retry() -> None:
    """A no-commit address pass supplies its unresolved findings to the retry."""
    address_states: list[ReviewLoopState] = []
    outcomes = iter([_outcome(), _outcome(), _outcome(verdict="GO", should_break=True)])

    def _address(state: ReviewLoopState) -> AddressOutcome:
        address_states.append(state)
        if len(address_states) == 1:
            return AddressOutcome(({"id": "still-open"},), False)
        return AddressOutcome((), True)

    coordinator = _coordinator(
        review_iteration=lambda _state: next(outcomes),
        address_iteration=_address,
    )

    coordinator.run(has_pr=True)

    assert address_states[1].pending_unaddressed == ({"id": "still-open"},)


def test_progress_extends_soft_budget_without_exceeding_hard_cap() -> None:
    """Progress earns a re-review, but the hard iteration limit remains absolute."""
    reviewed: list[int] = []
    extended: list[int] = []
    outcomes = iter([_outcome(), _outcome(), _outcome(verdict="GO", should_break=True)])

    def _review_iteration(state: ReviewLoopState) -> ReviewIterationOutcome:
        reviewed.append(state.iteration)
        return next(outcomes)

    coordinator = _coordinator(
        soft_limit=2,
        hard_limit=3,
        review_iteration=_review_iteration,
        budget_extended=extended.append,
    )

    result = coordinator.run(has_pr=True)

    assert result.iterations_run == 3
    assert reviewed == [0, 1, 2]
    assert extended == [3]


def test_no_progress_stops_at_soft_budget() -> None:
    """A validator reopening prevents a soft-budget extension."""
    reviewed: list[int] = []

    def _review_iteration(state: ReviewLoopState) -> ReviewIterationOutcome:
        reviewed.append(state.iteration)
        return _outcome(validator_clean=False)

    coordinator = _coordinator(
        soft_limit=2,
        hard_limit=4,
        review_iteration=_review_iteration,
    )

    result = coordinator.run(has_pr=True)

    assert result.iterations_run == 2
    assert reviewed == [0, 1]


def test_pr_less_mode_advances_without_address_operation() -> None:
    """Diff-only review never invokes an address operation with no PR threads."""
    addressed: list[ReviewLoopState] = []
    coordinator = _coordinator(
        soft_limit=2,
        review_iteration=lambda _state: _outcome(posted_thread_ids=()),
        address_iteration=addressed.append,
    )

    result = coordinator.run(has_pr=False)

    assert result.iterations_run == 2
    assert addressed == []


@pytest.mark.parametrize("has_pr", [False, True])
def test_finalization_occurs_once(has_pr: bool) -> None:
    """Both loop modes report one and only one terminal result."""
    finalizations: list[ReviewLoopResult] = []
    coordinator = _coordinator(
        review_iteration=lambda _state: _outcome(verdict="GO", should_break=True),
        finalize=finalizations.append,
    )

    result = coordinator.run(has_pr=has_pr)

    assert finalizations == [result]
