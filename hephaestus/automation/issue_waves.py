"""Durable, repository-scoped checkpoints for staged issue waves.

The queue coordinator deliberately keeps its work items in memory.  A staged
rollout needs one small exception: the exact issue identities and the
loop-owned merge receipts must survive a process restart.  This module owns
that exception.  It contains no GitHub or Git subprocesses; callers provide
fresh facts and an ancestry result from the existing worker boundary.
"""

from __future__ import annotations

import json
import stat
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.seeding import SeedEntry
from hephaestus.io.utils import write_secure
from hephaestus.utils.file_lock import LockUnavailableError, file_lock

WAVE_LIMITS: tuple[int | None, ...] = (1, 2, 4, 8, None)
_SCHEMA = "hephaestus.issue-wave-checkpoint.v1"
_CHECKPOINT_NAME = "issue-wave-checkpoint.json"
_LOCK_NAME = "issue-wave-checkpoint.lock"
_SHA_LENGTHS = (40, 64)

WAVE_LEASE_PAYLOAD = "_issue_wave_lease"


class IssueWaveError(RuntimeError):
    """Base class for fail-closed issue-wave errors."""


class IssueWaveValidationError(IssueWaveError):
    """Raised when checkpoint data or a selector is malformed."""


class IssueWaveConflictError(IssueWaveError):
    """Raised when another process changed or holds the checkpoint."""


class IssueWaveBlockedError(IssueWaveError):
    """Raised when a prior wave is not eligible for advancement."""


class IssueWaveRepositoryError(IssueWaveError):
    """Raised when checkpoint paths are not confined to a repository."""


def is_full_commit_sha(value: object) -> bool:
    """Return whether *value* is a lowercase full Git commit identifier."""
    return bool(
        isinstance(value, str)
        and len(value) in _SHA_LENGTHS
        and all(character in "0123456789abcdef" for character in value)
    )


def _positive_issue_numbers(values: Any, field_name: str) -> tuple[int, ...]:
    """Validate and preserve an ordered, duplicate-free issue sequence."""
    if not isinstance(values, (list, tuple)):
        raise IssueWaveValidationError(f"{field_name} must be a list of issue numbers")
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise IssueWaveValidationError(f"{field_name} contains an invalid issue number")
        if value in seen:
            raise IssueWaveValidationError(f"{field_name} contains duplicate issue #{value}")
        seen.add(value)
        result.append(value)
    return tuple(result)


def _limit(value: Any, field_name: str = "limit") -> int | None:
    """Validate a wave limit without accepting arbitrary phase values."""
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
        raise IssueWaveValidationError(f"{field_name} must be a positive integer or null")
    if value not in WAVE_LIMITS:
        allowed = ", ".join("all" if item is None else str(item) for item in WAVE_LIMITS)
        raise IssueWaveValidationError(f"{field_name} must be one of {allowed}")
    return value


def _required_text(value: Any, field_name: str) -> str:
    """Validate a non-empty text field."""
    if not isinstance(value, str) or not value:
        raise IssueWaveValidationError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class WaveLease:
    """Immutable identity of one checkpointed wave."""

    org: str
    repo: str
    wave_index: int
    limit: int | None
    issue_numbers: tuple[int, ...]
    base_main_sha: str
    nonce: str

    def __post_init__(self) -> None:
        """Validate the immutable lease identity."""
        if not self.org or not self.repo:
            raise IssueWaveValidationError(
                "wave lease must identify an organization and repository"
            )
        if isinstance(self.wave_index, bool) or self.wave_index < 0:
            raise IssueWaveValidationError("wave index must be non-negative")
        _limit(self.limit)
        _positive_issue_numbers(self.issue_numbers, "wave issue_numbers")
        if not is_full_commit_sha(self.base_main_sha):
            raise IssueWaveValidationError("wave base_main_sha is not a full commit SHA")
        _required_text(self.nonce, "wave nonce")

    @property
    def phase(self) -> int:
        """Compatibility alias for callers that call a wave a phase."""
        return self.wave_index

    @property
    def is_final(self) -> bool:
        """Return whether this is the all-eligible final wave."""
        return self.limit is None


@dataclass(frozen=True)
class WaveMergeReceipt:
    """Loop-owned proof that one issue PR was normally merged."""

    issue_number: int
    pr_number: int
    reviewed_head_sha: str
    merge_sha: str

    def __post_init__(self) -> None:
        """Validate the merge proof fields."""
        if isinstance(self.issue_number, bool) or self.issue_number <= 0:
            raise IssueWaveValidationError("merge receipt issue_number must be positive")
        if isinstance(self.pr_number, bool) or self.pr_number <= 0:
            raise IssueWaveValidationError("merge receipt pr_number must be positive")
        if not is_full_commit_sha(self.reviewed_head_sha):
            raise IssueWaveValidationError("merge receipt reviewed_head_sha is invalid")
        if not is_full_commit_sha(self.merge_sha):
            raise IssueWaveValidationError("merge receipt merge_sha is invalid")


