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
    StrictReviewArtifact,
    StrictReviewEvidence,
    StrictReviewLease,
)
from .ci import CiStage
from .finished import FinishedStage
from .implementation import ImplementationStage
from .merge_wait import MergeWaitStage
from .plan_review import PlanReviewStage
from .planning import PlanningStage
from .pr_review import PrReviewStage
from .repo import RepoStage
from .strict_review import StrictReviewStage

__all__ = [
    "CiStage",
    "Continue",
    "FinishedStage",
    "ImplementationStage",
    "JobRequest",
    "MergeWaitStage",
    "PlanReviewStage",
    "PlanningStage",
    "PrReviewStage",
    "RepoStage",
    "Stage",
    "StageContext",
    "StageGitHub",
    "StageOutcome",
    "StepResult",
    "StrictReviewArtifact",
    "StrictReviewEvidence",
    "StrictReviewLease",
    "StrictReviewStage",
]
