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
    session_agent: str = ""
    prompt_kwargs: dict[str, Any] = field(default_factory=dict)
    output_format: str = "text"
    parse: Callable[[str], Any] | None = None  # e.g. claude_invoke.parse_review_verdict
    descr: str = ""


@dataclass(frozen=True)
class BuildTestJob:
    """Job to run build/test commands.

    Security: ``argv`` MUST NOT carry untrusted (issue-body-derived) strings.
    It is executed directly as a subprocess argument vector, so only the
    coordinator may construct these jobs, from vetted command templates.
    """

    repo: str
    cwd: Path
    argv: tuple[str, ...]  # e.g. ("pixi","run","pytest","tests/unit","-q")
    timeout_s: int
    descr: str = ""

    def __post_init__(self) -> None:
        """Normalize argv to a tuple so the job is deeply immutable/hashable."""
        if not isinstance(self.argv, tuple):
            # frozen dataclass: bypass the frozen __setattr__ for normalization
            object.__setattr__(self, "argv", tuple(self.argv))


@dataclass(frozen=True)
class GitJob:
    """Job to perform a git operation.

    Security: ``kwargs`` values are forwarded to git/worktree helpers that
    shell out, so they MUST NOT carry untrusted (issue-body-derived) strings.
    Only the coordinator may construct these jobs, from vetted values.
    """

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


@dataclass(frozen=True, eq=False)
class JobHandle:
    """Handle to a submitted job, used to correlate its completion.

    Identity semantics (``eq=False``): each ``submit()`` mints a distinct
    handle that hashes and compares by object identity, NOT by field value.
    Two submissions of identical job specs therefore produce two distinct
    handles, so the coordinator can key dicts/sets by handle without
    collisions, and unhashable field values (``dict`` kwargs, callables)
    never break hashing.
    """

    job: AgentJob | BuildTestJob | GitJob
    on_done_state: StageName