@dataclass(frozen=True)
class WaveIssueOutcome:
    """Durable terminal result for one selected issue."""

    issue_number: int
    passed: bool
    reason: str
    pr_number: int | None = None

    def __post_init__(self) -> None:
        """Validate the terminal issue result."""
        if isinstance(self.issue_number, bool) or self.issue_number <= 0:
            raise IssueWaveValidationError("wave outcome issue_number must be positive")
        if not isinstance(self.passed, bool):
            raise IssueWaveValidationError("wave outcome passed must be boolean")
        if not isinstance(self.reason, str) or not self.reason:
            raise IssueWaveValidationError("wave outcome reason must be non-empty")
        if self.pr_number is not None and (
            isinstance(self.pr_number, bool)
            or not isinstance(self.pr_number, int)
            or self.pr_number <= 0
        ):
            raise IssueWaveValidationError("wave outcome pr_number must be positive or null")


@dataclass(frozen=True)
class WaveRecord:
    """One immutable selection plus its progressively recorded evidence."""

    wave_index: int
    limit: int | None
    issue_numbers: tuple[int, ...]
    base_main_sha: str
    lease_nonce: str
    outcomes: tuple[WaveIssueOutcome, ...] = ()
    merge_receipts: tuple[WaveMergeReceipt, ...] = ()
    verified_main_sha: str | None = None

    def __post_init__(self) -> None:
        """Validate wave selection and evidence invariants."""
        if isinstance(self.wave_index, bool) or self.wave_index < 0:
            raise IssueWaveValidationError("wave record index must be non-negative")
        _limit(self.limit)
        _positive_issue_numbers(self.issue_numbers, "wave record issue_numbers")
        if self.limit is not None and len(self.issue_numbers) > self.limit:
            raise IssueWaveValidationError("wave record exceeds its issue limit")
        if not is_full_commit_sha(self.base_main_sha):
            raise IssueWaveValidationError("wave record base_main_sha is invalid")
        _required_text(self.lease_nonce, "wave record lease_nonce")
        outcome_ids = [outcome.issue_number for outcome in self.outcomes]
        receipt_ids = [receipt.issue_number for receipt in self.merge_receipts]
        if len(set(outcome_ids)) != len(outcome_ids) or not set(outcome_ids).issubset(
            set(self.issue_numbers)
        ):
            raise IssueWaveValidationError("wave outcomes do not match the immutable selection")
        if len(set(receipt_ids)) != len(receipt_ids) or not set(receipt_ids).issubset(
            set(self.issue_numbers)
        ):
            raise IssueWaveValidationError("wave receipts do not match the immutable selection")
        if self.verified_main_sha is not None and not is_full_commit_sha(self.verified_main_sha):
            raise IssueWaveValidationError("wave verified_main_sha is invalid")

    @property
    def complete(self) -> bool:
        """Return whether every selected issue has a terminal outcome."""
        return {outcome.issue_number for outcome in self.outcomes} == set(self.issue_numbers)

    @property
    def passed(self) -> bool:
        """Return whether all recorded terminal outcomes passed."""
        return self.complete and all(outcome.passed for outcome in self.outcomes)

    def lease(self, org: str, repo: str) -> WaveLease:
        """Return the runtime lease represented by this record."""
        return WaveLease(
            org=org,
            repo=repo,
            wave_index=self.wave_index,
            limit=self.limit,
            issue_numbers=self.issue_numbers,
            base_main_sha=self.base_main_sha,
            nonce=self.lease_nonce,
        )


@dataclass(frozen=True)
class WaveCheckpoint:
    """Validated repository-scoped checkpoint document."""

    org: str
    repo: str
    generation: int
    status: Literal["active", "completed"]
    waves: tuple[WaveRecord, ...]
    completed_main_sha: str | None = None

    def __post_init__(self) -> None:
        """Validate checkpoint identity, phase order, and completion state."""
        if not self.org or not self.repo:
            raise IssueWaveValidationError(
                "checkpoint must identify an organization and repository"
            )
        if isinstance(self.generation, bool) or self.generation < 1:
            raise IssueWaveValidationError("checkpoint generation must be positive")
        if self.status not in {"active", "completed"}:
            raise IssueWaveValidationError("checkpoint status is invalid")
        if not self.waves:
            raise IssueWaveValidationError("checkpoint must contain at least one wave")
        expected = list(range(len(self.waves)))
        if [wave.wave_index for wave in self.waves] != expected:
            raise IssueWaveValidationError("checkpoint wave indexes are not contiguous")
        if self.status == "completed" and self.completed_main_sha is None:
            raise IssueWaveValidationError("completed checkpoint requires completed_main_sha")
        if self.completed_main_sha is not None and not is_full_commit_sha(self.completed_main_sha):
            raise IssueWaveValidationError("checkpoint completed_main_sha is invalid")

    @property
    def current_wave(self) -> WaveRecord:
        """Return the latest wave record."""
        return self.waves[-1]


