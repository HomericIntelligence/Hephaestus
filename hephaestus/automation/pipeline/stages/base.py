"""Base protocol and step-result types for pipeline stages.

This module defines the :class:`Stage` protocol that all pipeline stages
implement, the step-result types (:class:`Continue` / :class:`JobRequest` /
re-exported :class:`StageOutcome`), and :class:`StageContext`, the bundle of
coordinator-owned accessors injected into every stage call.

Core types come from their source modules (epic #1809):
``StageOutcome``/``Disposition``/``StageName`` from :mod:`..routing`,
``WorkItem``/``ItemKind`` from :mod:`..work_item`, and
``AgentJob``/``JobResult``/``JobHandle`` from :mod:`..jobs`. They are
re-exported here so stage modules and their tests have a single import
surface for the stage contract.

Coordinator convention (binding for #1817, the coordinator slice):

- ``on_enter`` runs once when an item enters the stage. It must be
  idempotent, and its label checks are ordered at-or-past checks (never
  equality), so re-entry after a restart fast-forwards instead of redoing
  work. It returns ``None`` to proceed or a ``StageOutcome`` to route away.
- ``step`` is invoked for the item's *current* ``state``. Returning
  ``Continue`` advances ``item.state`` and steps again; returning
  ``JobRequest`` submits the job while ``item.state`` stays at the
  submitting WAIT state; returning ``StageOutcome`` routes via ROUTES.
- When a requested job completes and was NOT interrupted, the coordinator
  calls ``on_job_done`` (``item.state`` still the WAIT state that submitted
  the job), then sets ``item.state = on_done_state`` and steps again.
  ``on_job_done`` is never called for interrupted results — interrupts
  leave items resumable, never failed.
- All durable GitHub mutations go through ``ctx.github`` and happen
  immediately BEFORE the outcome that causes a queue push ("durable write
  precedes the queue push").
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias, runtime_checkable

from ..jobs import AgentJob, JobHandle, JobResult
from ..routing import Disposition, StageName, StageOutcome
from ..work_item import ItemKind, WorkItem

__all__ = [
    "AgentJob",
    "Continue",
    "Disposition",
    "ItemKind",
    "JobHandle",
    "JobRequest",
    "JobResult",
    "Stage",
    "StageContext",
    "StageName",
    "StageOutcome",
    "StepResult",
    "WorkItem",
]


@dataclass(frozen=True)
class Continue:
    """Advance to the next state without requesting a job."""

    next_state: str


@dataclass(frozen=True)
class JobRequest:
    """Request a job be submitted to the worker pool.

    Attributes:
        job: The frozen job spec to submit.
        on_done_state: The state the coordinator moves the item to after the
            job completes and ``on_job_done`` has run.

    """

    job: AgentJob
    on_done_state: str


StepResult: TypeAlias = "Continue | JobRequest | StageOutcome"


@dataclass(frozen=True)
class StageContext:
    """Context passed to every stage call.

    All coordinator-owned accessors (github, paths, clock, budgets) are
    injected here so stages never construct their own I/O helpers. The
    ``github`` accessor is the coordinator's single mutation channel: its
    mutator surface uses coordinator-neutral names (``add_labels``,
    ``remove_labels``, ``close_issue_as_covered``) that the coordinator
    (#1817) maps onto the ``github_api`` mutators, while its read surface
    mirrors the existing helper names (``gh_issue_json``,
    ``find_merged_closing_pr``, ``find_pr_for_issue``,
    ``has_existing_plan``). Stages never import ``github_api`` directly —
    enforced by ``tests/unit/automation/pipeline/test_pipeline_architecture``.
    """

    config: Any  # PlannerOptions-like (enable_advise, enable_learn, force, agent, dry_run)
    org: str
    dry_run: bool
    github: Any  # coordinator-owned GitHub accessor (label/comment/PR writes+reads)
    paths: Any  # coordinator-owned path accessor (repo_root, worktree)
    now_fn: Callable[[], float] | None = None  # injectable clock (tests pass a fake)
    budget_fn: Callable[[str], int] | None = None  # routing accessor: ROUTES budget lookup

    def now(self) -> float:
        """Return the current time in seconds since epoch (injectable for tests)."""
        if self.now_fn is not None:
            return self.now_fn()
        return time.time()

    def budget(self, name: str) -> int:
        """Look up the budget for a given counter name from the routing tables."""
        if self.budget_fn is not None:
            return self.budget_fn(name)
        return 1  # conservative default

    def option(self, name: str, default: Any = None) -> Any:
        """Read an optional config attribute with a default (POLA accessor)."""
        return getattr(self.config, name, default)


@runtime_checkable
class Stage(Protocol):
    """Protocol for pipeline stage implementations.

    A stage processes work items through a small in-memory state machine
    (states are stage-local strings, never GitHub labels):

    1. ``on_enter``: refresh item state, perform idempotent fast-forward
       checks (ordered at-or-past label checks, never equality), and ensure
       required entry labels durably. Return ``None`` to proceed or a
       ``StageOutcome`` to skip/finish.
    2. ``step``: take the next action for the current state (``Continue`` to
       advance state, ``JobRequest`` to submit work, or ``StageOutcome`` to
       route). Every durable mutation happens immediately before the return.
    3. ``on_job_done``: handle the result of a completed job (never called
       for interrupted results), storing parsed values on ``item.payload``.
    """

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Refresh labels and perform idempotent fast-forward checks on entry.

        Must be safe to call repeatedly (restart = re-run): label checks are
        ordered at-or-past checks, and any entry-label write is guarded by a
        presence check so re-entry produces no duplicate mutations.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            None to proceed with step(), or a StageOutcome to skip/finish.

        """
        ...

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Execute the next action for the item's current state.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            A Continue (advance state), JobRequest (submit work while this
            state waits), or StageOutcome (route via ROUTES). All durable
            mutations happen immediately before the return.

        """
        ...

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Handle completion of a job (never called for interrupted results).

        Called with ``item.state`` still at the WAIT state that submitted the
        job. Store parsed results on ``item.payload``; the coordinator then
        advances ``item.state`` to the JobRequest's ``on_done_state``.

        Args:
            item: The work item being processed.
            result: The job result from the worker pool.
            ctx: The stage context.

        """
        ...
