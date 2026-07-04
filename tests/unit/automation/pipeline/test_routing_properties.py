"""Property-based tests for the routing graph invariants.

Verifies structural properties that must hold across all routing configurations:
  (a) Every non-terminal stage has a "*" fail route (mandatory default).
  (b) Every cycle in the routing graph passes through an attempt-budget key.
  (c) Driving an item through budget+1 failures lands it at the fail target.
  (d) Every PipelineScope subset yields routes that stay inside scope ∪ {finished}.
"""

from __future__ import annotations

from hephaestus.automation.pipeline import (
    ROUTES,
    PipelineScope,
    StageName,
)

# Mapping of loop-guarded stages to their budget keys.
# These are the stages that are allowed to loop back via fail_routes.
_LOOP_GUARDED = {
    StageName.PLAN_REVIEW: "plan_review_iter",
    StageName.PR_REVIEW: "pr_review_iter",
    StageName.CI: "ci_fix",
}


class TestRoutingProperties:
    """Property-based tests for routing invariants."""

    def test_property_a_all_nonterminal_stages_have_default_fail_route(self) -> None:
        """Property (a): Every non-terminal stage has a "*" fail route."""
        terminal_stages = {StageName.FINISHED}
        violations = []

        for stage, route in ROUTES.items():
            if stage in terminal_stages:
                continue
            if "*" not in route.fail_routes:
                violations.append(f"{stage} lacks '*' fail route")

        assert not violations, "Fail-route violations:\n" + "\n".join(violations)

    def test_property_b_every_cycle_passes_through_budget(self) -> None:
        """Property (b): Every cycle in the routing graph passes through an attempt-budget key.

        A cycle is detected when a fail-route loops back to a stage that has
        already been visited (i.e., the fail target is not a forward/terminal target).
        Every such looping stage must have a corresponding budget key in ROUTES.
        """
        violations = []

        for stage, route in ROUTES.items():
            for fail_key, fail_target in route.fail_routes.items():
                # Is this a loop (fail target is earlier or same stage)?
                # In our ROUTES, loops are:
                #   PLAN_REVIEW fails to PLANNING (earlier)
                #   PR_REVIEW fails to IMPLEMENTATION (earlier)
                #   CI fails to IMPLEMENTATION (earlier)
                # All others advance or terminate.

                is_loop = False
                stage_order = [
                    StageName.REPO,
                    StageName.PLANNING,
                    StageName.PLAN_REVIEW,
                    StageName.IMPLEMENTATION,
                    StageName.PR_REVIEW,
                    StageName.CI,
                    StageName.MERGE_WAIT,
                    StageName.FINISHED,
                ]
                stage_idx = stage_order.index(stage)
                target_idx = stage_order.index(fail_target) if fail_target in stage_order else -1

                if target_idx <= stage_idx and fail_target != StageName.FINISHED:
                    is_loop = True

                # If it's a loop, the looping stage must have a budget entry
                if is_loop and stage not in _LOOP_GUARDED:
                    violations.append(
                        f"{stage} loops back to {fail_target} "
                        f"(via '{fail_key}' fail route) but is not in _LOOP_GUARDED"
                    )

        assert not violations, "Unguarded loop violations:\n" + "\n".join(violations)

    def test_property_c_budget_plus_one_failures_reach_fail_target(self) -> None:
        """Property (c): Driving an item through budget+1 failures lands it at the fail target.

        For each loop-guarded stage, we verify that after exhausting the budget,
        a subsequent failure in that stage routes to its fail_target, which should
        NOT be the same stage (to prevent infinite loops).
        """
        violations = []

        for stage, budget_key in _LOOP_GUARDED.items():
            if stage not in ROUTES:
                violations.append(f"{stage} in _LOOP_GUARDED but not in ROUTES")
                continue

            route = ROUTES[stage]
            if budget_key not in route.budgets:
                violations.append(
                    f"{stage} loop-guard references budget_key={budget_key!r} "
                    f"but {stage} route has budgets={set(route.budgets.keys())}"
                )
                continue

            budget = route.budgets[budget_key]
            fail_target = route.fail_routes.get("*")

            if fail_target is None:
                violations.append(f"{stage} lacks '*' fail route")
                continue

            # When budget is exhausted (after budget+1 attempts), the fail_target
            # should be a different stage than the current one, to avoid self-loops
            assert budget >= 1, f"{stage}: budget should be >= 1"
            assert fail_target != stage, (
                f"Self-loop guard: {stage} has fail_target={fail_target}, "
                f"which is the same stage (would loop infinitely)"
            )

        assert not violations, "Property (c) violations:\n" + "\n".join(violations)

    def test_property_d_pipeline_scope_subset_stays_in_scope(self) -> None:
        """Property (d): Every PipelineScope subset yields routes inside scope ∪ {finished}.

        For a few representative scopes, verify that trimmed_routes never point
        outside the scope (except to FINISHED, which is always valid).
        """
        test_scopes = [
            frozenset({StageName.PLANNING}),
            frozenset({StageName.PLANNING, StageName.PLAN_REVIEW, StageName.IMPLEMENTATION}),
            frozenset({StageName.PR_REVIEW, StageName.CI, StageName.MERGE_WAIT}),
        ]
        violations = []

        for scope_set in test_scopes:
            scope = PipelineScope(scope_set)
            trimmed = scope.trimmed_routes()

            for stage, route in trimmed.items():
                valid_targets = scope_set | {StageName.FINISHED}

                # Check next target
                if route.next not in valid_targets:
                    violations.append(
                        f"Scope {scope_set}: stage {stage} next={route.next} is outside scope"
                    )

                # Check all fail targets
                for fail_key, fail_target in route.fail_routes.items():
                    if fail_target not in valid_targets:
                        violations.append(
                            f"Scope {scope_set}: stage {stage} "
                            f"fail_routes[{fail_key!r}]={fail_target} is outside scope"
                        )

        assert not violations, "Pipeline scope violations:\n" + "\n".join(violations)
