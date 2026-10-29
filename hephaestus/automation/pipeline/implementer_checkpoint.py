"""Implementer progress checkpoints and mid-run resume records (issue #3435).

An implementer turn that dies at its invocation deadline keeps the lane
worktree, but the loop so far retained no record of what the turn had
reached: the retry restarted from scratch. This module gives the pipeline
side a bounded record of the turn:

- a lane-local checkpoint record written on dispatch, at a configurable
  interval, and on timeout classification;
- an ``implementer_progress`` event on the durable event log at the same
  interval;
- a bounded, advisory resume context that the implementation stage injects
  into the next implementer prompt after a timeout.

The checkpoint writer is pipeline-side because a timeout kill can fire at
any instant and a record that needs the agent's cooperation loses exactly
the burned checkpoints. The record lives in the lane worktree's Git
metadata directory, so lane adoption keeps it and the working-tree diff
never contains it. The checkpoint never alters Git state and never decides
to skip work: it informs the re-prompted agent only. A stale, corrupt, or
head-superseded checkpoint fails closed: it is discarded and the retry
runs a fresh turn.

Config: ``implementer_checkpoint_interval`` (CLI
``--implementer-checkpoint-interval``) selects the interval; zero disables
the whole feature. With the feature disabled there is no new branch on the
hot path beyond one config read.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from hephaestus.automation.agent_config import AGENT_IMPLEMENTER, implementer_checkpoint_interval_s
from hephaestus.io.utils import write_secure
from hephaestus.prompts import PromptCatalog
from hephaestus.utils.git import _is_full_commit_sha, run_git

from .jobs import AgentJob, JobHandle, JobResult
from .routing import StageName
from .work_item import WorkItem

logger = logging.getLogger(__name__)

CHECKPOINT_SCHEMA_VERSION: int = 1
CHECKPOINT_FILENAME: str = "hephaestus-implementer-checkpoint.json"

_MAX_CHANGED_FILES: int = 50
_MAX_PATH_CHARS: int = 200
_MAX_STEP_CHARS: int = 200
_MAX_TEST_CHARS: int = 500
_MAX_SESSION_CHARS: int = 128

#: Payload key for the implementer-turn start stamp (monotonic clock).
#: Its presence marks an ordinary implementer turn in flight; checkpoints and
#: timeout session continuity apply only while the mark stands.
_TURN_STAMP_KEY: str = "_impl_checkpoint_turn_start"


@dataclass(frozen=True, slots=True)
class ImplementerCheckpoint:
    """One bounded pipeline-side record of an implementer turn."""

    schema_version: int
    issue: int
    lane_identity: str
    elapsed_s: float
    attempt: int
    head_sha: str | None
    changed_files: tuple[str, ...]
    last_step: str
    last_test_result: str
    session_id: str | None


@dataclass(frozen=True, slots=True)
class LaneSnapshot:
    """One live observation of the lane worktree and its Git metadata."""

    git_dir: Path
    head_sha: str | None
    changed_files: tuple[str, ...]


#: Tick entries in the coordinator's ``_impl_checkpoint_ticks`` map:
#: ``(turn_start, next_due)`` in monotonic seconds per item key.
TickEntry = tuple[float, float]


def interval_from_config(config: Any) -> int:
    """Return the nonnegative checkpoint interval from pipeline config.

    The value zero disables the checkpoint, progress, and resume paths. A
    negative value is a configuration error and fails plainly.

    Args:
        config: Pipeline config carrying ``implementer_checkpoint_interval``.

    Returns:
        The configured interval, or the fixed default when unset.

    Raises:
        ValueError: If the configured interval is negative.

    """
    value = getattr(config, "implementer_checkpoint_interval", None)
    if value is None:
        return implementer_checkpoint_interval_s()
    interval = int(value)
    if interval < 0:
        raise ValueError("implementer_checkpoint_interval must be nonnegative")
    return interval


def stamp_turn(item: WorkItem, *, now: float) -> None:
    """Mark the start of one ordinary implementer turn on the item payload."""
    item.payload[_TURN_STAMP_KEY] = now


def lane_identity(branch: str, worktree: str) -> str:
    """Return the lane binding that ties a checkpoint to a branch and path."""
    return f"{branch}@/{worktree.lstrip('/')}"


def checkpoint_path(git_dir: Path) -> Path:
    """Return the checkpoint file location inside the lane Git metadata."""
    return git_dir / CHECKPOINT_FILENAME


def _valid_stamp(stamp: Any) -> bool:
    """Return whether the payload turn stamp is a usable clock value."""
    return (
        not isinstance(stamp, bool)
        and isinstance(stamp, (int, float))
        and math.isfinite(float(stamp))
        and float(stamp) >= 0
    )


def _sanitize_text(value: Any, limit: int) -> str:
    """Return one bounded single-line text field without control characters."""
    text = str(value) if value is not None else ""
    text = "".join(char for char in text if ord(char) >= 32 and ord(char) != 127)
    return text[:limit]


def _sanitize_files(values: Any) -> tuple[str, ...]:
    """Return a capped tuple of bounded, control-character-free file paths."""
    files: list[str] = []
    if not isinstance(values, (list, tuple)):
        return ()
    for raw in values:
        clean = _sanitize_text(raw, _MAX_PATH_CHARS)
        if clean and clean not in files:
            files.append(clean)
        if len(files) >= _MAX_CHANGED_FILES:
            break
    return tuple(files)


def _parse_porcelain(text: str) -> tuple[str, ...]:
    """Return changed paths from ``git status --porcelain=v1`` output."""
    files: list[str] = []
    for line in text.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        clean = _sanitize_text(path, _MAX_PATH_CHARS)
        if clean and clean not in files:
            files.append(clean)
        if len(files) >= _MAX_CHANGED_FILES:
            break
    return tuple(files)


def _git_dir(worktree: Path, *, timeout_s: int) -> Path:
    """Return the lane worktree's Git metadata directory, raising plainly.

    Raises:
        subprocess.CalledProcessError: If the Git query fails.
        subprocess.TimeoutExpired: If the Git query exceeds ``timeout_s``.

    """
    result = run_git(
        ["-C", str(worktree), "rev-parse", "--git-dir"],
        timeout=timeout_s,
        check=True,
    )
    raw_dir = result.stdout.strip()
    git_dir = Path(raw_dir)
    if not git_dir.is_absolute():
        git_dir = (worktree / git_dir).resolve()
    return git_dir


def collect_lane(worktree: Path, *, timeout_s: int) -> LaneSnapshot:
    """Snapshot the lane worktree: changed files, head, and Git metadata dir.

    Raises plainly on a failed or timed-out Git call; the checkpoint
    boundaries degrade on that failure.

    Args:
        worktree: The lane worktree path.
        timeout_s: Per-call Git metadata timeout in seconds.

    Returns:
        The live lane snapshot.

    Raises:
        subprocess.CalledProcessError: If a Git query fails.
        subprocess.TimeoutExpired: If a Git query exceeds ``timeout_s``.

    """
    git_dir = _git_dir(worktree, timeout_s=timeout_s)
    head_result = run_git(
        ["-C", str(worktree), "rev-parse", "HEAD"],
        timeout=timeout_s,
        check=True,
    )
    head_raw = head_result.stdout.strip()
    head_sha: str | None = head_raw if _is_full_commit_sha(head_raw) else None
    status_result = run_git(
        ["-C", str(worktree), "status", "--porcelain=v1"],
        timeout=timeout_s,
        check=True,
    )
    return LaneSnapshot(
        git_dir=git_dir,
        head_sha=head_sha,
        changed_files=_parse_porcelain(status_result.stdout),
    )


def _encode(checkpoint: ImplementerCheckpoint) -> str:
    """Serialize one checkpoint record to bounded JSON text."""
    return (
        json.dumps(
            {
                "schema_version": checkpoint.schema_version,
                "issue": checkpoint.issue,
                "lane_identity": checkpoint.lane_identity,
                "elapsed_s": checkpoint.elapsed_s,
                "attempt": checkpoint.attempt,
                "head_sha": checkpoint.head_sha,
                "changed_files": list(checkpoint.changed_files),
                "last_step": checkpoint.last_step,
                "last_test_result": checkpoint.last_test_result,
                "session_id": checkpoint.session_id,
            },
            sort_keys=True,
        )
        + "\n"
    )


def write_checkpoint(
    lane: LaneSnapshot,
    *,
    issue: int,
    branch: str,
    worktree: str,
    elapsed_s: float,
    attempt: int,
    step: str,
    last_test_result: str = "",
    session_id: str | None = None,
) -> ImplementerCheckpoint:
    """Build one bounded checkpoint and write it to the lane atomically.

    Raises plainly on a write failure; the checkpoint boundaries degrade on
    that failure.

    Returns:
        The record that was written.

    """
    checkpoint = ImplementerCheckpoint(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        issue=issue,
        lane_identity=lane_identity(branch, worktree),
        elapsed_s=max(0.0, float(elapsed_s)),
        attempt=attempt,
        head_sha=lane.head_sha,
        changed_files=_sanitize_files(lane.changed_files),
        last_step=_sanitize_text(step, _MAX_STEP_CHARS),
        last_test_result=_sanitize_text(last_test_result, _MAX_TEST_CHARS),
        session_id=_sanitize_text(session_id, _MAX_SESSION_CHARS) or None,
    )
    write_secure(checkpoint_path(lane.git_dir), _encode(checkpoint))
    return checkpoint


def read_checkpoint(path: Path) -> dict[str, Any] | None:
    """Read the raw checkpoint mapping, returning ``None`` when usable data is absent.

    Returns:
        The decoded mapping, or ``None`` for a missing or undecodable file.

    """
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def validate_checkpoint(
    raw: dict[str, Any],
    *,
    issue: int,
    branch: str,
    worktree: str,
) -> ImplementerCheckpoint | None:
    """Rebuild one bounded checkpoint from storage, rejecting any mismatch.

    Returns:
        The validated record, or ``None`` when the data is malformed or
        bound to a different lane; a caller must discard such a checkpoint.

    """
    try:
        schema_version = raw["schema_version"]
        raw_issue = raw["issue"]
        raw_lane = raw["lane_identity"]
        elapsed = raw["elapsed_s"]
        attempt = raw["attempt"]
    except KeyError:
        return None
    if (
        schema_version != CHECKPOINT_SCHEMA_VERSION
        or not isinstance(raw_issue, int)
        or isinstance(raw_issue, bool)
        or raw_issue != issue
        or raw_lane != lane_identity(branch, worktree)
        or isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(float(elapsed))
        or float(elapsed) < 0
        or not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or attempt < 1
    ):
        return None
    head_sha = raw.get("head_sha")
    if head_sha is not None and not _is_full_commit_sha(head_sha):
        return None
    session_id = raw.get("session_id")
    return ImplementerCheckpoint(
        schema_version=schema_version,
        issue=raw_issue,
        lane_identity=str(raw_lane),
        elapsed_s=float(elapsed),
        attempt=attempt,
        head_sha=head_sha if isinstance(head_sha, str) else None,
        changed_files=_sanitize_files(raw.get("changed_files")),
        last_step=_sanitize_text(raw.get("last_step"), _MAX_STEP_CHARS),
        last_test_result=_sanitize_text(raw.get("last_test_result"), _MAX_TEST_CHARS),
        session_id=_sanitize_text(session_id, _MAX_SESSION_CHARS) or None,
    )


def discard_checkpoint(path: Path) -> None:
    """Best-effort remove one checkpoint file; a miss is not an error."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("implementer checkpoint: could not discard %s", path)


