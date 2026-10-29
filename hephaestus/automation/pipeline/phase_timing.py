"""Bounded per-work-item phase timing and retry causes. Pure data, zero I/O.

Closed vocabularies mirror the issue-required phase list and retry causes.
The coordinator thread records observation-only facts here; a timing failure
never routes, poisons, or fails a work item. The durable receipt encoder is
fail-closed: it rejects foreign keys, unbounded text, and invalid durations
so no prompt text, credential, path, host name, or free text can enter the
event log (epic #3295).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .routing import StageName

_FULL_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_TEXT_LIMIT = 128
_REPO_LIMIT = 256


class PhaseName(StrEnum):
    """Workflow phases that report time for one work item (issue #3295)."""

    ISSUE_INTAKE = "issue_intake"
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    TEST_EXECUTION = "test_execution"
    PUBLICATION = "publication"
    CI_WAIT = "ci_wait"
    CORRECTION = "correction"
    MERGE_ADMISSION = "merge_admission"
    EXTERNAL_WAIT = "external_wait"


class RetryCause(StrEnum):
    """Closed retry causes that explain repeated work (issue #3295)."""

    SOURCE_CHANGE = "source_change"
    REVIEW_FINDINGS = "review_findings"
    TEST_FAILURE = "test_failure"
    CHECK_FAILURE = "check_failure"
    REMOTE_MOVEMENT = "remote_movement"
    TOOL_FAILURE = "tool_failure"
    EXTERNAL_WAIT = "external_wait"


#: Stage-to-phase map. Stages outside the map are auxiliary or terminal and
#: do not open phase intervals.
PHASE_FOR_STAGE: dict[StageName, PhaseName] = {
    StageName.REPO: PhaseName.ISSUE_INTAKE,
    StageName.PLANNING: PhaseName.PLANNING,
    StageName.PLAN_REVIEW: PhaseName.PLANNING,
    StageName.IMPLEMENTATION: PhaseName.IMPLEMENTATION,
    StageName.PR_REVIEW: PhaseName.IMPLEMENTATION,
    StageName.MERGE_WAIT: PhaseName.MERGE_ADMISSION,
}

#: Git operations that publish work; their completions count publication time.
PUBLICATION_OPS: frozenset[str] = frozenset(
    {
        "commit_push",
        "publish_dirty_direct_continuation",
        "finish_dirty_direct_publication",
    }
)

#: Phases whose time is waiting classification by definition.
_WAITING_PHASES = frozenset({PhaseName.CI_WAIT, PhaseName.EXTERNAL_WAIT})

#: Named fail-route notes with a fixed cause. Unknown notes use the stage
#: default so the map is total without ever recording free text.
_NOTE_CAUSES: dict[str, RetryCause] = {
    "plan_changed": RetryCause.SOURCE_CHANGE,
    "nogo": RetryCause.REVIEW_FINDINGS,
    "plan_missing": RetryCause.REVIEW_FINDINGS,
    "plan_not_go": RetryCause.REVIEW_FINDINGS,
    "implementation_remediation": RetryCause.REVIEW_FINDINGS,
    "scope_retraction_before_scope_block": RetryCause.REVIEW_FINDINGS,
    "tests_failed": RetryCause.TEST_FAILURE,
    "merge_not_ready": RetryCause.CHECK_FAILURE,
    "reviewed_head_drift": RetryCause.REMOTE_MOVEMENT,
    "reviewed_head_missing": RetryCause.REMOTE_MOVEMENT,
    "merge_conflicting": RetryCause.REMOTE_MOVEMENT,
    "not_implementation_go": RetryCause.REMOTE_MOVEMENT,
    "rebase_conflict": RetryCause.REMOTE_MOVEMENT,
    "rate_guard": RetryCause.EXTERNAL_WAIT,
}

#: Poll-style retry notes record wait time but no cause: there is nothing to
#: retry because of; the cause belongs to the cycle that follows.
_POLL_NOTES = frozenset({"merge_wait_poll"})

#: Error classes that mean a tool failure whenever a note does not say more.
_TOOL_ERROR_CLASSES = frozenset({"timeout", "worker_crash", "error"})

#: Per-stage defaults keep the cause map total for unknown notes.
_STAGE_FALLBACK: dict[StageName, RetryCause] = {
    StageName.REPO: RetryCause.TOOL_FAILURE,
    StageName.PLANNING: RetryCause.TOOL_FAILURE,
    StageName.PLAN_REVIEW: RetryCause.REVIEW_FINDINGS,
    StageName.IMPLEMENTATION: RetryCause.TOOL_FAILURE,
    StageName.PR_REVIEW: RetryCause.REVIEW_FINDINGS,
    StageName.MERGE_WAIT: RetryCause.CHECK_FAILURE,
}


def _now(value: object, *, name: str = "now_s") -> float:
    """Return one finite time mark."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"invalid {name}")
    return float(value)


def _bounded_text(value: object, *, name: str, limit: int = _TEXT_LIMIT) -> str:
    """Return one bounded text field without control characters."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"invalid {name}")
    return value


def classify_retry_cause(stage: StageName, note: str, error_class: str | None) -> RetryCause | None:
    """Classify one retry or correction with closed inputs only.

    Named fail-route notes map through the closed note table. Poll-style
    retries record no cause. Tool error classes map to ``tool_failure``. Any
    other combination records the stage default, never free text.
    """
    if note in _POLL_NOTES or error_class == "interrupted":
        return None
    if cause := _NOTE_CAUSES.get(note):
        return cause
    if error_class in _TOOL_ERROR_CLASSES:
        return RetryCause.TOOL_FAILURE
    return _STAGE_FALLBACK.get(stage, RetryCause.TOOL_FAILURE)


@dataclass
class PhaseRecord:
    """Bounded accumulated timing facts for one phase of one work item."""

    start_s: float | None = None
    end_s: float | None = None
    elapsed_s: float = 0.0
    wait_s: float = 0.0
    attempts: int = 0
    suspended_s: float = 0.0

    def mark(self, now_s: float) -> None:
        """Extend the observed start/end bounds with one finite mark."""
        now_s = _now(now_s)
        if self.start_s is None or now_s < self.start_s:
            self.start_s = now_s
        if self.end_s is None or now_s > self.end_s:
            self.end_s = now_s

    def add_interval(self, start_s: float, end_s: float) -> None:
        """Accumulate one closed [start, end] interval into elapsed time."""
        start_s = _now(start_s, name="start_s")
        end_s = _now(end_s, name="end_s")
        if end_s < start_s:
            raise ValueError("invalid interval")
        self.mark(start_s)
        self.mark(end_s)
        self.elapsed_s += end_s - start_s

    def add_wait(self, start_s: float, end_s: float) -> None:
        """Accumulate one closed wait interval into elapsed and wait time."""
        before = self.elapsed_s
        self.add_interval(start_s, end_s)
        self.wait_s += self.elapsed_s - before

    @property
    def active_s(self) -> float:
        """Elapsed time that is neither waiting nor attributed elsewhere."""
        return max(0.0, self.elapsed_s - self.wait_s - self.suspended_s)


@dataclass
class WorkItemTiming:
    """Coordinator-owned timing accumulator for one work item.

    All access is single-threaded (coordinator thread only). After
    :meth:`close` every recording method is a no-op, so a terminal path that
    runs once per item never double-counts completed phase intervals.
    """

    phases: dict[PhaseName, PhaseRecord] = field(default_factory=dict)
    causes: Counter[str] = field(default_factory=Counter)
    current_phase: PhaseName | None = None
    current_waiting: bool = False
    last_cause: RetryCause | None = None
    last_error_class: str | None = None
    closed: bool = False
    first_marker_s: float | None = None
    last_marker_s: float | None = None
    _open_phase: PhaseName | None = None
    _open_s: float | None = None
    _wait_phase: PhaseName | None = None
    _wait_open_s: float | None = None

    def _record(self, phase: PhaseName) -> PhaseRecord:
        return self.phases.setdefault(phase, PhaseRecord())

    def _mark(self, now_s: float) -> None:
        now_s = _now(now_s)
        if self.first_marker_s is None or now_s < self.first_marker_s:
            self.first_marker_s = now_s
        if self.last_marker_s is None or now_s > self.last_marker_s:
            self.last_marker_s = now_s

    def _close_interval(self, now_s: float) -> None:
        if self._open_phase is not None and self._open_s is not None:
            self._record(self._open_phase).add_interval(self._open_s, now_s)
        self._open_s = None

    def enter_phase(self, phase: PhaseName, now_s: float) -> None:
        """Open the phase interval; close any other open interval first."""
        if self.closed:
            return
        now_s = _now(now_s)
        self._close_interval(now_s)
        self._open_phase = phase
        self._open_s = now_s
        self._record(phase).mark(now_s)
        self._mark(now_s)
        self.current_phase = phase
        self.current_waiting = False

    def leave_phase(self, phase: PhaseName, now_s: float) -> None:
        """Close the named phase interval when it is the current phase."""
        if self.closed or self._open_phase is not phase:
            return
        now_s = _now(now_s)
        self._close_interval(now_s)
        self._open_phase = None
        self.current_phase = None
        self._mark(now_s)

    def begin_wait(self, phase: PhaseName, now_s: float) -> None:
        """Open a wait interval on *phase*, suspending the current interval."""
        if self.closed or self.current_waiting:
            return
        now_s = _now(now_s)
        self._close_interval(now_s)
        self._wait_phase = phase
        self._wait_open_s = now_s
        self._record(phase).attempts += 1
        self._record(phase).mark(now_s)
        self._mark(now_s)
        self.current_phase = phase
        self.current_waiting = True

    def end_wait(self, now_s: float) -> None:
        """Close the open wait interval and resume the suspended interval."""
        if self.closed or not self.current_waiting or self._wait_open_s is None:
            return
        now_s = _now(now_s)
        if self._wait_phase is not None:
            self._record(self._wait_phase).add_wait(self._wait_open_s, now_s)
        self._wait_phase = None
        self._wait_open_s = None
        self._mark(now_s)
        if self._open_phase is not None:
            self._open_s = now_s
            self._record(self._open_phase).mark(now_s)
            self.current_phase = self._open_phase
        self.current_waiting = False

    def record_job(
        self, phase: PhaseName, duration_s: float, error_class: str | None, now_s: float
    ) -> None:
        """Attribute one job completion to *phase*.

        A completion attributed outside the open interval suspends the same
        duration in the open phase so run-level totals never double count.
        """
        if self.closed:
            return
        now_s = _now(now_s)
        duration = _now(duration_s, name="duration_s")
        if error_class is not None:
            error_class = _bounded_text(error_class, name="error_class")
        start = now_s - duration
        record = self._record(phase)
        record.attempts += 1
        record.mark(start)
        record.mark(now_s)
        self._mark(start)
        self._mark(now_s)
        if phase is self._open_phase and self._open_s is not None:
            pass
        else:
            record.add_interval(start, now_s)
            if self._open_phase is not None:
                self._record(self._open_phase).suspended_s += duration
        self.last_error_class = error_class

    def record_cause(self, cause: RetryCause) -> None:
        """Count one classified retry cause; clear the pending error class."""
        if self.closed:
            return
        self.causes[cause.value] += 1
        self.last_cause = cause
        self.last_error_class = None

    def record_correction(self, cause: RetryCause, now_s: float) -> None:
        """Count one correction cycle with its closed cause."""
        if self.closed:
            return
        self.record_cause(cause)
        record = self._record(PhaseName.CORRECTION)
        record.attempts += 1
        record.mark(_now(now_s))
        self._mark(now_s)

    def close(self, final_now_s: float) -> None:
        """Close open intervals once and freeze the stalled-phase report."""
        if self.closed:
            return
        now_s = _now(final_now_s, name="final_now_s")
        if self.current_waiting and self._wait_open_s is not None:
            if self._wait_phase is not None:
                self._record(self._wait_phase).add_wait(self._wait_open_s, now_s)
            self._wait_phase = None
            self._wait_open_s = None
        self._close_interval(now_s)
        self._mark(now_s)
        self.closed = True

    def wall_s(self) -> float:
        """Return the recorded wall span, or zero for an untouched item."""
        if self.first_marker_s is None or self.last_marker_s is None:
            return 0.0
        return max(0.0, self.last_marker_s - self.first_marker_s)

    def totals(self) -> tuple[float, float]:
        """Return run-level (active, wait) totals across all phases."""
        return (
            sum(record.active_s for record in self.phases.values()),
            sum(record.wait_s for record in self.phases.values()),
        )


def receipt_head_sha(payload: dict[str, Any]) -> str | None:
    """Select the validated head SHA from an item's payload identity fields."""
    for key in ("_impl_source_revision", "reviewed_pr_head_sha"):
        candidate = payload.get(key)
        if isinstance(candidate, str) and _FULL_SHA_PATTERN.fullmatch(candidate) is not None:
            return candidate
    return None


def _validate_receipt_identity(
    identity: dict[str, Any],
) -> tuple[str, int | None, int | None, str | None]:
    """Validate the closed identity key set, fail closed."""
    if set(identity) != {"repo", "issue", "pr", "head_sha"}:
        raise ValueError("timing receipt identity must use the closed key set")
    repo = identity["repo"]
    issue = identity["issue"]
    pr = identity["pr"]
    head_sha = identity["head_sha"]
    if not isinstance(repo, str) or not repo or len(repo) > _REPO_LIMIT:
        raise ValueError("invalid repo")
    for number, name in ((issue, "issue"), (pr, "pr")):
        if number is not None and (
            isinstance(number, bool) or not isinstance(number, int) or number <= 0
        ):
            raise ValueError(f"invalid {name}")
    if head_sha is not None and (
        not isinstance(head_sha, str) or _FULL_SHA_PATTERN.fullmatch(head_sha) is None
    ):
        raise ValueError("invalid head_sha")
    return repo, issue, pr, head_sha


def _validate_final_stage(final_stage: str) -> str:
    """Validate the terminal stage label against the StageName vocabulary."""
    try:
        return StageName(_bounded_text(final_stage, name="final_stage")).value
    except ValueError:
        raise ValueError("invalid final_stage") from None


def _encode_receipt_phases(timing: WorkItemTiming, now: float) -> dict[str, Any]:
    """Encode the per-phase records with any open interval folded in."""
    phases: dict[str, Any] = {}
    pending_wait_s = (
        now - timing._wait_open_s
        if (not timing.closed and timing.current_waiting and timing._wait_open_s is not None)
        else 0.0
    )
    pending_interval_s = (
        now - timing._open_s if (not timing.closed and timing._open_s is not None) else 0.0
    )
    for phase in PhaseName:
        record = timing.phases.get(phase)
        if record is None:
            continue
        elapsed = record.elapsed_s
        wait = record.wait_s
        end = record.end_s
        if not timing.closed:
            if phase is timing._open_phase and pending_interval_s > 0:
                elapsed += pending_interval_s
                end = now if end is None else max(end, now)
            if timing.current_waiting and phase is timing._wait_phase and pending_wait_s > 0:
                elapsed += pending_wait_s
                wait += pending_wait_s
                end = now if end is None else max(end, now)
        active = max(0.0, elapsed - wait - record.suspended_s)
        phases[phase.value] = {
            "start_s": record.start_s,
            "end_s": end,
            "elapsed_s": round(elapsed, 6),
            "active_s": round(active, 6),
            "wait_s": round(wait, 6),
            "attempts": record.attempts,
            "class": "waiting" if phase in _WAITING_PHASES else "active",
        }
    return phases


def _receipt_wall_s(timing: WorkItemTiming, now: float) -> float:
    """Compute the bounded wall duration, folding in an open tail."""
    first = timing.first_marker_s
    last = timing.last_marker_s
    if not timing.closed and (timing._open_s is not None or timing._wait_open_s is not None):
        if first is None:
            first = now
        last = now if last is None else max(last, now)
    if first is None or last is None:
        return 0.0
    return max(0.0, last - first)


def encode_timing_receipt(
    identity: dict[str, Any],
    timing: WorkItemTiming,
    *,
    run_identity: str,
    passed: bool,
    final_stage: str,
    now_fn: Callable[[], float],
) -> dict[str, Any]:
    """Encode one closed-schema work-item timing receipt, fail closed.

    Raises:
        TypeError: On a non-boolean pass flag.
        ValueError: On any out-of-schema field, unbounded text, or invalid
            identity value.

    """
    repo, issue, pr, head_sha = _validate_receipt_identity(identity)
    _bounded_text(run_identity, name="run_identity")
    if not isinstance(passed, bool):
        raise TypeError("invalid passed")
    final_stage_value = _validate_final_stage(final_stage)
    now = _now(now_fn(), name="now")

    phases = _encode_receipt_phases(timing, now)
    wall = _receipt_wall_s(timing, now)
    active_total = sum(encoded["active_s"] for encoded in phases.values())
    wait_total = sum(encoded["wait_s"] for encoded in phases.values())

    current_phase = timing.current_phase.value if timing.current_phase is not None else None
    last_cause = timing.last_cause.value if timing.last_cause is not None else None
    for cause in timing.causes:
        RetryCause(cause)
    if timing.last_error_class is not None:
        _bounded_text(timing.last_error_class, name="error_class")
    return {
        "repo": repo,
        "issue": issue,
        "pr": pr,
        "head_sha": head_sha,
        "run_identity": run_identity,
        "wall_s": round(wall, 6),
        "active_s": round(active_total, 6),
        "wait_s": round(wait_total, 6),
        "phases": phases,
        "causes": dict(sorted(timing.causes.items())),
        "current_phase": current_phase,
        "current_waiting": bool(timing.current_waiting),
        "last_cause": last_cause,
        "last_error_class": timing.last_error_class,
        "passed": passed,
        "final_stage": final_stage_value,
    }
