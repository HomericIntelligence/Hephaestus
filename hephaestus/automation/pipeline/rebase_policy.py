"""Host-selected validation policy for a rebased worktree."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .job_results import JobResult

RebaseSemanticValidator = Callable[[Path], JobResult | None]


@dataclass(frozen=True)
class RebaseValidationPolicy:
    """Bind all validation inputs for one repository rebase policy."""

    name: str
    semantic_validator: RebaseSemanticValidator
    structural_test_argv: tuple[str, ...] | None


RebasePolicySelector = Callable[[str], RebaseValidationPolicy | None]

__all__ = [
    "RebasePolicySelector",
    "RebaseSemanticValidator",
    "RebaseValidationPolicy",
]