@dataclass(frozen=True)
class WaveAdmissionPlan:
    """Read-only result of checkpoint admission before queue mutation."""

    mode: Literal["ordinary", "resume", "select", "audit"]
    requested_limit: int | None
    current_main_sha: str
    expected_generation: int | None = None
    wave_index: int | None = None
    lease: WaveLease | None = None
    requires_ancestry: bool = False
    ancestor_shas: tuple[str, ...] = ()
    checkpoint: WaveCheckpoint | None = None
    diagnostic: str = ""

    @property
    def wave_mode(self) -> bool:
        """Return whether this plan uses a durable or ephemeral wave source."""
        return self.mode != "ordinary"


def _outcome_to_json(outcome: WaveIssueOutcome) -> dict[str, Any]:
    return {
        "issue_number": outcome.issue_number,
        "passed": outcome.passed,
        "reason": outcome.reason,
        "pr_number": outcome.pr_number,
    }


def _receipt_to_json(receipt: WaveMergeReceipt) -> dict[str, Any]:
    return {
        "issue_number": receipt.issue_number,
        "pr_number": receipt.pr_number,
        "reviewed_head_sha": receipt.reviewed_head_sha,
        "merge_sha": receipt.merge_sha,
    }


def _wave_to_json(wave: WaveRecord) -> dict[str, Any]:
    return {
        "wave_index": wave.wave_index,
        "limit": wave.limit,
        "issue_numbers": list(wave.issue_numbers),
        "base_main_sha": wave.base_main_sha,
        "lease_nonce": wave.lease_nonce,
        "outcomes": [_outcome_to_json(item) for item in wave.outcomes],
        "merge_receipts": [_receipt_to_json(item) for item in wave.merge_receipts],
        "verified_main_sha": wave.verified_main_sha,
    }


def _checkpoint_to_json(checkpoint: WaveCheckpoint) -> dict[str, Any]:
    return {
        "schema": _SCHEMA,
        "org": checkpoint.org,
        "repo": checkpoint.repo,
        "generation": checkpoint.generation,
        "status": checkpoint.status,
        "waves": [_wave_to_json(wave) for wave in checkpoint.waves],
        "completed_main_sha": checkpoint.completed_main_sha,
    }


def _decode_checkpoint(raw: Any) -> WaveCheckpoint:
    """Decode the strict on-disk schema without silently repairing it."""
    if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA:
        raise IssueWaveValidationError("issue-wave checkpoint schema is missing or unsupported")
    waves_raw = raw.get("waves")
    if not isinstance(waves_raw, list):
        raise IssueWaveValidationError("checkpoint waves must be a list")
    waves: list[WaveRecord] = []
    for value in waves_raw:
        if not isinstance(value, dict):
            raise IssueWaveValidationError("checkpoint wave must be an object")
        outcomes_raw = value.get("outcomes", [])
        receipts_raw = value.get("merge_receipts", [])
        if not isinstance(outcomes_raw, list) or not isinstance(receipts_raw, list):
            raise IssueWaveValidationError("checkpoint outcomes and receipts must be lists")
        outcomes = tuple(
            WaveIssueOutcome(
                issue_number=cast(int, item.get("issue_number")),
                passed=cast(bool, item.get("passed")),
                reason=cast(str, item.get("reason")),
                pr_number=cast(int | None, item.get("pr_number")),
            )
            for item in outcomes_raw
            if isinstance(item, dict)
        )
        if len(outcomes) != len(outcomes_raw):
            raise IssueWaveValidationError("checkpoint outcome must be an object")
        receipts = tuple(
            WaveMergeReceipt(
                issue_number=cast(int, item.get("issue_number")),
                pr_number=cast(int, item.get("pr_number")),
                reviewed_head_sha=cast(str, item.get("reviewed_head_sha")),
                merge_sha=cast(str, item.get("merge_sha")),
            )
            for item in receipts_raw
            if isinstance(item, dict)
        )
        if len(receipts) != len(receipts_raw):
            raise IssueWaveValidationError("checkpoint merge receipt must be an object")
        waves.append(
            WaveRecord(
                wave_index=cast(int, value.get("wave_index")),
                limit=value.get("limit"),
                issue_numbers=tuple(value.get("issue_numbers", ())),
                base_main_sha=cast(str, value.get("base_main_sha")),
                lease_nonce=cast(str, value.get("lease_nonce")),
                outcomes=outcomes,
                merge_receipts=receipts,
                verified_main_sha=value.get("verified_main_sha"),
            )
        )
    return WaveCheckpoint(
        org=cast(str, raw.get("org")),
        repo=cast(str, raw.get("repo")),
        generation=cast(int, raw.get("generation")),
        status=cast(Literal["active", "completed"], raw.get("status")),
        waves=tuple(waves),
        completed_main_sha=raw.get("completed_main_sha"),
    )


