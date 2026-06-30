"""Backward-compatibility shim. Canonical impl: agent_config (#1441)."""

from hephaestus.automation.agent_config import (
    CODEX_ADVISE as CODEX_ADVISE,
    FABLE as FABLE,
    HAIKU as HAIKU,
    OPUS as OPUS,
    OPUS_48 as OPUS_48,
    SONNET as SONNET,
    advise_model as advise_model,
    codex_advise_model as codex_advise_model,
    git_message_model as git_message_model,
    implementer_model as implementer_model,
    learn_model as learn_model,
    planner_model as planner_model,
    reviewer_model as reviewer_model,
)

__all__ = [
    "CODEX_ADVISE",
    "FABLE",
    "HAIKU",
    "OPUS",
    "OPUS_48",
    "SONNET",
    "advise_model",
    "codex_advise_model",
    "git_message_model",
    "implementer_model",
    "learn_model",
    "planner_model",
    "reviewer_model",
]
