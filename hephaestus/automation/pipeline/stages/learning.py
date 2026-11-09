"""Flat re-export for the ``learning`` stage package.

The stage lives in :mod:`hephaestus.automation.pipeline.stages.learning` (issue
#3521). This module keeps the historical flat import path working for callers
and tests. It has no behaviour of its own.
"""

from __future__ import annotations

from .learning.learning import (
    CLAIM,
    LearningStage,
)

__all__ = [
    "CLAIM",
    "LearningStage",
]
