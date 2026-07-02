"""Tests for hephaestus.automation.mesh.roles.coordination."""

from __future__ import annotations

from typing import Any

from hephaestus.automation.mesh.config import MeshConfig
from hephaestus.automation.mesh.roles import ROLE_HANDLERS, resolve_handler
from hephaestus.automation.mesh.roles.chief_architect import ChiefArchitectHandler
from hephaestus.automation.mesh.roles.coordination import CoordinationHandler
from hephaestus.automation.mesh.worker import TaskContext

CFG = MeshConfig(domain="pipeline", role="component-lead", agent_id="c-1", exec_host="h")


def _ctx(payload: dict[str, Any]) -> TaskContext:
    ctx = TaskContext(
        config=CFG,
        payload=payload,
        task_id="t-1",
        team_id="mesh",
        attempt=1,
        publisher=None,  # type: ignore[arg-type]
        agamemnon=None,  # type: ignore[arg-type]
        deadline=float("inf"),
    )
    ctx.progress = lambda text: None  # type: ignore[method-assign, assignment]
    return ctx


class TestCoordinationHandler:
    """Tests for the L1/L2 coordination role."""

    def test_acknowledges_node(self) -> None:
        result = CoordinationHandler().handle(
            _ctx({"layer": "L1_ComponentLead", "subject": "epic [repo]"})
        )
        assert result.ok
        assert "coordination node acknowledged" in result.summary

    def test_registered_for_component_and_module_lead(self) -> None:
        assert ("pipeline", "component-lead") in ROLE_HANDLERS
        assert ("pipeline", "module-lead") in ROLE_HANDLERS
        assert isinstance(resolve_handler("pipeline", "component-lead"), CoordinationHandler)


class TestChiefArchitectCoordinationFallback:
    """Brief-tree L0 roots (no epic payload) are coordination nodes."""

    def test_brief_root_without_epic_is_acknowledged(self) -> None:
        handler = ChiefArchitectHandler(planner_factory=lambda issues: None)
        result = handler.handle(_ctx({"brief_id": "b-1", "layer": "L0_ChiefArchitect"}))
        assert result.ok
        assert "coordination node acknowledged" in result.summary

    def test_missing_epic_and_brief_is_still_bad_dispatch(self) -> None:
        handler = ChiefArchitectHandler(planner_factory=lambda issues: None)
        result = handler.handle(_ctx({}))
        assert not result.ok
        assert result.error_kind == "BadDispatch"
