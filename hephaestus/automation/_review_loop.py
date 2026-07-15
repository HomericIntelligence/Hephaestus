"""Pure state machine for the bounded implementation-review loop.

The compatibility facade in :mod:`hephaestus.automation._review_phase` binds
the side-effecting review, address, status, and finalization operations.  This
module owns only the state transitions and therefore remains straightforward to
exercise without GitHub, Git, agents, or persisted stage state.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class ReviewLoopState:
    """State supplied to one review or address operation."""

    iteration: int
    prior_review: str | None = None
    prior_addressed_threads: tuple[dict[str, Any], ...] = ()
    reopened_keys: frozenset[str] = frozenset()
    pending_unaddressed: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ReviewIterationOutcome:
    """The side-effecting review operation's typed result."""

    verdict: str | None
    grade: str | None
    review_text: str
    posted_thread_ids: tuple[str, ...]
    go_blocked_by_automation: bool
    reopened: tuple[str, ...]
    should_break: bool
    reopened_keys: frozenset[str]
    validator_clean: bool


@dataclass(frozen=True)
class AddressOutcome:
    """The side-effecting address operation's typed result."""

    prior_addressed_threads: tuple[dict[str, Any], ...]
    addressed: bool


@dataclass(frozen=True)
class ReviewLoopResult:
    """Terminal outcome returned to the review-phase compatibility facade."""

    iterations_run: int
    verdict: str | None
    grade: str | None


@dataclass(frozen=True)
class ReviewLoopOperations:
    """Effects invoked by :class:`ReviewLoopCoordinator` at explicit seams."""

    resolve_conflict: Callable[[], bool]
    review_iteration: Callable[[ReviewLoopState], ReviewIterationOutcome]
    address_iteration: Callable[[ReviewLoopState], AddressOutcome]
    finalize: Callable[[ReviewLoopResult], None]
    budget_extended: Callable[[int], None]


class ReviewLoopCoordinator:
    """Advance the bounded review/address loop without direct I/O dependencies."""

    def __init__(
        self,
        *,
        soft_limit: int,
        hard_limit: int,
        operations: ReviewLoopOperations,
    ) -> None:
        """Configure the bounded state machine and its injected effects."""
        if soft_limit < 1 or hard_limit < soft_limit:
            raise ValueError("review-loop limits must satisfy 1 <= soft_limit <= hard_limit")
        self._soft_limit = soft_limit
        self._hard_limit = hard_limit
        self._operations = operations

    def run(self, *, has_pr: bool) -> ReviewLoopResult:
        """Run conflict resolution, review iterations, and terminal finalization."""
        if has_pr and not self._operations.resolve_conflict():
            return self._finalize(ReviewLoopResult(0, "NOGO", None))

        budget = self._soft_limit
        made_progress = False
        state = ReviewLoopState(iteration=0)
        result = ReviewLoopResult(0, None, None)
        while state.iteration < budget:
            budget = self._extend_budget_if_earned(state, budget, made_progress)
            made_progress = False
            review = self._operations.review_iteration(state)
            result = ReviewLoopResult(state.iteration + 1, review.verdict, review.grade)
            next_state = replace(
                state,
                prior_review=review.review_text,
                reopened_keys=review.reopened_keys,
            )
            if review.should_break:
                break
            if has_pr and self._should_re_review_without_address(review):
                state = replace(next_state, iteration=state.iteration + 1)
                continue
            if state.iteration == budget - 1:
                break
            if not has_pr:
                state = replace(next_state, iteration=state.iteration + 1)
                continue
            address = self._operations.address_iteration(next_state)
            state, made_progress, should_stop = self._advance_after_address(
                next_state,
                address,
                review.validator_clean,
                budget,
            )
            if should_stop:
                break
        return self._finalize(result)

    def _extend_budget_if_earned(
        self,
        state: ReviewLoopState,
        budget: int,
        made_progress: bool,
    ) -> int:
        """Grant one extra review when the preceding address pass made progress."""
        if made_progress and state.iteration == budget - 1 and budget < self._hard_limit:
            budget += 1
            self._operations.budget_extended(budget)
        return budget

    @staticmethod
    def _should_re_review_without_address(review: ReviewIterationOutcome) -> bool:
        """Identify the clean no-thread NOGO convergence path."""
        return (
            not review.posted_thread_ids
            and not review.reopened
            and not review.go_blocked_by_automation
            and review.validator_clean
        )

    @staticmethod
    def _advance_after_address(
        state: ReviewLoopState,
        address: AddressOutcome,
        validator_clean: bool,
        budget: int,
    ) -> tuple[ReviewLoopState, bool, bool]:
        """Carry address results into the next review or stop a stuck loop."""
        next_iteration = state.iteration + 1
        if not address.addressed:
            if address.prior_addressed_threads and state.iteration < budget - 1:
                return (
                    replace(
                        state,
                        iteration=next_iteration,
                        prior_addressed_threads=address.prior_addressed_threads,
                        pending_unaddressed=address.prior_addressed_threads,
                    ),
                    False,
                    False,
                )
            return state, False, True
        return (
            replace(
                state,
                iteration=next_iteration,
                prior_addressed_threads=address.prior_addressed_threads,
                pending_unaddressed=(),
            ),
            validator_clean,
            False,
        )

    def _finalize(self, result: ReviewLoopResult) -> ReviewLoopResult:
        """Invoke the terminal effect once and return its immutable result."""
        self._operations.finalize(result)
        return result
