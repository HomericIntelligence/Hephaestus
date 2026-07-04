"""Tests for routing table, stages, and route computation."""

import pytest

from hephaestus.automation.pipeline import (
    ROUTES,
    Disposition,
    PipelineScope,
    Route,
    StageName,
    StageOutcome,
)


class TestStageName:
    """Tests for StageName enum."""

    def test_stage_name_values(self) -> None:
        """Verify all expected stages exist."""
        expected = {
            "repo",
            "planning",
            "plan_review",
            "implementation",
            "pr_review",
            "ci",
            "merge_wait",
            "finished",
        }
        assert {s.value for s in StageName} == expected

    def test_stage_name_string_behavior(self) -> None:
        """StageName inherits from str."""
        assert isinstance(StageName.REPO, str)
        assert StageName.REPO == "repo"


class TestDisposition:
    """Tests for Disposition enum."""

    def test_disposition_values(self) -> None:
        """Verify all expected dispositions exist."""
        expected = {
            "advance",
            "retry",
            "fail_back",
            "skip",
            "blocked",
            "finish_pass",
            "finish_fail",
        }
        assert {d.value for d in Disposition} == expected


class TestStageOutcome:
    """Tests for StageOutcome dataclass."""

    def test_stage_outcome_creation(self) -> None:
        """Create a StageOutcome with required and optional fields."""
        outcome = StageOutcome(disposition=Disposition.ADVANCE, note="test")
        assert outcome.disposition == Disposition.ADVANCE
        assert outcome.note == "test"

    def test_stage_outcome_default_note(self) -> None:
        """StageOutcome.note defaults to empty string."""
        outcome = StageOutcome(disposition=Disposition.ADVANCE)
        assert outcome.note == ""

    def test_stage_outcome_frozen(self) -> None:
        """StageOutcome is frozen (immutable)."""
        outcome = StageOutcome(disposition=Disposition.ADVANCE)
        with pytest.raises(AttributeError):
            outcome.disposition = Disposition.RETRY  # type: ignore


class TestRoute:
    """Tests for Route dataclass."""

    def test_route_minimal(self) -> None:
        """Create a Route with only next stage."""
        route = Route(next=StageName.PLANNING)
        assert route.next == StageName.PLANNING
        assert route.fail_routes == {}
        assert route.budgets == {}

    def test_route_with_fail_routes(self) -> None:
        """Create a Route with fail_routes."""
        route = Route(
            next=StageName.IMPLEMENTATION,
            fail_routes={"*": StageName.PLANNING, "conflict": StageName.REPO},
        )
        assert route.next == StageName.IMPLEMENTATION
        assert route.fail_routes["*"] == StageName.PLANNING
        assert route.fail_routes["conflict"] == StageName.REPO

    def test_route_with_budgets(self) -> None:
        """Create a Route with budgets."""
        route = Route(
            next=StageName.PLAN_REVIEW,
            budgets={"plan": 1, "plan_cycles": 2},
        )
        assert route.budgets["plan"] == 1
        assert route.budgets["plan_cycles"] == 2


class TestROUTES:
    """Tests for the ROUTES table."""

    def test_routes_completeness(self) -> None:
        """ROUTES contains all non-terminal stages and FINISHED."""
        stages = set(ROUTES.keys())
        expected = set(StageName)
        assert stages == expected

    def test_routes_structure(self) -> None:
        """Each ROUTES entry is a Route dataclass."""
        for _stage, route in ROUTES.items():
            assert isinstance(route, Route)
            assert isinstance(route.next, StageName)

    def test_repo_stage_routes_to_planning(self) -> None:
        """REPO stage routes to PLANNING."""
        assert ROUTES[StageName.REPO].next == StageName.PLANNING
        assert ROUTES[StageName.REPO].fail_routes["*"] == StageName.FINISHED

    def test_planning_stage_has_budgets(self) -> None:
        """PLANNING stage has plan and plan_cycles budgets."""
        route = ROUTES[StageName.PLANNING]
        assert route.budgets["plan"] == 1
        assert route.budgets["plan_cycles"] == 2

    def test_plan_review_stage_loops_back(self) -> None:
        """PLAN_REVIEW default fail target is PLANNING (loop back)."""
        route = ROUTES[StageName.PLAN_REVIEW]
        assert route.fail_routes["*"] == StageName.PLANNING

    def test_pr_review_stage_budgets(self) -> None:
        """PR_REVIEW has pr_review_iter, pr_review_hard, blocked_address budgets."""
        route = ROUTES[StageName.PR_REVIEW]
        assert route.budgets["pr_review_iter"] == 3
        assert route.budgets["pr_review_hard"] == 6
        assert route.budgets["blocked_address"] == 2

    def test_finished_stage_terminal(self) -> None:
        """FINISHED stage is terminal (routes to itself)."""
        route = ROUTES[StageName.FINISHED]
        assert route.next == StageName.FINISHED


