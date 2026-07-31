"""Pipeline end-of-run / interrupt summary (epic #1809, coordinator slice #1817).

Printed from the coordinator's ``finally`` — on completion AND interrupt:
per-item rows (repo, issue, PR, entry queue, final stage,
PASS/FAIL:reason/SKIP/BLOCKED/RESUMABLE, attempt counters, elapsed),
aggregates (per-disposition counts, per-stage throughput, agent-job
count/time, wall clock, loops), preserved worktrees (the exact legacy
implementer preserved-worktree line sequence, re-housed here as
:func:`format_preserved_worktrees`), and the ``emit_json_status``
envelope extension when ``--json`` is active.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TypeAlias, TypeVar

from hephaestus.automation.pipeline.work_item import (
    ItemKind,
    PreservedWorktree,
    WorkItem,
    WorkItemSummary,
)
from hephaestus.cli.utils import emit_json_status

logger = logging.getLogger(__name__)

SummaryItem: TypeAlias = WorkItem | WorkItemSummary
_SummaryItemT = TypeVar("_SummaryItemT", bound=SummaryItem)


@dataclass(frozen=True)
class RunStats:
    """Aggregate run statistics the coordinator hands to :func:`print_summary`."""

    exit_code: int
    loops_run: int
    agent_job_count: int
    agent_job_time_s: float
    wall_s: float
    terminal_item_count: int = 0
    terminal_dispositions: dict[str, int] = field(default_factory=dict)
    terminal_per_stage: dict[str, int] = field(default_factory=dict)

    @property
    def interrupted(self) -> bool:
        """Return whether the run ended with the interrupt exit code."""
        return self.exit_code == 130


def format_preserved_worktrees(preserved: Sequence[PreservedWorktree], script: str) -> list[str]:
    """Format the preserved-worktree footer (legacy line sequence, verbatim).

    Re-housed from the legacy implementer preserved-worktree footer so the
    pipeline prints byte-identical guidance; the legacy printer was removed
    with the pipeline conversion (#1821).

    Args:
        preserved: ``(repo, issue_number, worktree_path)`` tuples for failed items.
        script: The script name (``sys.argv[0]``) for the rerun hint.

    Returns:
        The formatted lines (empty when nothing is preserved).

    """
    if not preserved:
        return []
    issue_nums = [number for _, number, _ in preserved]
    # ``--issues`` takes ONE comma-separated string (loop_runner._parse_issue_list);
    # a space-joined list makes argparse read only the first number and reject the
    # rest, and ``--resume`` is not an option on this CLI at all (#2281). The loop
    # resumes a preserved worktree by re-seeding the same ``--issues``.
    issues_arg = ",".join(str(n) for n in issue_nums)
    lines: list[str] = ["\nPreserved worktrees (contain uncommitted changes):"]
    lines.extend(f"  #{number}: {path}" for _, number, path in preserved)
    lines.append("\nRerun these issues after inspecting/cleaning the worktrees:")
    lines.append(f"  {script} --issues {issues_arg}")
    lines.append("To discard them instead:")
    lines.extend(f"  git worktree remove --force {path}" for _, _, path in preserved)
    return lines


def _logical_item_key(item: SummaryItem) -> tuple[object, ...]:
    """Return the stable logical identity for a potentially re-seeded item."""
    if item.kind is ItemKind.REPO:
        return (item.repo, "repo")
    if item.issue is not None:
        return (item.repo, "issue", item.issue)
    if item.pr is not None:
        return (item.repo, "pr", item.pr)
    return (item.repo, item.kind.value, id(item))


def latest_logical_items(items: Sequence[_SummaryItemT]) -> list[_SummaryItemT]:
    """Return only the latest queued item for each logical issue/PR/repo."""
    latest: dict[tuple[object, ...], _SummaryItemT] = {}
    for item in items:
        key = _logical_item_key(item)
        latest.pop(key, None)
        latest[key] = item
    return list(latest.values())


def _disposition(item: SummaryItem) -> str:
    """Classify one item's summary disposition cell."""
    result = item.result
    if result is None:
        return "PENDING"
    if result.reason.startswith("resumable"):
        return f"RESUMABLE at {result.final_stage.value}"
    if result.passed:
        return "PASS"
    if result.reason.startswith("skip"):
        return "SKIP"
    if result.reason.startswith("blocked"):
        return "BLOCKED"
    return f"FAIL:{result.reason}"


def _disposition_bucket(item: SummaryItem) -> str:
    """Aggregate-count bucket for one item (pass/fail/skip/blocked/resumable)."""
    cell = _disposition(item)
    return cell.split(":")[0].split(" ")[0].lower()


def _json_message(exit_code: int) -> str:
    """Map a pipeline exit code to its JSON summary message."""
    if exit_code == 130:
        return "pipeline interrupted"
    if exit_code == 0:
        return "pipeline complete"
    return "pipeline failed"


def _item_row(item: SummaryItem) -> str:
    """Format one per-item summary row."""
    issue = f"#{item.issue}" if item.issue else "-"
    pr = f"!{item.pr}" if item.pr else "-"
    if isinstance(item, WorkItemSummary):
        entry = item.entry_stage
        attempt_items = item.attempts
    else:
        entry = str(item.payload.get("entry_stage", item.stage.value))
        attempt_items = tuple(sorted((key, value) for key, value in item.attempts.items() if value))
    attempts = ",".join(f"{key}={value}" for key, value in attempt_items) or "-"
    elapsed_s = (item.updated_at - item.created_at).total_seconds()
    return (
        f"  {item.repo:<28} {issue:>7} {pr:>7} {entry:<15} "
        f"{item.stage.value:<15} {_disposition(item):<28} {attempts:<24} {elapsed_s:7.1f}s"
    )


def print_summary(
    items: Sequence[SummaryItem],
    stats: RunStats,
    preserved: list[PreservedWorktree],
    *,
    json_out: bool,
) -> None:
    """Log the end-of-run summary; emit the JSON envelope when requested.

    Args:
        items: Effective active items and compact terminal summaries.
        stats: Aggregate run statistics (exit code, loops, agent time, wall).
        preserved: ``(repo, issue_number, worktree_path)`` tuples for failed items.
        json_out: Emit the machine-readable ``emit_json_status`` envelope.

    """
    items = latest_logical_items(items)

    logger.info("")
    logger.info("=== Pipeline summary ===")
    header = (
        f"  {'repo':<28} {'issue':>7} {'pr':>7} {'entry':<15} "
        f"{'final':<15} {'disposition':<28} {'attempts':<24} {'elapsed':>8}"
    )
    logger.info("%s", header)
    logger.info("  %s", "-" * (len(header) - 2))
    for item in items:
        logger.info("%s", _item_row(item))

    if stats.terminal_item_count or stats.terminal_dispositions or stats.terminal_per_stage:
        dispositions = dict(stats.terminal_dispositions)
        per_stage = dict(stats.terminal_per_stage)
        aggregate_items = stats.terminal_item_count
        # Terminal WorkItemSummary rows are a bounded recent sample already
        # represented in the terminal aggregates. Active WorkItems still need
        # to contribute to the end-of-run counts.
        for item in items:
            if isinstance(item, WorkItemSummary):
                continue
            dispositions[_disposition_bucket(item)] = (
                dispositions.get(_disposition_bucket(item), 0) + 1
            )
            per_stage[item.stage.value] = per_stage.get(item.stage.value, 0) + 1
            aggregate_items += 1
    else:
        dispositions = {}
        per_stage = {}
        for item in items:
            dispositions[_disposition_bucket(item)] = (
                dispositions.get(_disposition_bucket(item), 0) + 1
            )
            per_stage[item.stage.value] = per_stage.get(item.stage.value, 0) + 1
        aggregate_items = len(items)

    logger.info("")
    logger.info("=== Aggregates ===")
    logger.info(
        "  items: %d  dispositions: %s",
        aggregate_items,
        dict(sorted(dispositions.items())),
    )
    logger.info("  per-stage: %s", dict(sorted(per_stage.items())))
    logger.info(
        "  agent jobs: %d (%.1fs total)  loops: %d  wall: %.1fs  interrupted: %s",
        stats.agent_job_count,
        stats.agent_job_time_s,
        stats.loops_run,
        stats.wall_s,
        stats.interrupted,
    )

    for line in format_preserved_worktrees(preserved, sys.argv[0]):
        logger.info("%s", line)

    if json_out:
        resumable = [
            f"{item.repo}#{item.issue or item.pr or ''}@{item.stage.value}"
            for item in items
            if item.result is not None and item.result.reason.startswith("resumable")
        ]
        emit_json_status(
            stats.exit_code,
            message=_json_message(stats.exit_code),
            dispositions=dict(sorted(dispositions.items())),
            loops_run=stats.loops_run,
            agent_jobs=stats.agent_job_count,
            agent_job_time_s=round(stats.agent_job_time_s, 1),
            wall_s=round(stats.wall_s, 1),
            resumable=resumable,
            preserved_worktrees=[[number, path] for _, number, path in preserved],
        )
