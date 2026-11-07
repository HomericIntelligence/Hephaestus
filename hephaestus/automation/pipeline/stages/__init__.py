"""Pipeline stage implementations.

Stages are pure-ish step functions that process work items through a
stage-local state machine. The base protocol and step-result types live in
:mod:`..stage_base` and the GitHub seam in :mod:`..stage_github`; concrete
stages (planning, plan_review, ...) follow.
"""

from ..stage_base import Continue, JobRequest, Stage, StageContext, StageOutcome, StepResult
from ..stage_github import AutoMergeEnableResult, StageGitHub
from .finished import FinishedStage
from .implementation import ImplementationStage
from .learning import LearningStage
from .merge_wait import MergeWaitStage
from .plan_review import PlanReviewStage
from .planning import PlanningStage
from .pr_review import PrReviewStage
from .repo import RepoStage

__all__ = [
    "AutoMergeEnableResult",
    "Continue",
    "FinishedStage",
    "ImplementationStage",
    "JobRequest",
    "LearningStage",
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
]
