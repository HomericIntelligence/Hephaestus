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
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from hephaestus.automation.pipeline.work_item import ItemKind, PreservedWorktree, WorkItem
from hephaestus.cli.utils import emit_json_status

logger = logging.getLogger(__name__)

_SUMMARY_ACTIONS_KEY = "_planning_summary_actions"
_REVIEW_RUN_KEY = "_pr_review_run"
_EXPLICIT_REVIEW_RUN_REASON = "explicit-review"
_FULL_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


def record_review_run(item: WorkItem, *, reason: str, head_sha: str) -> None:
    """Record one bounded explicit-review completion reason on an item."""
    if reason != _EXPLICIT_REVIEW_RUN_REASON:
        raise ValueError("unsupported review run reason")
    if not isinstance(head_sha, str) or _FULL_SHA_PATTERN.fullmatch(head_sha) is None:
        raise ValueError("review run head must be a lowercase full commit SHA")
    item.payload[_REVIEW_RUN_KEY] = {"reason": reason, "head_sha": head_sha}


def _review_run(item: WorkItem) -> tuple[str, str] | None:
    """Return a valid bounded review-run record, if one exists."""
    raw = item.payload.get(_REVIEW_RUN_KEY)
    if not isinstance(raw, dict):
        return None
    reason = raw.get("reason")
    head_sha = raw.get("head_sha")
    if (
        reason != _EXPLICIT_REVIEW_RUN_REASON
        or not isinstance(head_sha, str)
        or _FULL_SHA_PATTERN.fullmatch(head_sha) is None
    ):
        return None
    return reason, head_sha


def _review_run_reasons(items: Sequence[WorkItem]) -> dict[str, int]:
    """Count valid review-run records in the retained item window."""
    reasons: Counter[str] = Counter()
    for item in items:
        review_run = _review_run(item)
        if review_run is not None:
            reasons[review_run[0]] += 1
    return dict(sorted(reasons.items()))


def record_summary_action(item: WorkItem, action: str) -> None:
    """Record one idempotent, bounded planning action on a work item."""
    normalized = action.strip()
    if not normalized:
        raise ValueError("summary action must not be empty")
    actions = item.payload.setdefault(_SUMMARY_ACTIONS_KEY, [])
    if not isinstance(actions, list):
        raise TypeError("planning summary actions payload must be a list")
    if normalized not in actions:
        actions.append(normalized)


def _summary_actions(item: WorkItem) -> tuple[str, ...]:
    """Return validated action names attached to one item."""
    raw = item.payload.get(_SUMMARY_ACTIONS_KEY, [])
    if not isinstance(raw, list):
        return ()
    return tuple(action for action in raw if isinstance(action, str) and action)


@dataclass(frozen=True)
class RunStats:
    """Aggregate run statistics the coordinator hands to :func:`print_summary`."""

    exit_code: int
    loops_run: int
    agent_job_count: int
    agent_job_time_s: float
    wall_s: float
    auxiliary_job_count: int = 0
    auxiliary_job_time_s: float = 0.0
    auxiliary_job_failure_count: int = 0
    auxiliary_job_deferred_count: int = 0

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
        preserved: ``(repo, issue_number, worktree_path)`` tuples retained for
            recovery or failed-item debugging.
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
    lines: list[str] = ["\nPreserved worktrees (retained for recovery or debugging):"]
    lines.extend(f"  #{number}: {path}" for _, number, path in preserved)
    lines.append("\nRerun these issues after inspecting/cleaning the worktrees:")
    lines.append(f"  {script} --issues {issues_arg}")
    lines.append("To discard them instead:")
    lines.extend(f"  git worktree remove --force {path}" for _, _, path in preserved)
    return lines


def format_direct_review_recovery_worktrees(
    recovery: Sequence[PreservedWorktree],
) -> list[str]:
    """Format inspection-only guidance for receipt-backed detached recoveries.

    Unlike ordinary failed-item worktrees, a recovery checkout may contain an
    unpublished detached commit. Never offer a destructive command for it:
    operators must first establish that no loop is still using the checkout.
    """
    if not recovery:
        return []
    lines = ["\nDetached-review recovery worktrees (inspection required):"]
    lines.extend(f"  #{number}: {path}" for _, number, path in recovery)
    lines.append(
        "Do not remove or reuse these paths until you have confirmed that no "
        "automation loop is active."
    )
    return lines


def _logical_item_key(item: WorkItem) -> tuple[object, ...]:
    """Return the stable logical identity for a potentially re-seeded item."""
    if item.kind is ItemKind.REPO:
        return (item.repo, "repo")
    if item.issue is not None:
        return (item.repo, "issue", item.issue)
    if item.pr is not None:
        return (item.repo, "pr", item.pr)
    return (item.repo, item.kind.value, id(item))


def latest_logical_items(items: Sequence[WorkItem]) -> list[WorkItem]:
    """Return only the latest queued item for each logical issue/PR/repo."""
    latest: dict[tuple[object, ...], WorkItem] = {}
    for item in items:
        key = _logical_item_key(item)
        latest.pop(key, None)
        latest[key] = item
    return list(latest.values())


