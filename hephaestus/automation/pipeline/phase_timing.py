"""Bounded per-work-item phase timing and closed retry causes.

Pure data, zero I/O.  The coordinator thread is the single writer: it records
every phase interval, wait interval, job attribution, and retry cause on the
item's :class:`WorkItemTiming` accumulator.  Timing observes the workflow only;
no recorded value can route, gate, or fail a work item.

Three bounded lenses are kept per phase and never merged:

- ``elapsed_s`` — wall seconds from coordinator open/close intervals.
- ``active_s`` — job seconds attributed at completion time.
- ``wait_s``   — parked wait seconds.

A job's active seconds overlap the wall interval of the stage that ran it, so
per-phase elapsed values are a breakdown, not a partition of wall time.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .routing import StageName

#: Full source-revision shape accepted in a timing receipt (summary.py keeps
#: the same contract for its own head fields).
_FULL_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")

#: Maximum length of one bounded receipt string field.
_IDENTITY_TEXT_LIMIT = 256

#: Coordinator-side closed sentinel recorded on a failed test job whose result
#: carries no error text; classify_retry_cause maps it to TEST_FAILURE.
TEST_FAILURE_ERROR_CLASS = "test_failure"


class PhaseName(StrEnum):
    """The nine closed workflow phases the issue requires."""

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
    """The seven closed retry causes the issue requires."""

    SOURCE_CHANGE = "source_change"
    REVIEW_FINDINGS = "review_findings"
    TEST_FAILURE = "test_failure"
    CHECK_FAILURE = "check_failure"
    REMOTE_MOVEMENT = "remote_movement"
    TOOL_FAILURE = "tool_failure"
    EXTERNAL_WAIT = "external_wait"


#: Stage-to-phase map for interval recording.  LEARNING and FINISHED are
#: auxiliary stages outside the issue's nine phases; no phase records them.
PHASE_FOR_STAGE: dict[StageName, PhaseName] = {
    StageName.REPO: PhaseName.ISSUE_INTAKE,
    StageName.PLANNING: PhaseName.PLANNING,
    StageName.PLAN_REVIEW: PhaseName.PLANNING,
    StageName.IMPLEMENTATION: PhaseName.IMPLEMENTATION,
    StageName.PR_REVIEW: PhaseName.IMPLEMENTATION,
    StageName.MERGE_WAIT: PhaseName.MERGE_ADMISSION,
}

#: Closed GitJob op subset whose completed active seconds are publication work.
PUBLICATION_OPS: frozenset[str] = frozenset(
    {
        "commit_push",
        "publish_dirty_direct_continuation",
        "finish_dirty_direct_publication",
    }
)

#: Closed wait-cause keywords accepted by the coordinator's park hook.
WAIT_CAUSE_STAGE = "stage"
WAIT_CAUSE_RATE_GUARD = "rate_guard"

_WAIT_PHASES: dict[str, PhaseName] = {
    WAIT_CAUSE_RATE_GUARD: PhaseName.EXTERNAL_WAIT,
}

#: Closed note-to-cause map.  Keys are fail-route or retry notes from ROUTES
#: and the stage modules; unknown notes never reach the receipt as free text.
_NOTE_CAUSES: dict[str, RetryCause] = {
    "plan_changed": RetryCause.SOURCE_CHANGE,
    "empty_pr_diff": RetryCause.SOURCE_CHANGE,
    "nogo": RetryCause.REVIEW_FINDINGS,
    "plan_missing": RetryCause.REVIEW_FINDINGS,
    "plan_not_go": RetryCause.REVIEW_FINDINGS,
    "implementation_remediation": RetryCause.REVIEW_FINDINGS,
    "scope_retraction_before_scope_block": RetryCause.REVIEW_FINDINGS,
    "merge_blocked_by_unresolved_threads": RetryCause.REVIEW_FINDINGS,
    # The approval label is the merge contract; a missing GO label means the
    # review state withdrew, not that a remote head moved.
    "not_implementation_go": RetryCause.REVIEW_FINDINGS,
    "merge_conflicting": RetryCause.REMOTE_MOVEMENT,
    "already_implementation_go_pr": RetryCause.REMOTE_MOVEMENT,
    "review_retained_after_rebase": RetryCause.REMOTE_MOVEMENT,
    "manual_rebase_complete_planning": RetryCause.REMOTE_MOVEMENT,
    "manual_rebase_complete_plan_review": RetryCause.REMOTE_MOVEMENT,
    "merge_blocked_by_failing_checks": RetryCause.CHECK_FAILURE,
    "agent_error": RetryCause.TOOL_FAILURE,
}

#: Closed job-error-class-to-cause map.  Every class that reaches this map
#: names one cause; the caller maps unlisted classes to TOOL_FAILURE.
_ERROR_CLASS_CAUSES: dict[str, RetryCause] = {
    TEST_FAILURE_ERROR_CLASS: RetryCause.TEST_FAILURE,
    "github_rate_limit": RetryCause.EXTERNAL_WAIT,
    "github_unavailable": RetryCause.EXTERNAL_WAIT,
    "lock_timeout": RetryCause.EXTERNAL_WAIT,
    "lock_metadata_error": RetryCause.EXTERNAL_WAIT,
    "publish_remote_head_changed": RetryCause.REMOTE_MOVEMENT,
    "publish_remote_head_unchanged": RetryCause.REMOTE_MOVEMENT,
    "publish_remote_probe_failed": RetryCause.REMOTE_MOVEMENT,
    "publish_lease_drift": RetryCause.REMOTE_MOVEMENT,
}

#: Per-stage fallback for a correction attempt whose note and error class map
#: to nothing.  Keeps the cause vocabulary closed without inventing values.
_STAGE_DEFAULT_CAUSES: dict[StageName, RetryCause] = {
    StageName.PLAN_REVIEW: RetryCause.REVIEW_FINDINGS,
    StageName.MERGE_WAIT: RetryCause.CHECK_FAILURE,
}

_TOOL_FAILURE = RetryCause.TOOL_FAILURE


def _bounded_delta(value: object, *, name: str) -> float:
    """Return one finite nonnegative duration increment."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"invalid {name}")
    return float(value)


