"""Tests for the stage base contract (protocol, context, step-result types)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from hephaestus.automation.pipeline import ROUTES
from hephaestus.automation.pipeline.routing import Disposition, StageOutcome
from hephaestus.automation.pipeline.stages import (
    PlanningStage,
    PlanReviewStage,
    Stage,
    base as stage_base,
)
from hephaestus.automation.pipeline.stages.base import (
    Continue,
    JobRequest,
    StageContext,
    agent_provider,
)

KNOWN_ROUTE_BUDGETS: tuple[tuple[str, int], ...] = tuple(
    (budget_name, budget_value)
    for route in ROUTES.values()
    for budget_name, budget_value in route.budgets.items()
)


class TestStageContext:
    """StageContext accessor behavior with and without injected callables."""

    def _bare_ctx(self, **overrides: Any) -> StageContext:
        defaults: dict[str, Any] = {
            "config": object(),
            "org": "test-org",
            "dry_run": False,
            "github": object(),
            "paths": object(),
        }
        defaults.update(overrides)
        return StageContext(**defaults)

    def test_now_uses_injected_clock(self) -> None:
        """now() returns the injected fake clock's value."""
        ctx = self._bare_ctx(now_fn=lambda: 1234.5)
        assert ctx.now() == 1234.5

    def test_now_defaults_to_wall_clock(self) -> None:
        """now() without an injected clock returns epoch seconds."""
        ctx = self._bare_ctx()
        assert ctx.now() > 1_000_000_000.0

    def test_budget_uses_injected_lookup(self) -> None:
        """budget() returns the injected routing lookup's value."""
        ctx = self._bare_ctx(budget_fn=lambda name: {"plan": 7}.get(name, 0))
        assert ctx.budget("plan") == 7

    @pytest.mark.parametrize(("budget_name", "expected"), KNOWN_ROUTE_BUDGETS)
    def test_budget_defaults_to_route_budget_for_known_keys(
        self, budget_name: str, expected: int
    ) -> None:
        """budget() falls back to the declared ROUTES budget for known keys."""
        ctx = self._bare_ctx()
        assert ctx.budget(budget_name) == expected

    def test_budget_unknown_key_defaults_conservatively(self) -> None:
        """budget() without a lookup returns 1 for unknown keys only."""
        ctx = self._bare_ctx()
        assert ctx.budget("anything") == 1


class TestStageProtocol:
    """Both concrete stages satisfy the runtime-checkable Stage protocol."""

    def test_planning_stage_is_a_stage(self) -> None:
        """PlanningStage structurally satisfies Stage."""
        assert isinstance(PlanningStage(), Stage)

    def test_plan_review_stage_is_a_stage(self) -> None:
        """PlanReviewStage structurally satisfies Stage."""
        assert isinstance(PlanReviewStage(), Stage)

    def test_non_stage_rejected(self) -> None:
        """An unrelated object does not satisfy the protocol."""
        assert not isinstance(object(), Stage)


class TestStepResultTypes:
    """Step-result value objects are frozen and carry their routing data."""

    def test_continue_carries_next_state(self) -> None:
        """Continue names the next in-memory state."""
        assert Continue(next_state="VERIFY").next_state == "VERIFY"

    def test_stage_outcome_is_the_routing_type(self) -> None:
        """The re-exported StageOutcome is routing.StageOutcome itself."""
        from hephaestus.automation.pipeline.stages import StageOutcome as ReExported

        assert ReExported is StageOutcome
        outcome = ReExported(Disposition.ADVANCE, "done")
        assert outcome.disposition == Disposition.ADVANCE

    def test_job_request_carries_on_done_state(self) -> None:
        """JobRequest names the state entered after on_job_done."""
        request = JobRequest(job=None, on_done_state="EVAL")  # type: ignore[arg-type]
        assert request.on_done_state == "EVAL"


class TestAgentProvider:
    """The provider helper falls back to the shared agent default."""

    def _ctx(self, agent: str | None = None) -> StageContext:
        """Build a minimal stage context for provider selection tests."""
        return TestStageContext()._bare_ctx(config=SimpleNamespace(agent=agent))

    def test_defaults_to_shared_default_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Blank config values use the shared backend default, not a literal."""
        monkeypatch.setattr(stage_base, "DEFAULT_AGENT", "codex", raising=False)
        assert agent_provider(self._ctx(agent="")) == "codex"

    def test_prefers_explicit_agent(self) -> None:
        """Configured agent values are returned unchanged."""
        assert agent_provider(self._ctx(agent="pi")) == "pi"
