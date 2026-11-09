"""Pipeline stage implementations.

Stages are pure-ish step functions that process work items through a
stage-local state machine. Each stage owns one package under ``stages/``
(``stages/<name>/<name>.py``); the flat ``stages/<name>.py`` modules re-export
the package names for historical import paths. The base protocol and step-result
types live in :mod:`..stage_base` and the GitHub seam in :mod:`..stage_github`.
"""

from ..stage_base import Continue, JobRequest, Stage, StageContext, StageOutcome, StepResult
from ..stage_github import AutoMergeEnableResult, StageGitHub
from .finished.finished import FinishedStage
from .implementation.implementation import ImplementationStage
from .learning.learning import LearningStage
from .merge_wait.merge_wait import MergeWaitStage
from .plan_review.plan_review import PlanReviewStage
from .planning.planning import PlanningStage
from .pr_review.pr_review import PrReviewStage
from .repo.repo import RepoStage

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