def _bounded_instant(value: object, *, name: str) -> float | None:
    """Return one finite nonnegative clock instant or None."""
    if value is None:
        return None
    return _bounded_delta(value, name=name)


def classify_error_cause(error_class: str) -> RetryCause:
    """Map one closed job error class to its retry cause.

    Unlisted classes fall back to TOOL_FAILURE, matching the issue's generic
    tool-failure bucket for timeouts, worker crashes, and unknown errors.
    """
    return _ERROR_CLASS_CAUSES.get(error_class, _TOOL_FAILURE)


def classify_retry_cause(
    stage: StageName, note: str, error_class: str | None = None
) -> RetryCause | None:
    """Classify one retry or fail-back into the closed cause vocabulary.

    The fail-route note is the primary input; the job error class is the
    fallback.  Returns ``None`` when no closed input maps: a poll-style wait
    has no retry cause because nothing failed to cause it.  ``stage`` is part
    of the signature so future per-stage rules stay call-site compatible; the
    current closed map needs no stage branch.
    """
    if note and note in _NOTE_CAUSES:
        return _NOTE_CAUSES[note]
    if error_class is not None:
        return classify_error_cause(error_class)
    return None


def default_retry_cause(stage: StageName) -> RetryCause:
    """Return the stage's closed fallback cause for an unmapped correction."""
    return _STAGE_DEFAULT_CAUSES.get(stage, _TOOL_FAILURE)


def classify_wait_phase(stage: StageName, wait_cause: str) -> PhaseName | None:
    """Return the phase that owns one timer park, or None when unmapped."""
    phase = _WAIT_PHASES.get(wait_cause)
    if phase is not None:
        return phase
    if stage is StageName.MERGE_WAIT:
        return PhaseName.CI_WAIT
    return PHASE_FOR_STAGE.get(stage)


def classify_job_phase(job_type: str, git_op: str | None, stage: StageName) -> PhaseName | None:
    """Map one completed job to the phase that owns its active seconds.

    ``job_type`` is the closed ``type(job).__name__`` value the coordinator
    already records; ``git_op`` is the GitJob op when applicable.
    """
    if job_type == "BuildTestJob":
        return PhaseName.TEST_EXECUTION
    if job_type == "GitJob" and git_op in PUBLICATION_OPS:
        return PhaseName.PUBLICATION
    return PHASE_FOR_STAGE.get(stage)


@dataclass(slots=True)
class PhaseRecord:
    """Bounded timing facts for one phase of one work item."""

    start_s: float | None = None
    end_s: float | None = None
    active_s: float = 0.0
    wait_s: float = 0.0
    elapsed_s: float = 0.0
    attempts: int = 0

    def observe(self, now_s: object, *, closed: bool) -> None:
        """Record one interval boundary for this phase."""
        instant = _bounded_instant(now_s, name="phase instant")
        if instant is None:
            return
        if self.start_s is None or instant < self.start_s:
            self.start_s = instant
        if closed and (self.end_s is None or instant > self.end_s):
            self.end_s = instant

    def add_active(self, delta_s: object) -> None:
        """Accumulate one validated job-attribution increment."""
        value = _bounded_delta(delta_s, name="active_s")
        self.active_s += value
        self.elapsed_s += value

    def add_wait(self, delta_s: object) -> None:
        """Accumulate one validated parked-wait increment."""
        value = _bounded_delta(delta_s, name="wait_s")
        self.wait_s += value
        self.elapsed_s += value

    def add_elapsed(self, delta_s: object) -> None:
        """Accumulate one validated open-interval increment."""
        self.elapsed_s += _bounded_delta(delta_s, name="elapsed_s")

    def count_attempt(self) -> None:
        """Count one retry or correction attempt for this phase."""
        self.attempts += 1


