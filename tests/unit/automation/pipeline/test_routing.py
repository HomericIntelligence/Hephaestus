"""Tests for routing table, stages, and route computation."""

from pathlib import Path

import pytest

import hephaestus.automation.pipeline.routing as routing
from hephaestus.automation.pipeline import (
    ROUTES,
    Disposition,
    PipelineScope,
    Route,
    StageName,
    StageOutcome,
)
from hephaestus.automation.pipeline.coordinator_types import _DRAIN_ORDER
from hephaestus.automation.pipeline.routing import (
    AUXILIARY_PIPELINE_ORDER,
    MAIN_PIPELINE_ORDER,
    PIPELINE_ORDER,
)

_ROUTE_CASES = tuple(
    pytest.param(stage, route, id=stage.value) for stage, route in ROUTES.items()
)
_SCOPE_CASES = tuple(
    pytest.param(
        frozenset(MAIN_PIPELINE_ORDER[start:stop]),
        id=f"{start}:{stop}",
    )
    for start in range(len(MAIN_PIPELINE_ORDER))
    for stop in range(start + 1, len(MAIN_PIPELINE_ORDER) + 1)
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
            "merge_wait",
            "learning",
            "finished",
        }
        assert {s.value for s in StageName} == expected

    def test_stage_name_string_behavior(self) -> None:
        """StageName inherits from str."""
        assert isinstance(StageName.REPO, str)
        assert StageName.REPO == "repo"

    def test_stage_name_docstring_warns_about_order(self) -> None:
        """StageName docstring must warn that declaration order is semantic."""
        assert StageName.__doc__ is not None
        assert "defines pipeline" in StageName.__doc__
        assert "not enum declaration order" in StageName.__doc__