class IssueWaveStore:
    """Strict, atomic store for one repository's staged issue rollout."""

    def __init__(self, repo_root: Path, org: str, repo: str) -> None:
        """Bind the store to one existing repository checkout."""
        self.repo_root = Path(repo_root)
        self.org = _required_text(org, "organization")
        self.repo = _required_text(repo, "repository")
        self._validate_paths()

    @property
    def state_dir(self) -> Path:
        """Return the repository-confined state directory."""
        return self.repo_root / DEFAULT_STATE_DIR

    @property
    def checkpoint_path(self) -> Path:
        """Return the durable checkpoint path."""
        return self.state_dir / _CHECKPOINT_NAME

    @property
    def lock_path(self) -> Path:
        """Return the stable sibling lock path."""
        return self.state_dir / _LOCK_NAME

    @property
    def path(self) -> Path:
        """Compatibility alias for :attr:`checkpoint_path`."""
        return self.checkpoint_path

    def _validate_paths(self) -> None:
        """Reject symlink escapes and non-directory repository roots."""
        if self.repo_root.is_symlink() or not self.repo_root.is_dir():
            raise IssueWaveRepositoryError(
                f"issue-wave repository root is not a real directory: {self.repo_root}"
            )
        root = self.repo_root.resolve(strict=True)
        for candidate in (
            self.repo_root / "build",
            self.state_dir,
            self.checkpoint_path,
            self.lock_path,
        ):
            if candidate.is_symlink():
                raise IssueWaveRepositoryError(f"refusing symlinked issue-wave path: {candidate}")
            try:
                resolved = candidate.resolve(strict=False)
            except OSError as exc:
                raise IssueWaveRepositoryError(
                    f"cannot resolve issue-wave path: {candidate}"
                ) from exc
            if not resolved.is_relative_to(root):
                raise IssueWaveRepositoryError(f"issue-wave path escapes repository: {candidate}")
        for candidate in (self.checkpoint_path, self.lock_path):
            if candidate.exists() and not candidate.is_file():
                raise IssueWaveRepositoryError(
                    f"issue-wave path is not a regular file: {candidate}"
                )

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Acquire the stable exclusive lock after rechecking confinement."""
        self._validate_paths()
        try:
            with file_lock(self.lock_path, blocking=False, require_exclusive=True):
                self._validate_paths()
                yield
        except LockUnavailableError as exc:
            raise IssueWaveConflictError(
                f"issue-wave checkpoint is busy for {self.org}/{self.repo}; retry later"
            ) from exc
        except IssueWaveError:
            raise
        except (OSError, RuntimeError) as exc:
            raise IssueWaveRepositoryError(f"cannot lock issue-wave checkpoint: {exc}") from exc

    def _read_unlocked(self) -> WaveCheckpoint | None:
        self._validate_paths()
        path = self.checkpoint_path
        if not path.exists():
            return None
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise IssueWaveValidationError(f"checkpoint permissions are too broad: {path}")
        try:
            with path.open(encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise IssueWaveValidationError(
                f"cannot read issue-wave checkpoint {path}: {exc}"
            ) from exc
        checkpoint = _decode_checkpoint(raw)
        if checkpoint.org != self.org or checkpoint.repo != self.repo:
            raise IssueWaveValidationError(
                "issue-wave checkpoint repository binding does not match the current repository"
            )
        return checkpoint

    def _write_unlocked(self, checkpoint: WaveCheckpoint) -> None:
        """Atomically write a validated checkpoint with owner-only permissions."""
        self._validate_paths()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._validate_paths()
        write_secure(
            self.checkpoint_path,
            json.dumps(_checkpoint_to_json(checkpoint), indent=2, sort_keys=True) + "\n",
            permissions=0o600,
        )

    def load(self) -> WaveCheckpoint | None:
        """Strictly load the checkpoint, returning ``None`` when absent."""
        with self._locked():
            return self._read_unlocked()

    strict_load = load

    @staticmethod
    def _current_record(checkpoint: WaveCheckpoint, lease: WaveLease) -> WaveRecord:
        """Find a lease's record and reject cross-repository or stale leases."""
        if lease.org != checkpoint.org or lease.repo != checkpoint.repo:
            raise IssueWaveValidationError(
                "wave lease repository binding does not match checkpoint"
            )
        if lease.wave_index >= len(checkpoint.waves):
            raise IssueWaveConflictError("wave lease is no longer present in the checkpoint")
        record = checkpoint.waves[lease.wave_index]
        if record.lease_nonce != lease.nonce or record.issue_numbers != lease.issue_numbers:
            raise IssueWaveConflictError("wave lease selection is stale or was externally changed")
        return record

    @staticmethod
    def _next_limit(current: int | None) -> int | None:
        """Return the selector required after a successful wave."""
        index = WAVE_LIMITS.index(current)
        return WAVE_LIMITS[index + 1] if index + 1 < len(WAVE_LIMITS) else None

    def _ancestry_shas(self, record: WaveRecord) -> tuple[str, ...]:
        """Return the base and every recorded merge commit for Git checking."""
        return (record.base_main_sha, *(receipt.merge_sha for receipt in record.merge_receipts))

    def _plan_locked(  # noqa: C901
        self, current_main_sha: str, requested_limit: int | None
    ) -> WaveAdmissionPlan:
        if not is_full_commit_sha(current_main_sha):
            raise IssueWaveValidationError("synchronized main revision is not a full commit SHA")
        _limit(requested_limit, "issue-limit")
        checkpoint = self._read_unlocked()
        if checkpoint is None:
            if requested_limit is None:
                return WaveAdmissionPlan("ordinary", None, current_main_sha)
            if requested_limit != WAVE_LIMITS[0]:
                raise IssueWaveBlockedError(
                    "no issue-wave checkpoint exists; rollout must start with "
                    f"--issue-limit {WAVE_LIMITS[0]}"
                )
            return WaveAdmissionPlan(
                "select",
                requested_limit,
                current_main_sha,
                expected_generation=0,
                wave_index=0,
            )

        current = checkpoint.current_wave
        lease = current.lease(self.org, self.repo)
        if checkpoint.status == "completed":
            if requested_limit is not None:
                raise IssueWaveBlockedError(
                    "issue-wave rollout already completed; bounded selectors are rejected; "
                    "use explicit --issues/--prs recovery if needed"
                )
            if checkpoint.completed_main_sha != current_main_sha:
                raise IssueWaveBlockedError(
                    "completed issue-wave rollout is bound to a different "
                    "synchronized main revision"
                )
            return WaveAdmissionPlan(
                "audit",
                None,
                current_main_sha,
                expected_generation=checkpoint.generation,
                wave_index=current.wave_index,
                lease=lease,
                checkpoint=checkpoint,
                diagnostic="issue-wave rollout is completed; audit-only run",
            )

        if requested_limit == current.limit:
            main_advanced = current_main_sha != current.base_main_sha
            partial_receipt_resume = bool(current.merge_receipts) and not current.complete
            if main_advanced and not current.merge_receipts and not current.complete:
                raise IssueWaveBlockedError(
                    "active issue wave advanced without a recorded loop-owned merge receipt"
                )
            # A partial receipt is authorization to retry the sealed remainder,
            # but only after the host revalidates both Git ancestry and fresh
            # GitHub merge facts.  Revalidate even when a stale local main still
            # equals the wave base; the receipt's merge SHA must be on main.
            resume_requires_ancestry = main_advanced or partial_receipt_resume
            if current.complete and current.passed and current.limit is None:
                return WaveAdmissionPlan(
                    "audit",
                    None,
                    current_main_sha,
                    expected_generation=checkpoint.generation,
                    wave_index=current.wave_index,
                    lease=lease,
                    requires_ancestry=current.verified_main_sha != current_main_sha,
                    ancestor_shas=self._ancestry_shas(current),
                    checkpoint=checkpoint,
                    diagnostic="final wave is complete; verify and close the rollout",
                )
            return WaveAdmissionPlan(
                "resume",
                requested_limit,
                current_main_sha,
                expected_generation=checkpoint.generation,
                wave_index=current.wave_index,
                lease=lease,
                requires_ancestry=resume_requires_ancestry,
                ancestor_shas=(self._ancestry_shas(current) if resume_requires_ancestry else ()),
                checkpoint=checkpoint,
                diagnostic="resuming the immutable selected issue identifiers",
            )

        if not current.complete or not current.passed:
            failures = (
                ", ".join(
                    f"#{outcome.issue_number}: {outcome.reason}"
                    for outcome in current.outcomes
                    if not outcome.passed
                )
                or "selected issues have not reached terminal outcomes"
            )
            raise IssueWaveBlockedError(
                f"issue wave {current.wave_index + 1} is not complete; repair or recover it before "
                f"requesting the next wave ({failures})"
            )
        expected = self._next_limit(current.limit)
        if requested_limit != expected:
            expected_text = (
                "all eligible issues" if expected is None else f"--issue-limit {expected}"
            )
            actual_text = (
                "all eligible issues"
                if requested_limit is None
                else f"--issue-limit {requested_limit}"
            )
            raise IssueWaveBlockedError(
                f"unexpected issue-wave selector {actual_text}; after wave "
                f"{current.limit or 'all'} "
                f"the next selector is {expected_text}"
            )
        return WaveAdmissionPlan(
            "select",
            requested_limit,
            current_main_sha,
            expected_generation=checkpoint.generation,
            wave_index=current.wave_index + 1,
            requires_ancestry=current.verified_main_sha != current_main_sha,
            ancestor_shas=self._ancestry_shas(current),
            checkpoint=checkpoint,
            diagnostic="prior issue wave verified before selecting the next wave",
        )

    def plan_admission(
        self, current_main_sha: str, requested_limit: int | None
    ) -> WaveAdmissionPlan:
        """Plan admission without sealing or advancing the checkpoint."""
        with self._locked():
            return self._plan_locked(current_main_sha, requested_limit)

    admission_plan = plan_admission

    def seal_selection(
        self,
        plan: WaveAdmissionPlan,
        issue_numbers: Iterator[int] | tuple[int, ...] | list[int],
    ) -> WaveLease:
        """Compare-and-swap a newly selected ordered issue identity list."""
        if plan.mode not in {"select", "resume"}:
            raise IssueWaveConflictError("only a selecting or resuming wave can be sealed")
        selected = _positive_issue_numbers(tuple(issue_numbers), "selected issue_numbers")
        if plan.mode == "resume":
            if plan.lease is None or selected != plan.lease.issue_numbers:
                raise IssueWaveConflictError("resume selection does not match the immutable wave")
            return plan.lease
        if plan.requested_limit is not None and len(selected) > plan.requested_limit:
            raise IssueWaveValidationError("selected issue count exceeds issue-limit")
        with self._locked():
            current = self._read_unlocked()
            generation = 0 if current is None else current.generation
            if generation != plan.expected_generation:
                raise IssueWaveConflictError(
                    "issue-wave checkpoint changed before selection sealing"
                )
            if current is not None and current.org != self.org:
                raise IssueWaveValidationError("issue-wave organization binding changed")
            record = WaveRecord(
                wave_index=plan.wave_index if plan.wave_index is not None else 0,
                limit=plan.requested_limit,
                issue_numbers=selected,
                base_main_sha=plan.current_main_sha,
                lease_nonce=uuid.uuid4().hex,
            )
            waves = () if current is None else current.waves
            checkpoint = WaveCheckpoint(
                org=self.org,
                repo=self.repo,
                generation=generation + 1,
                status="active",
                waves=(*waves, record),
            )
            self._write_unlocked(checkpoint)
            return record.lease(self.org, self.repo)

    seal = seal_selection

    def bind_recovery_scope(
        self, issue_numbers: set[int] | frozenset[int], current_main_sha: str
    ) -> WaveLease | None:
        """Validate direct recovery identifiers against an active wave.

        No checkpoint is created for ordinary explicit recovery.  A completed
        rollout also leaves explicit recovery available without reopening the
        staged rollout.
        """
        with self._locked():
            checkpoint = self._read_unlocked()
            if checkpoint is None or checkpoint.status == "completed":
                return None
            if not is_full_commit_sha(current_main_sha):
                raise IssueWaveValidationError(
                    "recovery scope requires a full synchronized main SHA"
                )
            selected = _positive_issue_numbers(
                tuple(sorted(issue_numbers)), "recovery issue_numbers"
            )
            lease = checkpoint.current_wave.lease(self.org, self.repo)
            if not set(selected).issubset(set(lease.issue_numbers)):
                outside = sorted(set(selected) - set(lease.issue_numbers))
                raise IssueWaveBlockedError(
                    "direct recovery issue scope is outside active wave "
                    f"{lease.wave_index + 1}: {outside}"
                )
            return lease

    def receipt_for(self, lease: WaveLease, issue_number: int) -> WaveMergeReceipt | None:
        """Return a recorded receipt for an issue, if one exists."""
        with self._locked():
            checkpoint = self._read_unlocked()
            if checkpoint is None:
                return None
            record = self._current_record(checkpoint, lease)
            return next(
                (item for item in record.merge_receipts if item.issue_number == issue_number), None
            )

    def record_merge_receipt(
        self,
        lease: WaveLease,
        *,
        issue_number: int,
        pr_number: int,
        reviewed_head_sha: str,
        merge_sha: str,
    ) -> WaveLease:
        """Persist one successful conditional normal-merge receipt."""
        receipt = WaveMergeReceipt(issue_number, pr_number, reviewed_head_sha, merge_sha)
        with self._locked():
            checkpoint = self._read_unlocked()
            if checkpoint is None:
                raise IssueWaveConflictError("cannot record a receipt without a checkpoint")
            record = self._current_record(checkpoint, lease)
            existing = next(
                (item for item in record.merge_receipts if item.issue_number == issue_number), None
            )
            if existing is not None:
                if existing != receipt:
                    raise IssueWaveConflictError(
                        "a different merge receipt already exists for this issue"
                    )
                return lease
            updated = replace(record, merge_receipts=(*record.merge_receipts, receipt))
            waves = (
                *checkpoint.waves[: record.wave_index],
                updated,
                *checkpoint.waves[record.wave_index + 1 :],
            )
            self._write_unlocked(
                replace(checkpoint, generation=checkpoint.generation + 1, waves=waves)
            )
            return lease

    def record_terminal_outcome(
        self,
        lease: WaveLease,
        *,
        issue_number: int,
        passed: bool,
        reason: str,
        pr_number: int | None = None,
    ) -> WaveLease:
        """Persist a terminal issue outcome before it reaches the ledger."""
        outcome = WaveIssueOutcome(issue_number, passed, reason, pr_number)
        with self._locked():
            checkpoint = self._read_unlocked()
            if checkpoint is None:
                raise IssueWaveConflictError("cannot record an outcome without a checkpoint")
            record = self._current_record(checkpoint, lease)
            if passed and not any(
                item.issue_number == issue_number for item in record.merge_receipts
            ):
                raise IssueWaveBlockedError(
                    "cannot record passed outcome for "
                    f"#{issue_number} without a loop-owned merge receipt"
                )
            existing = next(
                (item for item in record.outcomes if item.issue_number == issue_number), None
            )
            if existing is not None and existing != outcome:
                if existing.passed:
                    raise IssueWaveConflictError("a passed terminal outcome is immutable")
                if not passed and existing.reason == reason and existing.pr_number == pr_number:
                    return lease
            outcomes = (
                *[item for item in record.outcomes if item.issue_number != issue_number],
                outcome,
            )
            updated = replace(record, outcomes=outcomes)
            waves = (
                *checkpoint.waves[: record.wave_index],
                updated,
                *checkpoint.waves[record.wave_index + 1 :],
            )
            self._write_unlocked(
                replace(checkpoint, generation=checkpoint.generation + 1, waves=waves)
            )
            return lease

    @staticmethod
    def _validate_receipt_facts(
        record: WaveRecord,
        facts_by_issue: Mapping[int, Any],
    ) -> None:
        """Require every durable receipt to match fresh GitHub merge facts."""
        for receipt in record.merge_receipts:
            facts = facts_by_issue.get(receipt.issue_number)
            if facts is None:
                raise IssueWaveBlockedError(
                    f"issue #{receipt.issue_number} lacks fresh merge facts"
                )
            labels = set(getattr(facts, "labels", set()))
            if (
                bool(getattr(facts, "is_epic", False))
                or "state:skip" in labels
                or "state:plan-blocked" in labels
            ):
                raise IssueWaveBlockedError(
                    f"issue #{receipt.issue_number} has an external skip/block override"
                )
            if getattr(facts, "pr_number", None) != receipt.pr_number or not bool(
                getattr(facts, "pr_is_merged", False)
            ):
                raise IssueWaveBlockedError(
                    f"issue #{receipt.issue_number} no longer matches its recorded merged PR"
                )

    def validate_active_wave_facts(
        self,
        lease: WaveLease,
        facts_by_issue: Mapping[int, Any],
    ) -> None:
        """Reconcile an active wave's receipts without requiring completion."""
        with self._locked():
            checkpoint = self._read_unlocked()
            if checkpoint is None:
                raise IssueWaveBlockedError("active-wave checkpoint disappeared")
            record = self._current_record(checkpoint, lease)
            self._validate_receipt_facts(record, facts_by_issue)

    def validate_prior_wave_facts(
        self,
        lease: WaveLease,
        facts_by_issue: Mapping[int, Any],
    ) -> None:
        """Fail closed when GitHub facts show post-wave external drift."""
        with self._locked():
            checkpoint = self._read_unlocked()
            if checkpoint is None:
                raise IssueWaveBlockedError("prior-wave checkpoint disappeared")
            record = self._current_record(checkpoint, lease)
            for outcome in record.outcomes:
                if not outcome.passed:
                    raise IssueWaveBlockedError(
                        f"issue #{outcome.issue_number} failed in prior wave: {outcome.reason}"
                    )
                receipt = next(
                    (
                        item
                        for item in record.merge_receipts
                        if item.issue_number == outcome.issue_number
                    ),
                    None,
                )
                if receipt is None:
                    raise IssueWaveBlockedError(
                        f"issue #{outcome.issue_number} lacks a complete loop-owned merge receipt"
                    )
            self._validate_receipt_facts(record, facts_by_issue)

    def verify_prior_wave(
        self,
        lease: WaveLease,
        *,
        current_main_sha: str,
        ancestry_verified: bool = False,
        ancestry_check: Callable[[str, tuple[str, ...]], bool] | None = None,
        facts_by_issue: Mapping[int, Any] | None = None,
    ) -> WaveCheckpoint:
        """Verify outcomes, receipts, facts, and Git ancestry on current main."""
        if not is_full_commit_sha(current_main_sha):
            raise IssueWaveValidationError("prior-wave verification requires a full main SHA")
        with self._locked():
            checkpoint = self._read_unlocked()
            if checkpoint is None:
                raise IssueWaveBlockedError("prior-wave checkpoint is absent")
            record = self._current_record(checkpoint, lease)
            if not record.complete:
                raise IssueWaveBlockedError(
                    "prior wave has not recorded every selected issue outcome"
                )
            if not record.passed:
                raise IssueWaveBlockedError("prior wave contains failed terminal outcomes")
            if len(record.merge_receipts) != len(record.issue_numbers):
                raise IssueWaveBlockedError(
                    "prior wave is missing one or more loop-owned merge receipts"
                )
            if ancestry_check is not None:
                try:
                    ancestry_verified = bool(
                        ancestry_check(current_main_sha, self._ancestry_shas(record))
                    )
                except Exception as exc:
                    raise IssueWaveBlockedError(
                        f"prior-wave ancestry verification failed: {exc}"
                    ) from exc
            if facts_by_issue is not None:
                # The lock is already held; validate the facts inline to avoid
                # recursively acquiring the stable sibling lock.
                for outcome in record.outcomes:
                    facts = facts_by_issue.get(outcome.issue_number)
                    receipt = next(
                        item
                        for item in record.merge_receipts
                        if item.issue_number == outcome.issue_number
                    )
                    if (
                        facts is None
                        or getattr(facts, "pr_number", None) != receipt.pr_number
                        or not bool(getattr(facts, "pr_is_merged", False))
                    ):
                        raise IssueWaveBlockedError(
                            f"issue #{outcome.issue_number} is closed/unmerged or "
                            "externally changed"
                        )
            if not ancestry_verified and record.issue_numbers:
                raise IssueWaveBlockedError(
                    "prior-wave merge ancestry was not verified against synchronized main"
                )
            updated = replace(record, verified_main_sha=current_main_sha)
            waves = (
                *checkpoint.waves[: record.wave_index],
                updated,
                *checkpoint.waves[record.wave_index + 1 :],
            )
            updated_checkpoint = replace(
                checkpoint, generation=checkpoint.generation + 1, waves=waves
            )
            self._write_unlocked(updated_checkpoint)
            return updated_checkpoint

    def complete_rollout(self, lease: WaveLease, *, current_main_sha: str) -> WaveCheckpoint:
        """Mark the final verified wave completed; future unbounded runs audit only."""
        if not is_full_commit_sha(current_main_sha):
            raise IssueWaveValidationError("rollout completion requires a full main SHA")
        with self._locked():
            checkpoint = self._read_unlocked()
            if checkpoint is None:
                raise IssueWaveBlockedError("cannot complete an absent rollout")
            record = self._current_record(checkpoint, lease)
            if record.limit is not None or not record.complete or not record.passed:
                raise IssueWaveBlockedError(
                    "only a complete passing all-eligible wave can finish rollout"
                )
            if record.verified_main_sha != current_main_sha:
                raise IssueWaveBlockedError("final wave is not verified against current main")
            updated = replace(
                checkpoint,
                generation=checkpoint.generation + 1,
                status="completed",
                completed_main_sha=current_main_sha,
            )
            self._write_unlocked(updated)
            return updated

    mark_completed = complete_rollout

    def audit_only(self, current_main_sha: str) -> WaveCheckpoint:
        """Read and validate a completed checkpoint without any mutation."""
        if not is_full_commit_sha(current_main_sha):
            raise IssueWaveValidationError("audit requires a full main SHA")
        checkpoint = self.load()
        if checkpoint is None or checkpoint.status != "completed":
            raise IssueWaveBlockedError("issue-wave rollout is not in completed audit-only state")
        return checkpoint