class TestPipelineScope:
    """Tests for PipelineScope (trimming routes to a stage subset)."""

    def test_pipeline_scope_all_stages(self) -> None:
        """PipelineScope with all stages returns unchanged ROUTES."""
        all_stages = frozenset(StageName)
        scope = PipelineScope(all_stages)
        trimmed = scope.trimmed_routes()

        for stage in StageName:
            assert stage in trimmed or stage == StageName.FINISHED
            if stage in trimmed:
                assert trimmed[stage].next == ROUTES[stage].next

    def test_pipeline_scope_partial_subset(self) -> None:
        """PipelineScope trims PLANNING..PR_REVIEW subset."""
        stages = frozenset(
            {
                StageName.PLANNING,
                StageName.PLAN_REVIEW,
                StageName.IMPLEMENTATION,
                StageName.PR_REVIEW,
            }
        )
        scope = PipelineScope(stages)
        trimmed = scope.trimmed_routes()

        assert len(trimmed) == 4
        assert StageName.REPO not in trimmed
        assert StageName.CI not in trimmed

    def test_pipeline_scope_rewrites_out_of_scope_next_target(self) -> None:
        """Out-of-scope next targets are rewritten to FINISHED."""
        stages = frozenset({StageName.PLANNING})
        scope = PipelineScope(stages)
        trimmed = scope.trimmed_routes()

        # PLANNING.next is PLAN_REVIEW, which is out of scope
        route = trimmed[StageName.PLANNING]
        assert route.next == StageName.FINISHED

    def test_pipeline_scope_preserves_in_scope_targets(self) -> None:
        """In-scope targets are preserved."""
        stages = frozenset({StageName.PLAN_REVIEW, StageName.IMPLEMENTATION})
        scope = PipelineScope(stages)
        trimmed = scope.trimmed_routes()

        # PLAN_REVIEW.next is IMPLEMENTATION, which IS in scope
        route = trimmed[StageName.PLAN_REVIEW]
        assert route.next == StageName.IMPLEMENTATION

    def test_pipeline_scope_rewrites_fail_routes(self) -> None:
        """Out-of-scope fail targets are rewritten to FINISHED."""
        stages = frozenset({StageName.PR_REVIEW})
        scope = PipelineScope(stages)
        trimmed = scope.trimmed_routes()

        # PR_REVIEW.fail_routes["*"] is IMPLEMENTATION, which is out of scope
        route = trimmed[StageName.PR_REVIEW]
        assert route.fail_routes["*"] == StageName.FINISHED

    def test_pipeline_scope_finished_always_in_scope(self) -> None:
        """FINISHED is always a valid target (never out of scope)."""
        stages = frozenset({StageName.PLANNING})
        scope = PipelineScope(stages)
        trimmed = scope.trimmed_routes()

        # PLANNING.fail_routes["*"] is FINISHED, which stays FINISHED
        route = trimmed[StageName.PLANNING]
        assert route.fail_routes["*"] == StageName.FINISHED

    def test_pipeline_scope_caches_result(self) -> None:
        """trimmed_routes() caches its result."""
        stages = frozenset({StageName.PLANNING})
        scope = PipelineScope(stages)
        result1 = scope.trimmed_routes()
        result2 = scope.trimmed_routes()

        # Should return the same object (cached)
        assert result1 is result2
