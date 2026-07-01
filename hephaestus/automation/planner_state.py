"""Backward-compatibility shim. Canonical impl: hephaestus.automation.state.planner."""

from hephaestus.automation.state.planner import (
    PlannerStateManager as PlannerStateManager,
    _comments_contain_plan as _comments_contain_plan,
)

__all__ = ["PlannerStateManager"]