def wave_entry_from_facts(
    lease: WaveLease,
    facts: Any,
    entry: SeedEntry,
    repo_root: Path,
    org: str,
    repo: str,
) -> SeedEntry:
    """Turn post-seal issue drift into a terminal item without GitHub writes."""
    if facts.number not in lease.issue_numbers:
        return replace(
            entry,
            stage=StageName.FINISHED,
            reason=f"issue #{facts.number} is outside the sealed issue wave",
            passed=False,
        )
    receipt = IssueWaveStore(repo_root, org, repo).receipt_for(lease, facts.number)
    if facts.pr_is_merged and receipt is not None and facts.pr_number == receipt.pr_number:
        return replace(
            entry,
            stage=StageName.FINISHED,
            reason=f"issue #{facts.number} already has its recorded loop-owned merge",
            passed=True,
            pr_number=receipt.pr_number,
        )
    if facts.issue_is_closed:
        reason = f"issue #{facts.number} closed without its recorded loop-owned merge"
    elif facts.is_epic or entry.stage is None:
        reason = f"issue #{facts.number} became skipped, blocked, or epic after wave selection"
    elif entry.stage is StageName.FINISHED:
        reason = f"issue #{facts.number} reached a terminal state without a wave receipt"
    else:
        return entry
    return replace(entry, stage=StageName.FINISHED, reason=reason, passed=False)


__all__ = [
    "WAVE_LEASE_PAYLOAD",
    "WAVE_LIMITS",
    "IssueWaveBlockedError",
    "IssueWaveConflictError",
    "IssueWaveError",
    "IssueWaveRepositoryError",
    "IssueWaveStore",
    "IssueWaveValidationError",
    "WaveAdmissionPlan",
    "WaveCheckpoint",
    "WaveIssueOutcome",
    "WaveLease",
    "WaveMergeReceipt",
    "WaveRecord",
    "is_full_commit_sha",
]