def _disposition(item: WorkItem) -> str:
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


def _disposition_bucket(item: WorkItem) -> str:
    """Aggregate-count bucket for one item (pass/fail/skip/blocked/resumable)."""
    cell = _disposition(item)
    return cell.split(":")[0].split(" ")[0].lower()


@dataclass
class TerminalSummary:
    """Constant-space aggregates for terminal pipeline outcomes.

    The coordinator may retain only a recent bounded window of detailed
    :class:`WorkItem` objects during an all-organization run.  This accumulator
    preserves the run-wide counts needed for the final status and aggregate
    summary without becoming a second per-item ledger.
    """

    total: int = 0
    _dispositions: Counter[str] = field(default_factory=Counter)
    _per_stage: Counter[str] = field(default_factory=Counter)
    _planning_actions: Counter[str] = field(default_factory=Counter)
    _review_run_reasons: Counter[str] = field(default_factory=Counter)

    def record(self, item: WorkItem) -> None:
        """Add one terminal or resumable item outcome to the aggregate."""
        self.total += 1
        self._dispositions[_disposition_bucket(item)] += 1
        self._per_stage[item.stage.value] += 1
        self._planning_actions.update(_summary_actions(item))
        review_run = _review_run(item)
        if review_run is not None:
            self._review_run_reasons[review_run[0]] += 1

    def reset(self) -> None:
        """Start a fresh reseed-pass aggregate without retaining item identities."""
        self.total = 0
        self._dispositions.clear()
        self._per_stage.clear()
        self._planning_actions.clear()
        self._review_run_reasons.clear()

    @property
    def dispositions(self) -> dict[str, int]:
        """Return a stable copy of counts grouped by disposition."""
        return dict(sorted(self._dispositions.items()))

    @property
    def per_stage(self) -> dict[str, int]:
        """Return a stable copy of counts grouped by final stage."""
        return dict(sorted(self._per_stage.items()))

    @property
    def planning_actions(self) -> dict[str, int]:
        """Return counts of autonomous planning actions taken by the run."""
        return dict(sorted(self._planning_actions.items()))

    @property
    def review_run_reasons(self) -> dict[str, int]:
        """Return counts of bounded review-run completion reasons."""
        return dict(sorted(self._review_run_reasons.items()))


def _json_message(exit_code: int) -> str:
    """Map a pipeline exit code to its JSON summary message."""
    if exit_code == 130:
        return "pipeline interrupted"
    if exit_code == 0:
        return "pipeline complete"
    return "pipeline failed"


def _item_row(item: WorkItem) -> str:
    """Format one per-item summary row."""
    issue = f"#{item.issue}" if item.issue else "-"
    pr = f"!{item.pr}" if item.pr else "-"
    entry = str(item.payload.get("entry_stage", item.stage.value))
    attempts = ",".join(f"{k}={v}" for k, v in sorted(item.attempts.items()) if v) or "-"
    elapsed_s = (item.updated_at - item.created_at).total_seconds()
    return (
        f"  {item.repo:<28} {issue:>7} {pr:>7} {entry:<15} "
        f"{item.stage.value:<15} {_disposition(item):<28} {attempts:<24} {elapsed_s:7.1f}s"
    )


def _review_publication_targets(item: WorkItem) -> dict[str, list[str]]:
    """Return safe target labels for the review publication summary."""
    raw_summary = item.payload.get("review_publication_summary")
    if not isinstance(raw_summary, dict):
        return {}
    targets: dict[str, list[str]] = {}
    for bucket in ("published", "corrected", "could_not_publish"):
        raw_entries = raw_summary.get(bucket)
        if not isinstance(raw_entries, list):
            continue
        labels: list[str] = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            finding = entry.get("finding") if bucket == "corrected" else entry
            if not isinstance(finding, dict):
                finding = entry.get("finding")
            path = finding.get("path") if isinstance(finding, dict) else None
            line = finding.get("line") if isinstance(finding, dict) else None
            side = finding.get("side") if isinstance(finding, dict) else None
            if not isinstance(path, str) or not path:
                continue
            target = f"{path}:{line if line is not None else '?'}:{side or '?'}"
            reason = entry.get("reason")
            if isinstance(reason, str) and reason:
                target = f"{target} [{reason}]"
            labels.append(target)
        if labels:
            targets[bucket] = labels
    return targets


