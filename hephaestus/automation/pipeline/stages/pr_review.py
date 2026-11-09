"""Flat re-export for the ``pr_review`` stage package.

The stage lives in :mod:`hephaestus.automation.pipeline.stages.pr_review` (issue
#3521). This module keeps the historical flat import path working for callers
and tests. It has no behaviour of its own.
"""

from __future__ import annotations

from .pr_review.pr_review import (
    PrReviewStage,
)

__all__ = [
    "PrReviewStage",
]
