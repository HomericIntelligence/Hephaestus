"""Pipeline stage implementations.

Stages are pure-ish step functions that process work items through a
stage-local state machine. The base protocol and step-result types live in
:mod:`.base`; concrete stages (planning, plan_review, ...) follow.
"""

from .base import (
    Continue,
    JobRequest,
    Stage,
    StageContext,
    StageGitHub,
    StageOutcome,
    StepResult,
)
from .plan_review import PlanReviewStage
from .planning import PlanningStage

__all__ = [
    "Continue",
    "JobRequest",
    "PlanReviewStage",
    "PlanningStage",
    "Stage",
    "StageContext",
    "StageGitHub",
    "StageOutcome",
    "StepResult",
]
