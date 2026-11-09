"""Flat re-export for the ``implementation`` stage package.

The stage lives in :mod:`hephaestus.automation.pipeline.stages.implementation` (issue
#3521). This module keeps the historical flat import path working for callers
and tests. It has no behaviour of its own.
"""

from __future__ import annotations

from .implementation.implementation import (
    BRANCH_WORKTREE_OWNER_PENDING_DELAY_S,
    DIRTY_RECOVERY_WAIT,
    GIT_ERROR_RETRY_CAP,
    HEPHAESTUS_REQUIRED_CHECK_ARGV,
    PRE_PR_TEST_ARGV,
    ImplementationStage,
    build_implementation_prompt,
    build_test_fix_prompt,
)

__all__ = [
    "BRANCH_WORKTREE_OWNER_PENDING_DELAY_S",
    "DIRTY_RECOVERY_WAIT",
    "GIT_ERROR_RETRY_CAP",
    "HEPHAESTUS_REQUIRED_CHECK_ARGV",
    "PRE_PR_TEST_ARGV",
    "ImplementationStage",
    "build_implementation_prompt",
    "build_test_fix_prompt",
]
