"""Payload keys and guards for a direct CLI scope.

A direct CLI run bootstraps one reusable checkout before any source read. The
keys below travel only with that bootstrap's cursors. They are stage-neutral
state: the repo stage writes them, and the implementation, finished, and
coordinator modules read them. They live here, not in a stage module, so no
stage needs to import another stage for a shared key.

``is_full_commit_sha`` is not re-homed here. The authoritative function is
:func:`hephaestus.automation.issue_waves.is_full_commit_sha`; this module
re-exports that exact object so a caller can import every direct-scope name
from one place without a second implementation.
"""

from __future__ import annotations

from typing import TypeGuard

from hephaestus.automation.issue_waves import is_full_commit_sha as is_full_commit_sha

# Direct CLI scopes bootstrap one reusable checkout before any source reads.
# The SHA travels only with that bootstrap's cursors; normal repository
# discovery retains per-issue fresh-trunk semantics.
DIRECT_SCOPE_BOOTSTRAP_KEY = "_direct_scope_bootstrap"
DIRECT_SCOPE_BASE_SHA_KEY = "_direct_scope_base_sha"
# The coordinator creates one UUID4 hex value per explicit issue cursor. A
# fresh direct run carries it through to its writer worktree so a preserved
# failed checkout cannot block a later run for the same issue.
DIRECT_SCOPE_WORKTREE_NONCE_KEY = "_direct_scope_worktree_nonce"
# A direct-scope worker returns this receipt only after it has atomically
# reserved the remote implementation branch. It remains on the item until a
# coordinator-owned push publishes a commit or the Finished stage releases the
# still-unused reservation.
DIRECT_SCOPE_RESERVATION_KEY = "_direct_scope_reservation"
# Typed result retained only between the direct worktree reservation worker
# and implementation's next step. A confirmed remote collision is terminal
# rather than a generic infrastructure retry: retrying cannot make a branch
# owned by another run safe to overwrite.
DIRECT_SCOPE_RESERVATION_COLLISION_KEY = "_direct_scope_reservation_collision"
# Typed result retained only between the direct worktree worker and
# implementation's next step. Deterministic stale local state fails on every
# attempt, so it stops the item instead of spending the Git retry budget.
DIRECT_SCOPE_STALE_STATE_KEY = "_direct_scope_stale_state"
# The remote reservation is already released after a direct no-op. Finished
# uses this receipt to compare-and-delete the now-detached local branch only
# after removing its worktree.
DIRECT_SCOPE_LOCAL_BRANCH_CLEANUP_KEY = "_direct_scope_local_branch_cleanup"


def is_direct_scope_worktree_nonce(value: object) -> TypeGuard[str]:
    """Return whether ``value`` is the coordinator's UUID4 hex token."""
    return bool(
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "DIRECT_SCOPE_BASE_SHA_KEY",
    "DIRECT_SCOPE_BOOTSTRAP_KEY",
    "DIRECT_SCOPE_LOCAL_BRANCH_CLEANUP_KEY",
    "DIRECT_SCOPE_RESERVATION_COLLISION_KEY",
    "DIRECT_SCOPE_RESERVATION_KEY",
    "DIRECT_SCOPE_STALE_STATE_KEY",
    "DIRECT_SCOPE_WORKTREE_NONCE_KEY",
    "is_direct_scope_worktree_nonce",
    "is_full_commit_sha",
]
