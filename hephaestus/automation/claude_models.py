"""Backward-compatibility shim. Canonical impl: agent_config (#1441)."""

from hephaestus.automation.agent_config import (
    advise_model as advise_model,
    implementer_model as implementer_model,
    learn_model as learn_model,
    normalize_claude_model as normalize_claude_model,
    planner_model as planner_model,
    reviewer_model as reviewer_model,
)

__all__ = [
    "advise_model",
    "implementer_model",
    "learn_model",
    "normalize_claude_model",
    "planner_model",
    "reviewer_model",
]
