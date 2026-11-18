"""Adaptive planner timeout ladder (#3627)."""

from __future__ import annotations

from hephaestus.automation import agent_config
from hephaestus.constants import AGENT_LEARN_TIMEOUT, AGENT_PLAN_TIMEOUT


def test_defaults_raised_to_1800() -> None:
    """Planner and learner defaults are 1800 s, plan path only."""
    assert AGENT_PLAN_TIMEOUT == 1800
    assert AGENT_LEARN_TIMEOUT == 1800
    assert agent_config.planner_claude_timeout() == 1800


def test_adaptive_ladder() -> None:
    """Each prior attempt raises the budget by 50 % of the default."""
    assert agent_config.adaptive_planner_claude_timeout(0) == 1800
    assert agent_config.adaptive_planner_claude_timeout(1) == 2700
    assert agent_config.adaptive_planner_claude_timeout(2) == 3600
    assert agent_config.adaptive_planner_claude_timeout(7) == 3600


def test_adaptive_guards() -> None:
    """Negative or non-int input degrades to the default."""
    assert agent_config.adaptive_planner_claude_timeout(-1) == 1800
    assert agent_config.adaptive_planner_claude_timeout(1.5) == 1800  # type: ignore[arg-type]
