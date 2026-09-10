"""Tests for the stage base contract (protocol, context, step-result types)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.pipeline import ROUTES
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
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
            "config": PipelineConfig(org="test-org", repos=["test-repo"], rate_guard_enabled=False),
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
        request = JobRequest(job=None, on_done_state="EVAL")
        assert request.on_done_state == "EVAL"


class TestAgentProvider:
    """The provider helper falls back to the shared agent default."""

    def _ctx(self, agent: str | None = None) -> StageContext:
        """Build a minimal stage context for provider selection tests."""
        config = PipelineConfig(
            org="test-org", repos=["test-repo"], agent=agent or "", rate_guard_enabled=False
        )
        return TestStageContext()._bare_ctx(config=config)

    def test_defaults_to_shared_default_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Blank config values use the shared backend default, not a literal."""
        monkeypatch.setattr(stage_base, "DEFAULT_AGENT", "codex", raising=False)
        assert agent_provider(self._ctx(agent="")) == "codex"

    def test_prefers_explicit_agent(self) -> None:
        """Configured agent values are returned unchanged."""
        assert agent_provider(self._ctx(agent="pi")) == "pi"


class TestPlanningSourceWorkspaceBinding:
    """Planning binds the detached captured source lane."""

    @pytest.mark.parametrize(
        ("synced_revision", "expected_revision"),
        [
            ("a" * 40, "a" * 40),
            (None, "b" * 40),
        ],
    )
    def test_uses_captured_revision_and_review_lane(
        self,
        synced_revision: str | None,
        expected_revision: str,
        make_ctx: Any,
        make_work_item: Any,
    ) -> None:
        """Planning ignores stale implementation and PR revisions."""
        calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        binding = object()

        class Manager:
            def prepare_bounded(self, *args: Any, **kwargs: Any) -> object:
                calls.append((args, kwargs))
                return binding

        payload: dict[str, Any] = {
            "_worktree_cleanup_head_sha": "c" * 40,
            "_impl_source_revision": "d" * 40,
            "reviewed_pr_head_sha": "e" * 40,
            "pr_head_sha": "f" * 40,
            "_direct_scope_base_sha": "b" * 40,
        }
        if synced_revision is not None:
            payload["_synced_default_branch_sha"] = synced_revision
        item = make_work_item(issue=12, state="ADVISE_WAIT", payload=payload)
        ctx = make_ctx(
            paths=SimpleNamespace(source_workspaces=Manager()),
            now_fn=lambda: 10.0,
        )
        helper = getattr(stage_base, "planning_source_workspace_binding", None)
        assert callable(helper)

        result = helper(item, ctx, preparation_timeout_s=5.0)

        assert result is binding
        assert calls[0][0] == (12, SourceLane.REVIEW, expected_revision)
        assert calls[0][1]["branch"] is None
        assert calls[0][1]["deadline"].expires_at == 15.0
