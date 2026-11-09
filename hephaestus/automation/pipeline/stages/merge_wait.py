"""Flat re-export for the ``merge_wait`` stage package.

The stage lives in :mod:`hephaestus.automation.pipeline.stages.merge_wait` (issue
#3521). This module keeps the historical flat import path working for callers
and tests. It has no behaviour of its own.
"""

from __future__ import annotations

from .merge_wait.merge_wait import (
    MW_FINISH,
    MergeWaitStage,
)

__all__ = [
    "MW_FINISH",
    "MergeWaitStage",
]
