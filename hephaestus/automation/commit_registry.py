"""Dependency-inversion seam for the higher-level commit operation."""

from collections.abc import Callable
from typing import Any

_commit_changes: Callable[..., None] | None = None


def register_commit_changes(callback: Callable[..., None]) -> None:
    """Register the product-layer implementation used by ``git_utils``."""
    global _commit_changes
    _commit_changes = callback


def commit_changes(*args: Any, **kwargs: Any) -> None:
    """Dispatch a commit operation registered by ``pr_manager``."""
    if _commit_changes is None:
        raise RuntimeError("commit changes implementation is not registered")
    _commit_changes(*args, **kwargs)
