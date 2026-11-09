"""Flat re-export for the ``finished`` stage package.

The stage lives in :mod:`hephaestus.automation.pipeline.stages.finished` (issue
#3521). This module keeps the historical flat import path working for callers
and tests. It has no behaviour of its own.
"""

from __future__ import annotations

from .finished.finished import (
    FinishedStage,
)

__all__ = [
    "FinishedStage",
]
