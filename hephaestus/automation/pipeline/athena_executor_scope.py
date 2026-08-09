"""Athena skill executor admission helpers for pipeline scopes."""

from __future__ import annotations

from typing import Any

from hephaestus.automation.pipeline.routing import ROUTES, StageName


def pipeline_requires_athena_executor(config: Any) -> bool:
    """Return whether the selected scope can submit Athena skill jobs."""
    if config.dry_run:
        return False
    stages = config.scope.stages if config.scope is not None else frozenset(ROUTES)
    return (
        not config.no_advise
        and bool(stages.intersection({StageName.PLANNING, StageName.IMPLEMENTATION}))
    ) or (
        config.enable_learn
        and bool(stages.intersection({StageName.PLAN_REVIEW, StageName.MERGE_WAIT}))
    )
