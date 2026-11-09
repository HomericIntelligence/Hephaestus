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
from .direct_scope_keys import (
    DIRECT_SCOPE_BASE_SHA_KEY,
    DIRECT_SCOPE_LOCAL_BRANCH_CLEANUP_KEY,
    DIRECT_SCOPE_RESERVATION_COLLISION_KEY,
    DIRECT_SCOPE_RESERVATION_KEY,
    DIRECT_SCOPE_STALE_STATE_KEY,
    DIRECT_SCOPE_WORKTREE_NONCE_KEY,
    is_direct_scope_worktree_nonce,
    is_full_commit_sha,
)
from .durable_request import PENDING_GITHUB_REQUEST, pending_request
from ..host_verification_diagnostic import publish_host_verification_failure


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
