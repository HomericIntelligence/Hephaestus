"""Flat re-export for the ``repo`` stage package.

The stage lives in :mod:`hephaestus.automation.pipeline.stages.repo` (issue
#3521). This module keeps the historical flat import path working for callers
and tests. It has no behaviour of its own.
"""

from __future__ import annotations

from .repo.repo import (
    CHECKOUT_CONTENTION_ATTEMPTS_KEY,
    CHECKOUT_CONTENTION_ELAPSED_KEY,
    CHECKOUT_CONTENTION_FIRST_WAIT_KEY,
    CHECKOUT_CONTENTION_PENDING_KEY,
    CHECKOUT_CONTENTION_RESULT_KEY,
    CHECKOUT_CONTENTION_WAIT_KEY,
    SYNCED_MAIN_SHA_KEY,
    WAVE_ANCESTRY_VERIFIED_KEY,
    WAVE_PLAN_KEY,
    RepoIssueSource,
    RepoStage,
)

__all__ = [
    "CHECKOUT_CONTENTION_ATTEMPTS_KEY",
    "CHECKOUT_CONTENTION_ELAPSED_KEY",
    "CHECKOUT_CONTENTION_FIRST_WAIT_KEY",
    "CHECKOUT_CONTENTION_PENDING_KEY",
    "CHECKOUT_CONTENTION_RESULT_KEY",
    "CHECKOUT_CONTENTION_WAIT_KEY",
    "SYNCED_MAIN_SHA_KEY",
    "WAVE_ANCESTRY_VERIFIED_KEY",
    "WAVE_PLAN_KEY",
    "RepoIssueSource",
    "RepoStage",
]
