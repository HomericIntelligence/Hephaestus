"""Flat re-export for the ``planning`` stage package.

The stage lives in :mod:`hephaestus.automation.pipeline.stages.planning` (issue
#3521). This module keeps the historical flat import path working for callers
and tests. It has no behaviour of its own.
"""

from __future__ import annotations

from .planning.planning import (
    PlanningStage,
    build_plan_prompt,
)
from ..host_verification_diagnostic import publish_stage_diagnostic

__all__ = [
    "PlanningStage",
    "build_plan_prompt",
    "publish_stage_diagnostic",
]