@dataclass(slots=True)
class WorkItemTiming:
    """Coordinator-owned phase timing accumulator for one work item.

    All operations are idempotent where the workflow can repeat them: closing
    a closed interval, closing an already-closed accumulator, and re-entering
    the phase that is already open are all no-ops.
    """

    phases: dict[PhaseName, PhaseRecord] = field(default_factory=dict)
    causes: Counter[str] = field(default_factory=Counter)
    current_phase: PhaseName | None = None
    current_waiting: bool = False
    last_cause: RetryCause | None = None
    last_error_class: str | None = None
    wall_start_s: float | None = None
    wall_end_s: float | None = None
    _open_since_s: float | None = None
    _wait_open_since_s: float | None = None
    _wait_phase: PhaseName | None = None

    def _record(self, phase: PhaseName) -> PhaseRecord:
        record = self.phases.get(phase)
        if record is None:
            record = PhaseRecord()
            self.phases[phase] = record
        return record

    def _close_open_interval(self, now_s: float) -> None:
        if self._open_since_s is None or self.current_phase is None:
            self._open_since_s = None
            return
        phase = self.current_phase
        record = self._record(phase)
        record.add_elapsed(max(0.0, now_s - self._open_since_s))
        record.observe(now_s, closed=True)
        self._open_since_s = None

    def enter_phase(self, phase: PhaseName, now_s: object) -> None:
        """Open the active interval for *phase*, closing any open interval."""
        instant = _bounded_instant(now_s, name="enter instant")
        if instant is None:
            raise ValueError("invalid enter instant")
        if self.wall_start_s is None:
            self.wall_start_s = instant
        if self.current_phase is phase and self._open_since_s is not None:
            return
        self._close_open_interval(instant)
        self.current_phase = phase
        self._open_since_s = instant
        self._record(phase).observe(instant, closed=False)

    def leave_phase(self, phase: PhaseName, now_s: object) -> None:
        """Close the open interval of *phase*; a different or closed phase is a no-op."""
        instant = _bounded_instant(now_s, name="leave instant")
        if instant is None:
            raise ValueError("invalid leave instant")
        if self.current_phase is not phase or self._open_since_s is None:
            return
        self._close_open_interval(instant)

    def begin_wait(self, phase: PhaseName, now_s: object) -> None:
        """Close the open active interval and open one parked-wait interval."""
        instant = _bounded_instant(now_s, name="wait start instant")
        if instant is None:
            raise ValueError("invalid wait start instant")
        if self.current_waiting:
            return
        self._close_open_interval(instant)
        self.current_waiting = True
        self._wait_phase = phase
        self._wait_open_since_s = instant
        self._record(phase).observe(instant, closed=False)

    def end_wait(self, now_s: object) -> None:
        """Close the open parked-wait interval; no open wait is a no-op."""
        instant = _bounded_instant(now_s, name="wait end instant")
        if instant is None:
            raise ValueError("invalid wait end instant")
        if not self.current_waiting or self._wait_open_since_s is None:
            return
        phase = self._wait_phase
        opened = self._wait_open_since_s
        self.current_waiting = False
        self._wait_phase = None
        self._wait_open_since_s = None
        if phase is None:  # pragma: no cover - begin_wait always sets the phase
            return
        record = self._record(phase)
        record.add_wait(max(0.0, instant - opened))
        record.observe(instant, closed=True)

    def record_job(
        self,
        phase: PhaseName | None,
        duration_s: object,
        error_class: str | None,
    ) -> None:
        """Attribute one completed job's bounded active seconds to *phase*."""
        delta = _bounded_delta(duration_s, name="job duration")
        if phase is not None:
            self._record(phase).add_active(delta)
        if error_class is not None:
            self.last_error_class = error_class

    def record_attempt(self, phase: PhaseName, cause: RetryCause) -> None:
        """Count one retry or correction attempt and its closed cause."""
        self._record(phase).count_attempt()
        self.causes[cause.value] += 1
        self.last_cause = cause

    def close(self, final_wall_s: object) -> None:
        """Close every open interval once; a closed accumulator is a no-op."""
        instant = _bounded_instant(final_wall_s, name="close instant")
        if instant is None:
            raise ValueError("invalid close instant")
        if self.wall_end_s is not None:
            return
        if self.current_waiting and self._wait_open_since_s is not None:
            phase = self._wait_phase
            opened = self._wait_open_since_s
            self.current_waiting = False
            self._wait_phase = None
            self._wait_open_since_s = None
            if phase is not None:
                record = self._record(phase)
                record.add_wait(max(0.0, instant - opened))
                record.observe(instant, closed=True)
        self._close_open_interval(instant)
        self.wall_end_s = instant

    def active_total_s(self) -> float:
        """Return the summed job-attribution seconds across phases."""
        return math.fsum(record.active_s for record in self.phases.values())

    def wait_total_s(self) -> float:
        """Return the summed parked-wait seconds across phases."""
        return math.fsum(record.wait_s for record in self.phases.values())

    def wall_total_s(self) -> float | None:
        """Return the observed wall seconds, or None before the first entry."""
        if self.wall_start_s is None or self.wall_end_s is None:
            return None
        return max(0.0, self.wall_end_s - self.wall_start_s)