def print_summary(  # noqa: C901
    items: list[WorkItem],
    stats: RunStats,
    preserved: list[PreservedWorktree],
    *,
    json_out: bool,
    recovery_preserved: Sequence[PreservedWorktree] = (),
    terminal_summary: TerminalSummary | None = None,
) -> None:
    """Log the end-of-run summary; emit the JSON envelope when requested.

    Args:
        items: Recent detailed work items (results attached).
        stats: Aggregate run statistics (exit code, loops, agent time, wall).
        preserved: ``(repo, issue_number, worktree_path)`` tuples retained for
            recovery or failed-item debugging.
        json_out: Emit the machine-readable ``emit_json_status`` envelope.
        recovery_preserved: Receipt-backed detached-review checkouts requiring
            inspection-only recovery guidance.
        terminal_summary: Optional constant-space aggregate for all terminal
            outcomes.  When supplied, it controls aggregate counts while
            ``items`` remains the bounded detailed reporting window.

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
        review_run = _review_run(item)
        if review_run is not None:
            logger.info(
                "    review-run reason=%s head=%s",
                review_run[0],
                review_run[1],
            )
        publication_targets = _review_publication_targets(item)
        if publication_targets:
            logger.info(
                "    review-publication: published=%s corrected=%s could-not-publish=%s",
                publication_targets.get("published", []),
                publication_targets.get("corrected", []),
                publication_targets.get("could_not_publish", []),
            )
        cycle_id = item.payload.get("plan_review_cycle_id")
        if cycle_id:
            logger.info(
                "    plan-review cycle=%s session=%s round=%s revision=%s",
                cycle_id,
                item.payload.get("plan_review_session_id") or "pending",
                item.payload.get("review_round", 0),
                item.payload.get("plan_revision", 1),
            )

    if terminal_summary is None:
        dispositions: dict[str, int] = {}
        per_stage: dict[str, int] = {}
        planning_action_counter: Counter[str] = Counter()
        for item in items:
            dispositions[_disposition_bucket(item)] = (
                dispositions.get(_disposition_bucket(item), 0) + 1
            )
            per_stage[item.stage.value] = per_stage.get(item.stage.value, 0) + 1
            planning_action_counter.update(_summary_actions(item))
        total_items = len(items)
        planning_actions = dict(sorted(planning_action_counter.items()))
        review_run_reasons = _review_run_reasons(items)
    else:
        dispositions = terminal_summary.dispositions
        per_stage = terminal_summary.per_stage
        total_items = terminal_summary.total
        planning_actions = terminal_summary.planning_actions
        review_run_reasons = terminal_summary.review_run_reasons

    logger.info("")
    logger.info("=== Aggregates ===")
    logger.info("  items: %d  dispositions: %s", total_items, dict(sorted(dispositions.items())))
    logger.info("  per-stage: %s", dict(sorted(per_stage.items())))
    if planning_actions:
        logger.info("  planning-actions: %s", planning_actions)
    if review_run_reasons:
        logger.info("  review-run reasons: %s", review_run_reasons)
    if terminal_summary is not None and total_items > len(items):
        logger.info("  detailed terminal rows retained: %d of %d", len(items), total_items)
    logger.info(
        "  agent jobs: %d (%.1fs total)  loops: %d  wall: %.1fs  interrupted: %s",
        stats.agent_job_count,
        stats.agent_job_time_s,
        stats.loops_run,
        stats.wall_s,
        stats.interrupted,
    )
    logger.info(
        "  auxiliary jobs: %d (%.1fs total, %d failed)",
        stats.auxiliary_job_count,
        stats.auxiliary_job_time_s,
        stats.auxiliary_job_failure_count,
    )

    logger.info("  auxiliary jobs deferred: %d", stats.auxiliary_job_deferred_count)

    for line in format_preserved_worktrees(preserved, sys.argv[0]):
        logger.info("%s", line)
    for line in format_direct_review_recovery_worktrees(recovery_preserved):
        logger.info("%s", line)

    if json_out:
        review_sessions = [
            {
                "repo": item.repo,
                "issue": item.issue,
                "planning_cycle_id": item.payload["plan_review_cycle_id"],
                "reviewer_session_id": item.payload.get("plan_review_session_id"),
                "review_round": item.payload.get("review_round", 0),
                "plan_revision": item.payload.get("plan_revision", 1),
            }
            for item in items
            if item.payload.get("plan_review_cycle_id")
        ]
        resumable = [
            f"{item.repo}#{item.issue or item.pr or ''}@{item.stage.value}"
            for item in items
            if item.result is not None and item.result.reason.startswith("resumable")
        ]
        review_publication = [
            {
                "repo": item.repo,
                "issue": item.issue,
                "pr": item.pr,
                **_review_publication_targets(item),
            }
            for item in items
            if _review_publication_targets(item)
        ]
        emit_json_status(
            stats.exit_code,
            message=_json_message(stats.exit_code),
            dispositions=dict(sorted(dispositions.items())),
            planning_actions=planning_actions,
            review_run_reasons=review_run_reasons,
            loops_run=stats.loops_run,
            agent_jobs=stats.agent_job_count,
            agent_job_time_s=round(stats.agent_job_time_s, 1),
            auxiliary_jobs=stats.auxiliary_job_count,
            auxiliary_job_time_s=round(stats.auxiliary_job_time_s, 1),
            auxiliary_job_failures=stats.auxiliary_job_failure_count,
            auxiliary_jobs_deferred=stats.auxiliary_job_deferred_count,
            wall_s=round(stats.wall_s, 1),
            resumable=resumable,
            preserved_worktrees=[[number, path] for _, number, path in preserved],
            recovery_worktrees=[[number, path] for _, number, path in recovery_preserved],
            plan_review_sessions=review_sessions,
            review_publication=review_publication,
        )
