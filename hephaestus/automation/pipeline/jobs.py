"""Frozen job specs and results for the pipeline worker pool.

Jobs are immutable value objects the coordinator freezes and hands to
:class:`~hephaestus.automation.pipeline.worker_pool.WorkerPool`. Prompts are
built IN the worker (several builders fetch diffs / issue bodies via ``gh`` and
must stay off the coordinator thread), so :class:`AgentJob` carries a
``prompt_builder`` callable rather than a pre-rendered string.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hephaestus.automation.pipeline.routing import StageName

GIT_OPS: frozenset[str] = frozenset(
    {"clone", "create_worktree", "remove_worktree", "rebase", "push", "commit_push"}
)


@dataclass(frozen=True)
class AgentJob:
    """Job to invoke an agent (Claude or other)."""

    repo: str
    issue: int | str
    agent: str
    model: str
    prompt_builder: Callable[..., str]
    cwd: Path
    timeout_s: int
    prompt_kwargs: dict[str, Any] = field(default_factory=dict)
    output_format: str = "text"
    parse: Callable[[str], Any] | None = None  # e.g. claude_invoke.parse_review_verdict
    descr: str = ""


@dataclass(frozen=True)
class BuildTestJob:
    """Job to run build/test commands."""

    repo: str
    cwd: Path
    argv: tuple[str, ...] | list[str]  # e.g. ("pixi","run","pytest","tests/unit","-q")
    timeout_s: int
    descr: str = ""


@dataclass(frozen=True)
class GitJob:
    """Job to perform a git operation."""

    repo: str
    op: str
    timeout_s: int
    kwargs: dict[str, Any] = field(default_factory=dict)
    descr: str = ""

    def __post_init__(self) -> None:
        """Validate that op is a recognized git operation."""
        if self.op not in GIT_OPS:
            raise ValueError(f"unknown git op {self.op!r}; expected one of {sorted(GIT_OPS)}")


@dataclass(frozen=True)
class JobResult:
    """Result of a completed job."""

    ok: bool
    value: Any = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str | None = None
    interrupted: bool = False
    duration_s: float = 0.0


@dataclass(frozen=True)
class JobHandle:
    """Handle to a submitted job paired with its completion result."""

    job: AgentJob | BuildTestJob | GitJob
    on_done_state: StageName