def encode_timing_receipt(
    identity: Mapping[str, Any],
    timing: WorkItemTiming,
    *,
    run_identity: str | None,
    passed: bool,
    final_stage: str,
) -> dict[str, Any]:
    """Encode one fail-closed, closed-schema timing receipt.

    Raises ``ValueError`` or ``TypeError`` on any out-of-schema field so a
    malformed observation can never enter the durable event log.
    """
    if not isinstance(identity, Mapping):
        raise TypeError("invalid identity")
    expected_identity_keys = {"repo", "issue", "pr", "head_sha"}
    if set(identity) != expected_identity_keys:
        raise ValueError("invalid identity keys")

    repo = _receipt_text(identity["repo"], name="repo")
    issue = _receipt_optional_index(identity["issue"], name="issue")
    pr = _receipt_optional_index(identity["pr"], name="pr")
    head_sha = identity["head_sha"]
    if head_sha is not None and (
        not isinstance(head_sha, str) or _FULL_SHA_PATTERN.fullmatch(head_sha) is None
    ):
        raise ValueError("invalid head_sha")
    if run_identity is not None:
        _receipt_text(run_identity, name="run_identity")
    if not isinstance(final_stage, str) or not final_stage:
        raise ValueError("invalid final_stage")

    wall_total = timing.wall_total_s()
    phases: dict[str, dict[str, Any]] = {}
    for phase, record in timing.phases.items():
        if not isinstance(phase, PhaseName):
            raise ValueError("invalid phase name")
        phases[phase.value] = {
            "start_s": _bounded_instant(record.start_s, name="phase start_s"),
            "end_s": _bounded_instant(record.end_s, name="phase end_s"),
            "elapsed_s": _bounded_delta(record.elapsed_s, name="phase elapsed_s"),
            "active_s": _bounded_delta(record.active_s, name="phase active_s"),
            "wait_s": _bounded_delta(record.wait_s, name="phase wait_s"),
            "attempts": _receipt_count(record.attempts, name="attempts"),
        }
    causes: dict[str, int] = {}
    for cause, count in timing.causes.items():
        if not isinstance(cause, str) or cause not in RetryCause._value2member_map_:
            raise ValueError("invalid cause value")
        causes[cause] = _receipt_count(count, name="cause count")

    return {
        "repo": repo,
        "issue": issue,
        "pr": pr,
        "head_sha": head_sha,
        "run_identity": run_identity,
        "passed": bool(passed),
        "final_stage": _receipt_text(final_stage, name="final_stage"),
        "wall_s": _bounded_instant(wall_total, name="wall_s"),
        "active_s": _bounded_delta(timing.active_total_s(), name="active_s"),
        "wait_s": _bounded_delta(timing.wait_total_s(), name="wait_s"),
        "phases": phases,
        "causes": causes,
        "current_phase": (timing.current_phase.value if timing.current_phase is not None else None),
        "last_cause": timing.last_cause.value if timing.last_cause is not None else None,
    }


def _receipt_text(value: object, *, name: str) -> str:
    """Return one bounded receipt string without control characters."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _IDENTITY_TEXT_LIMIT
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"invalid {name}")
    return value


def _receipt_optional_index(value: object, *, name: str) -> int | None:
    """Return one optional nonnegative integer identity field."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid {name}")
    return value


def _receipt_count(value: object, *, name: str) -> int:
    """Return one nonnegative integer count."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid {name}")
    return value