class TestDisposition:
    """Tests for Disposition enum."""

    def test_disposition_values(self) -> None:
        """Verify all expected dispositions exist."""
        expected = {
            "advance",
            "retry",
            "fail_back",
            "skip",
            "eject",
            "blocked",
            "finish_pass",
            "finish_fail",
        }
        assert {d.value for d in Disposition} == expected

    def test_finish_fail_has_terminal_comment(self) -> None:
        """FINISH_FAIL carries the terse terminal-fail rationale comment.

        Stable against reorders (#2298): parses ``routing.py`` as a Python AST,
        locates ``Disposition.FINISH_FAIL``, and verifies the immediately-preceding
        source line carries the rationale comment. Asserts on the comment text
        and the proximity rule, not on a specific line number.
        """
        import ast

        source = Path(routing.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        disposition_cls = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "Disposition"
        )
        finish_fail_stmt = next(
            stmt
            for stmt in disposition_cls.body
            if isinstance(stmt, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "FINISH_FAIL"
                for target in stmt.targets
            )
        )
        finish_fail_line = finish_fail_stmt.lineno  # 1-indexed
        previous_line = source.splitlines()[finish_fail_line - 2]
        assert previous_line.strip() == "# terminal fail; no S105 needed", (
            f"expected rationale comment immediately above FINISH_FAIL; "
            f"got line {finish_fail_line - 1}: {previous_line!r}"
        )


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

    def test_learning_is_an_implicit_auxiliary_route(self) -> None:
        """Learning is available but does not break main-scope contiguity."""
        assert ROUTES[StageName.LEARNING].next is StageName.FINISHED
        scope = PipelineScope(frozenset({StageName.PLANNING, StageName.PLAN_REVIEW}))
        routes = scope.trimmed_routes()
        assert StageName.LEARNING in routes
        assert routes[StageName.LEARNING].next is StageName.FINISHED

    @pytest.mark.parametrize(("stage", "route"), _ROUTE_CASES)
    def test_route_entries_are_closed_and_valid(
        self, stage: StageName, route: Route
    ) -> None:
        """Every table row has valid targets and a default failure route."""
        assert route.next in ROUTES
        assert set(route.fail_routes.values()) <= set(ROUTES)
        assert all(name and limit > 0 for name, limit in route.budgets.items())
        if stage is StageName.FINISHED:
            assert route.next is StageName.FINISHED
        else:
            assert "*" in route.fail_routes

    def test_routes_drive_pipeline_and_drain_order(self) -> None:
        """Route insertion order drives both queue orders."""
        assert set(ROUTES) == set(StageName)
        assert tuple(ROUTES) == PIPELINE_ORDER
        assert PIPELINE_ORDER == MAIN_PIPELINE_ORDER + AUXILIARY_PIPELINE_ORDER
        assert AUXILIARY_PIPELINE_ORDER == (StageName.LEARNING, StageName.FINISHED)
        assert PIPELINE_ORDER[-1] is StageName.FINISHED
        assert tuple(reversed(PIPELINE_ORDER)) == _DRAIN_ORDER

    @pytest.mark.parametrize("stages", _SCOPE_CASES)
    def test_route_order_drives_every_contiguous_scope(
        self, stages: frozenset[StageName]
    ) -> None:
        """Every generated contiguous scope follows the route-derived order."""
        trimmed = PipelineScope(stages).trimmed_routes()
        allowed = set(stages) | set(AUXILIARY_PIPELINE_ORDER)
        expected_order = tuple(stage for stage in PIPELINE_ORDER if stage in allowed)

        assert tuple(trimmed) == expected_order
        assert all(route.next in allowed for route in trimmed.values())
        assert all(
            target in allowed
            for route in trimmed.values()
            for target in route.fail_routes.values()
        )

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

    def test_pr_review_advances_directly_to_merge_wait(self) -> None:
        """The active loop has no CI/CD stage between review and merge wait."""
        assert ROUTES[StageName.PR_REVIEW].next is StageName.MERGE_WAIT
        assert "ci_fix" not in routing.budget_keys()

    def test_merge_budget_provenance_uses_stable_source_references(self) -> None:
        """#1902: merge-budget provenance should not pin volatile line numbers."""
        assert routing.__file__ is not None
        source = Path(routing.__file__).read_text(encoding="utf-8")
        assert "loop_runner.py:" not in source
        assert "LoopConfig.drive_green_loops" in source
        assert "--drive-green-loops" in source


class TestPipelineScope:
    """Tests for PipelineScope (trimming routes to a stage subset)."""

    def test_pipeline_scope_all_stages(self) -> None:
        """PipelineScope with all stages returns unchanged ROUTES."""
        all_stages = frozenset(StageName)
        scope = PipelineScope(all_stages)
        trimmed = scope.trimmed_routes()

        assert StageName.FINISHED in trimmed
        for stage in ROUTES:
            assert stage in trimmed
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

        assert len(trimmed) == 6
        assert StageName.LEARNING in trimmed
        assert StageName.FINISHED in trimmed
        assert StageName.REPO not in trimmed
        assert StageName.MERGE_WAIT not in trimmed

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
        """A gapped scope silently drops stages — reject it."""
        with pytest.raises(ValueError, match="contiguous"):
            PipelineScope(frozenset({StageName.PLANNING, StageName.MERGE_WAIT}))

    def test_pipeline_scope_finished_never_breaks_contiguity(self) -> None:
        """FINISHED (universal sink) is allowed in any scope."""
        scope = PipelineScope(frozenset({StageName.MERGE_WAIT, StageName.FINISHED}))
        assert StageName.MERGE_WAIT in scope.trimmed_routes()

    def test_pipeline_scope_finished_only(self) -> None:
        """A FINISHED-only scope is valid (empty ordered prefix) and terminal."""
        scope = PipelineScope(frozenset({StageName.FINISHED}))
        trimmed = scope.trimmed_routes()
        assert set(trimmed) == {StageName.LEARNING, StageName.FINISHED}
        assert trimmed[StageName.FINISHED] == ROUTES[StageName.FINISHED]