def render_resume_context(checkpoint: ImplementerCheckpoint) -> str:
    """Render the bounded advisory resume-context block for the retry prompt."""
    return PromptCatalog.current().render(
        "implementation/resume_context.j2",
        elapsed_s=f"{checkpoint.elapsed_s:.0f}",
        attempt=checkpoint.attempt,
        changed_files=list(checkpoint.changed_files),
        last_step=checkpoint.last_step or "not recorded",
        last_test_result=checkpoint.last_test_result or "none recorded",
        file_cap=_MAX_CHANGED_FILES,
    )


def _current_head(worktree: Path, *, timeout_s: int) -> str | None:
    """Return the lane worktree head, raising plainly on a Git failure.

    Raises:
        subprocess.CalledProcessError: If the Git query fails.
        subprocess.TimeoutExpired: If the Git query exceeds ``timeout_s``.

    """
    result = run_git(
        ["-C", str(worktree), "rev-parse", "HEAD"],
        timeout=timeout_s,
        check=True,
    )
    head = result.stdout.strip()
    return head if _is_full_commit_sha(head) else None


def resume_context_for(
    *,
    config: Any,
    worktree: Path,
    issue: int,
    branch: str,
    timeout_s: int,
) -> str:
    """Return the bounded advisory resume context for the next implementer turn.

    Fails closed: a missing, corrupt, superseded, or unreadable checkpoint
    yields an empty string, never an exception, so the retry still runs.

    Args:
        config: Pipeline config; a zero interval disables the read.
        worktree: The lane worktree path.
        issue: The issue the lane implements.
        branch: The lane branch.
        timeout_s: Per-call Git metadata timeout in seconds.

    Returns:
        The advisory block, or an empty string when none applies.

    """
    if interval_from_config(config) <= 0:
        return ""
    try:
        path = checkpoint_path(_git_dir(worktree, timeout_s=timeout_s))
        raw = read_checkpoint(path)
        if raw is None:
            return ""
        checkpoint = validate_checkpoint(raw, issue=issue, branch=branch, worktree=str(worktree))
        if checkpoint is None:
            discard_checkpoint(path)
            return ""
        head = _current_head(worktree, timeout_s=timeout_s)
        if checkpoint.head_sha is not None and head is not None and head != checkpoint.head_sha:
            # The lane head moved (for example a rebase in between): this
            # checkpoint's context no longer matches the lane.
            discard_checkpoint(path)
            return ""
        return render_resume_context(checkpoint)
    except (OSError, ValueError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        logger.warning(
            "implementer checkpoint: resume context unavailable for issue %s (%s); "
            "continuing with a fresh turn",
            issue,
            type(exc).__name__,
        )
        return ""


def _progress_checkpoint(
    item: WorkItem,
    stamp: float,
    *,
    now: float,
    step: str,
    metadata_timeout_s: int,
    session_id: str | None = None,
) -> ImplementerCheckpoint:
    """Write the periodic or final checkpoint for one in-flight implementer turn."""
    lane = collect_lane(Path(item.worktree), timeout_s=metadata_timeout_s)
    if session_id is None:
        session_id = item.session_ids.get(AGENT_IMPLEMENTER)
    return write_checkpoint(
        lane,
        issue=item.issue or 0,
        branch=item.branch,
        worktree=item.worktree,
        elapsed_s=max(0.0, now - stamp),
        attempt=item.attempts.get("implement", 0) + 1,
        step=step,
        last_test_result=_sanitize_text(item.payload.get("test_output", ""), _MAX_TEST_CHARS),
        session_id=session_id,
    )


def _is_implementer_turn(item: WorkItem, job: JobHandle | Any) -> bool:
    """Return whether an in-flight pair names a stamped implementer turn."""
    job_ref = job.job if isinstance(job, JobHandle) else job
    return (
        item.stage is StageName.IMPLEMENTATION
        and _valid_stamp(item.payload.get(_TURN_STAMP_KEY))
        and isinstance(job_ref, AgentJob)
        and job_ref.session_agent == AGENT_IMPLEMENTER
        and item.issue is not None
        and bool(item.worktree)
    )


def observe_in_flight(
    in_flight: Mapping[JobHandle, WorkItem],
    *,
    ticks: dict[str, TickEntry],
    now: float,
    interval_s: int,
    item_key: Callable[[WorkItem], str],
    metadata_timeout_s: int,
    emit: Callable[..., None],
) -> None:
    """Write due implementer checkpoints and emit progress events.

    The first observation of a stamped turn writes the dispatch checkpoint
    immediately. Later observations write at ``interval_s`` from the last
    write and emit one ``implementer_progress`` event with each interval
    write. Tick entries for items that left the in-flight registry are
    pruned. A zero interval disables every write and event.

    Each per-item write is a boundary: a failed Git call or write
    degrades to a warning and the item is skipped this tick.

    Args:
        in_flight: The coordinator's main-lane in-flight registry.
        ticks: Mutable per-item ``(turn_start, next_due)`` tick state.
        now: The monotonic clock reading for this tick.
        interval_s: The configured interval; zero disables the walk.
        item_key: The coordinator's item-key accessor.
        metadata_timeout_s: Per-call Git metadata timeout in seconds.
        emit: The coordinator's durable event recorder.

    """
    if interval_s <= 0:
        return
    due_keys: set[str] = set()
    for handle, item in list(in_flight.items()):
        if not _is_implementer_turn(item, handle):
            continue
        stamp = float(item.payload[_TURN_STAMP_KEY])
        key = item_key(item)
        due_keys.add(key)
        entry = ticks.get(key)
        if entry is not None and entry[0] != stamp:
            entry = None
        if entry is not None and now < entry[1]:
            continue
        first_write = entry is None
        step = "turn dispatched" if first_write else "turn in progress"
        try:
            checkpoint = _progress_checkpoint(
                item,
                stamp,
                now=now,
                step=step,
                metadata_timeout_s=metadata_timeout_s,
            )
        except (
            OSError,
            ValueError,
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
        ) as exc:
            logger.warning(
                "implementer checkpoint: progress snapshot failed for %s (%s); skipping this tick",
                key,
                type(exc).__name__,
            )
            ticks[key] = (stamp, now + interval_s)
            continue
        ticks[key] = (stamp, now + interval_s)
        if first_write:
            continue
        emit(
            "implementer_progress",
            key,
            {
                "elapsed_s": checkpoint.elapsed_s,
                "files_touched_count": len(checkpoint.changed_files),
                "last_step": checkpoint.last_step,
            },
        )
    for key in list(ticks):
        if key not in due_keys:
            del ticks[key]


def apply_timeout_session_continuity(
    item: WorkItem,
    result: JobResult,
    *,
    job: AgentJob,
    interval_s: int,
) -> JobResult:
    """Carry the attempted resume id onto a timeout result when the provider ran it.

    A provider killed at its deadline while resuming a session was executing
    that exact identity, so the already-known resume id is the one value the
    adapter boundary can safely attach. A fresh turn yields no id; the
    checkpoint record still applies. No other classification changes.
    """
    if (
        interval_s <= 0
        or result.error != "timeout"
        or result.session_id is not None
        or not _valid_stamp(item.payload.get(_TURN_STAMP_KEY))
        or not job.resume_session_id
    ):
        return result
    return replace(result, session_id=job.resume_session_id)


def classify_agent_completion(
    item: WorkItem,
    result: JobResult,
    *,
    interval_s: int,
    metadata_timeout_s: int,
    now: float,
) -> None:
    """Close the checkpoint life cycle for one completed implementer turn.

    A timeout classification keeps the lane checkpoint current (best effort)
    so the retry can resume from it. Any other terminal classification —
    success or another failure kind — discards the checkpoint: resume
    continuity is scoped to timeout retries. The turn stamp is always
    closed so a later turn starts from a clean mark.
    """
    stamp = item.payload.pop(_TURN_STAMP_KEY, None)
    if not _valid_stamp(stamp) or interval_s <= 0:
        return
    if item.issue is None or not item.worktree:
        return
    if result.error == "timeout":
        try:
            _progress_checkpoint(
                item,
                float(stamp),
                now=now,
                step="invocation deadline reached (timeout)",
                metadata_timeout_s=metadata_timeout_s,
                session_id=result.session_id or item.session_ids.get(AGENT_IMPLEMENTER),
            )
        except (
            OSError,
            ValueError,
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
        ) as exc:
            logger.warning(
                "implementer checkpoint: timeout snapshot failed for issue %s (%s)",
                item.issue,
                type(exc).__name__,
            )
        return
    try:
        git_dir = _git_dir(Path(item.worktree), timeout_s=metadata_timeout_s)
        discard_checkpoint(checkpoint_path(git_dir))
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        logger.warning(
            "implementer checkpoint: could not discard checkpoint for issue %s (%s)",
            item.issue,
            type(exc).__name__,
        )


__all__ = [
    "CHECKPOINT_FILENAME",
    "CHECKPOINT_SCHEMA_VERSION",
    "ImplementerCheckpoint",
    "LaneSnapshot",
    "TickEntry",
    "apply_timeout_session_continuity",
    "checkpoint_path",
    "classify_agent_completion",
    "collect_lane",
    "discard_checkpoint",
    "interval_from_config",
    "lane_identity",
    "observe_in_flight",
    "read_checkpoint",
    "render_resume_context",
    "resume_context_for",
    "stamp_turn",
    "validate_checkpoint",
    "write_checkpoint",
]
