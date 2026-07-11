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

    def test_stage_name_docstring_warns_about_order(self) -> None:
        """StageName docstring must warn that declaration order is semantic."""
        assert StageName.__doc__ is not None
        assert "pipeline order" in StageName.__doc__
        assert "MUST NOT be reordered" in StageName.__doc__


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

    def test_finish_fail_has_terminal_comment(self) -> None:
        """FINISH_FAIL keeps the terse terminal-fail rationale comment."""
        source_lines = Path(routing.__file__).read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(source_lines):
            if line.strip() == 'FINISH_FAIL = "finish_fail"':
                assert index > 0
                assert source_lines[index - 1].strip() == "# terminal fail; no S105 needed"
                break
        else:
            pytest.fail("FINISH_FAIL enum member not found")


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
                next=StageName.FINISHED,
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
                    "missing_worktree": StageName.IMPLEMENTATION,
                    "no_pr": StageName.FINISHED,
                    "*": StageName.CI,
                },
                budgets={"ci_fix": 1, "rebase": 2},
            ),
            StageName.MERGE_WAIT: Route(
                next=StageName.FINISHED,
                fail_routes={
                    "closed": StageName.FINISHED,
                    "*": StageName.FINISHED,
                },
                budgets={},
            ),
            StageName.FINISHED: Route(next=StageName.FINISHED),
        }
        assert expected == ROUTES

    def test_legacy_implementation_go_docs_match_ci_maintenance_behavior(self) -> None:
        """The architecture doc must not claim CI immediately terminates legacy GO work."""
        root = Path(__file__).resolve().parents[4]
        text = (root / "docs" / "AUTOMATION_LOOP_ARCHITECTURE.md").read_text(encoding="utf-8")
        normalized = " ".join(text.split())

        assert "`ci` may perform bounded rebase, polling, and CI-fix work" in normalized
        assert "`ci` immediately verifies auto-merge is" not in normalized

    def test_merge_budget_provenance_uses_stable_source_references(self) -> None:
        """#1902: merge-budget provenance should not pin volatile line numbers."""
        assert routing.__file__ is not None
        source = Path(routing.__file__).read_text(encoding="utf-8")
        assert "loop_runner.py:" not in source
        assert "LoopConfig.max_merge_attempts" in source
        assert "--max-merge-attempts" in source


class TestPipelineScope:
    """Tests for PipelineScope (trimming routes to a stage subset)."""

    def test_pipeline_scope_all_stages(self) -> None:
        """PipelineScope with all stages returns unchanged ROUTES."""
        all_stages = frozenset(StageName)
        scope = PipelineScope(all_stages)
        trimmed = scope.trimmed_routes()

        assert StageName.FINISHED in trimmed
        for stage in StageName:
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

    def test_pipeline_scope_finished_only(self) -> None:
        """A FINISHED-only scope is valid (empty ordered prefix) and terminal."""
        scope = PipelineScope(frozenset({StageName.FINISHED}))
        trimmed = scope.trimmed_routes()
        assert trimmed == {StageName.FINISHED: ROUTES[StageName.FINISHED]}
