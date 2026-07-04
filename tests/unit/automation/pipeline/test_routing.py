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

    def test_repo_item_is_terminal(self) -> None:
        """The repo item advances to FINISHED; seeded issues enter their own queues."""
        assert ROUTES[StageName.REPO].next == StageName.FINISHED
        assert ROUTES[StageName.REPO].fail_routes["*"] == StageName.FINISHED
        assert ROUTES[StageName.REPO].budgets == {"clone": 2}

    def test_plan_review_stage_loops_back(self) -> None:
        """PLAN_REVIEW default fail target is PLANNING (loop back)."""
        route = ROUTES[StageName.PLAN_REVIEW]
        assert route.fail_routes["*"] == StageName.PLANNING

    def test_finished_stage_terminal(self) -> None:
        """FINISHED stage is terminal (routes to itself)."""
        route = ROUTES[StageName.FINISHED]
        assert route.next == StageName.FINISHED

    def test_routes_match_architecture_doc_table(self) -> None:
        """Pin the FULL table to docs/AUTOMATION_LOOP_ARCHITECTURE.md "ROUTES table".

        The doc is the epic #1809 contract; any divergence between this
        literal transcription and routing.ROUTES is a bug in one of the two.
        """
        expected: dict[StageName, Route] = {
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
                    "already_implementation_go_pr": StageName.CI,
                    "*": StageName.FINISHED,
                },
                budgets={"implement": 2, "test_fix": 1},
            ),
            StageName.PR_REVIEW: Route(
                next=StageName.CI,
                fail_routes={
                    "agent_error": StageName.IMPLEMENTATION,
                    "human_blocked": StageName.FINISHED,
                    "exhaustion": StageName.FINISHED,
                    "*": StageName.PR_REVIEW,
                },
                budgets={"pr_review_iter": 3, "pr_review_hard": 6},
            ),
            StageName.CI: Route(
                next=StageName.MERGE_WAIT,
                fail_routes={
                    "fix_exhausted": StageName.IMPLEMENTATION,
                    "not_implementation_go": StageName.PR_REVIEW,
                    "no_pr": StageName.FINISHED,
                    "*": StageName.CI,
                },
                budgets={"ci_fix": 1, "rebase": 2},
            ),
            StageName.MERGE_WAIT: Route(
                next=StageName.FINISHED,
                fail_routes={
                    "ci_red": StageName.CI,
                    "blocked_exhausted": StageName.PR_REVIEW,
                    "closed": StageName.FINISHED,
                    "timeout": StageName.FINISHED,
                    "*": StageName.FINISHED,
                },
                budgets={"blocked_address": 2, "rebase": 2, "merge": 1},
            ),
            StageName.FINISHED: Route(next=StageName.FINISHED),
        }
        assert expected == ROUTES


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

        # PR_REVIEW.fail_routes["agent_error"] is IMPLEMENTATION (out of
        # scope) → rewritten; "*" self-targets PR_REVIEW (in scope) → kept.
        route = trimmed[StageName.PR_REVIEW]
        assert route.fail_routes["agent_error"] == StageName.FINISHED
        assert route.fail_routes["*"] == StageName.PR_REVIEW

    def test_pipeline_scope_finished_always_in_scope(self) -> None:
        """FINISHED is always a valid target (never out of scope)."""
        stages = frozenset({StageName.PLANNING})
        scope = PipelineScope(stages)
        trimmed = scope.trimmed_routes()

        # PLANNING.fail_routes["*"] is FINISHED, which stays FINISHED
        route = trimmed[StageName.PLANNING]
        assert route.fail_routes["*"] == StageName.FINISHED

    def test_pipeline_scope_returns_defensive_copies(self) -> None:
        """Mutating a trimmed result never corrupts ROUTES or later calls."""
        stages = frozenset({StageName.PLANNING})
        scope = PipelineScope(stages)
        result1 = scope.trimmed_routes()
        result1[StageName.PLANNING].budgets["plan"] = 99
        result1[StageName.PLANNING].fail_routes["*"] = StageName.REPO

        assert ROUTES[StageName.PLANNING].budgets["plan"] == 2
        result2 = scope.trimmed_routes()
        assert result2[StageName.PLANNING].budgets["plan"] == 2
        assert result2[StageName.PLANNING].fail_routes["*"] == StageName.FINISHED

    def test_pipeline_scope_rejects_empty(self) -> None:
        """An empty scope is a caller bug and raises."""
        with pytest.raises(ValueError, match="at least one stage"):
            PipelineScope(frozenset())

    def test_pipeline_scope_rejects_non_contiguous(self) -> None:
        """A gapped scope (e.g. planning + ci) silently drops stages — reject it."""
        with pytest.raises(ValueError, match="contiguous"):
            PipelineScope(frozenset({StageName.PLANNING, StageName.CI}))

    def test_pipeline_scope_finished_never_breaks_contiguity(self) -> None:
        """FINISHED (universal sink) is allowed in any scope."""
        scope = PipelineScope(frozenset({StageName.MERGE_WAIT, StageName.FINISHED}))
        assert StageName.MERGE_WAIT in scope.trimmed_routes()
