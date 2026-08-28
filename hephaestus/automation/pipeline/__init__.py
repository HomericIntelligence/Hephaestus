"""Pipeline foundation layer: work items, stage queues, routing table.

Pure data and pure functions with ZERO I/O — no gh, no claude, no
subprocess, no imports of github_api/claude_invoke. Part of epic #1809.

Thread-safety: a WorkItem and its StageQueue are only ever touched by the
coordinator thread. The bounded main and auxiliary completion queues are the
only cross-thread payload channels.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .athena_skill_jobs import AthenaSkillJob, AthenaSkillRequest, AthenaSkillResult
    from .auxiliary_worker_pool import AuxiliaryWorkerPool
    from .jobs import GIT_OPS, AgentJob, BuildTestJob, CompactJob, GitJob, JobHandle, JobResult
    from .queues import CompletionQueue, StageQueue
    from .routing import (
        ROUTES,
        Disposition,
        PipelineScope,
        Route,
        StageName,
        StageOutcome,
    )
    from .coordinator import PipelineConfig, run_pipeline
    from .work_item import HistoryEvent, ItemKind, ItemResult, LearningIntent, WorkItem
    from .worker_pool import WorkerPool

__all__ = [
    "GIT_OPS",
    "ROUTES",
    "AgentJob",
    "AthenaSkillJob",
    "AthenaSkillRequest",
    "AthenaSkillResult",
    "AuxiliaryWorkerPool",
    "BuildTestJob",
    "CompactJob",
    "CompletionQueue",
    "Disposition",
    "GitJob",
    "HistoryEvent",
    "ItemKind",
    "ItemResult",
    "JobHandle",
    "JobResult",
    "LearningIntent",
    "PipelineConfig",
    "PipelineScope",
    "Route",
    "StageName",
    "StageOutcome",
    "StageQueue",
    "WorkItem",
    "WorkerPool",
    "run_pipeline",
]

_LAZY_EXPORTS: dict[str, str] = {
    "AgentJob": "hephaestus.automation.pipeline.jobs",
    "AthenaSkillJob": "hephaestus.automation.pipeline.athena_skill_jobs",
    "AthenaSkillRequest": "hephaestus.automation.pipeline.athena_skill_jobs",
    "AthenaSkillResult": "hephaestus.automation.pipeline.athena_skill_jobs",
    "AuxiliaryWorkerPool": "hephaestus.automation.pipeline.auxiliary_worker_pool",
    "BuildTestJob": "hephaestus.automation.pipeline.jobs",
    "CompactJob": "hephaestus.automation.pipeline.jobs",
    "CompletionQueue": "hephaestus.automation.pipeline.queues",
    "Disposition": "hephaestus.automation.pipeline.routing",
    "GIT_OPS": "hephaestus.automation.pipeline.jobs",
    "GitJob": "hephaestus.automation.pipeline.jobs",
    "HistoryEvent": "hephaestus.automation.pipeline.work_item",
    "ItemKind": "hephaestus.automation.pipeline.work_item",
    "ItemResult": "hephaestus.automation.pipeline.work_item",
    "LearningIntent": "hephaestus.automation.pipeline.work_item",
    "JobHandle": "hephaestus.automation.pipeline.jobs",
    "JobResult": "hephaestus.automation.pipeline.jobs",
    "PipelineConfig": "hephaestus.automation.pipeline.coordinator",
    "PipelineScope": "hephaestus.automation.pipeline.routing",
    "ROUTES": "hephaestus.automation.pipeline.routing",
    "Route": "hephaestus.automation.pipeline.routing",
    "StageName": "hephaestus.automation.pipeline.routing",
    "StageOutcome": "hephaestus.automation.pipeline.routing",
    "StageQueue": "hephaestus.automation.pipeline.queues",
    "WorkItem": "hephaestus.automation.pipeline.work_item",
    "WorkerPool": "hephaestus.automation.pipeline.worker_pool",
    "run_pipeline": "hephaestus.automation.pipeline.coordinator",
}


def __getattr__(name: str) -> Any:
    try:
        module_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
