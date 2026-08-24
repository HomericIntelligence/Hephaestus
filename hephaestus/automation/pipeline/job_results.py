"""Provider-neutral pipeline job completion types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .routing import StageName


@dataclass(frozen=True)
class JobResult:
    """Result of a completed pipeline job."""

    ok: bool
    value: Any = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str | None = None
    interrupted: bool = False
    duration_s: float = 0.0
    worker_id: str = ""
    session_id: str | None = None
    session_binding: Any = None
    session_lost: bool = False
    observed_skill_invocations: tuple[str, ...] = ()


@dataclass(frozen=True, eq=False)
class JobHandle:
    """Identity-based handle that correlates a job with its completion."""

    job: Any
    on_done_state: str | StageName
