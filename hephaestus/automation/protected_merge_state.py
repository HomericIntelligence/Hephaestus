"""Durable, repository-scoped records for protected merges and rebases.

The queue coordinator keeps its work items in memory.  A protected merge needs
two small exceptions that must survive a process restart and coordinate across
processes: at most one worker may issue the merge request for one readiness
fingerprint, and at most eight rebase cycles may update one pull request.  This
module owns both exceptions.  It contains no GitHub or Git subprocesses;
callers provide fresh facts.
"""

from __future__ import annotations

import json
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.io.utils import write_secure
from hephaestus.utils.file_lock import LockUnavailableError, file_lock
from hephaestus.utils.git import _is_full_commit_sha

SCHEMA = "hephaestus.protected-merge-state.v1"
STATE_NAME = "protected-merge-state.json"
LOCK_NAME = "protected-merge-state.lock"
MAX_REBASE_CYCLES = 8

_CONCLUSIVE_HTTP_STATUSES = frozenset({200, 409, 405})


class ProtectedMergeStateError(RuntimeError):
    """Base class for fail-closed protected-merge state errors."""


class ProtectedMergeValidationError(ProtectedMergeStateError):
    """Raised when state data or an operation argument is malformed."""


class ProtectedMergeConflictError(ProtectedMergeStateError):
    """Raised when another process changed or holds the state file."""


class ProtectedMergeRepositoryError(ProtectedMergeStateError):
    """Raised when state paths are not confined to a repository."""


class ProtectedRebaseCyclesExhausted(ProtectedMergeStateError):  # noqa: N818
    """Raised when a ninth rebase cycle is requested for one pull request."""


class ProtectedMergeUncertainError(ProtectedMergeStateError):
    """Raised when crash reconciliation cannot prove a durable record."""


# The file lock alone serializes processes.  It does not give same-process
# threads one deterministic claimant because flock is per open file
# description, so this layer also names a lock per store path.
_KEY_LOCKS: dict[str, threading.Lock] = {}
_KEY_LOCKS_GUARD = threading.Lock()


def _key_lock_for(key: str) -> threading.Lock:
    """Return the stable in-process lock for one store path."""
    with _KEY_LOCKS_GUARD:
        lock = _KEY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _KEY_LOCKS[key] = lock
        return lock


def _required_text(value: Any, field_name: str) -> str:
    """Validate a non-empty text field."""
    if not isinstance(value, str) or not value:
        raise ProtectedMergeValidationError(f"{field_name} must be a non-empty string")
    return value


def _full_sha(value: Any, field_name: str) -> str:
    """Validate a full Git commit identifier."""
    if not _is_full_commit_sha(value):
        raise ProtectedMergeValidationError(f"{field_name} must be a full commit SHA")
    return value


def _positive_int(value: Any, field_name: str) -> int:
    """Validate a positive integer field."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProtectedMergeValidationError(f"{field_name} must be a positive integer")
    return value


@dataclass(frozen=True)
class MergeAttemptKey:
    """Immutable identity of one merge-readiness fingerprint."""

    repository: str
    pr_number: int
    reviewed_head_sha: str
    base_sha: str
    proof_generation: int
    mergeable: str
    readiness_status: str

    def __post_init__(self) -> None:
        """Validate the immutable claim identity."""
        _required_text(self.repository, "merge attempt repository")
        _positive_int(self.pr_number, "merge attempt pr_number")
        _full_sha(self.reviewed_head_sha, "merge attempt reviewed_head_sha")
        _full_sha(self.base_sha, "merge attempt base_sha")
        if (
            isinstance(self.proof_generation, bool)
            or not isinstance(self.proof_generation, int)
            or self.proof_generation < 0
        ):
            raise ProtectedMergeValidationError(
                "merge attempt proof_generation must be a non-negative integer"
            )
        _required_text(self.mergeable, "merge attempt mergeable")
        _required_text(self.readiness_status, "merge attempt readiness_status")

    @property
    def fingerprint(self) -> str:
        """Return the stable storage key for this readiness fingerprint."""
        return "|".join(
            (
                self.repository,
                str(self.pr_number),
                self.reviewed_head_sha,
                self.base_sha,
                str(self.proof_generation),
                self.mergeable,
                self.readiness_status,
            )
        )


@dataclass(frozen=True)
class MergeAttemptRecord:
    """Immutable durable record of one merge request claim."""

    key: MergeAttemptKey
    state: Literal["requested", "conclusive", "ambiguous"]
    http_status: int | None = None
    classification: str | None = None
    ambiguous: bool = False


@dataclass(frozen=True)
class MergeAttemptGrant:
    """Immutable result of one merge-attempt claim."""

    granted: bool
    reconcile_only: bool
    record: MergeAttemptRecord

    def __post_init__(self) -> None:
        """Validate the closed grant shape."""
        if not isinstance(self.granted, bool) or not isinstance(self.reconcile_only, bool):
            raise ProtectedMergeValidationError("grant fields must be booleans")
        if self.granted and self.reconcile_only:
            raise ProtectedMergeValidationError("a granted claim cannot be reconcile-only")


@dataclass(frozen=True)
class RebaseCycleRecord:
    """Immutable durable record of one protected rebase cycle."""

    index: int
    requested_head_sha: str
    requested_base_sha: str
    state: Literal["requested", "prepared", "published", "failed"]
    published_head_sha: str | None = None
    bridge_sha: str | None = None
    logical_tip_sha: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        """Validate the immutable cycle record."""
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise ProtectedMergeValidationError("rebase cycle index must be non-negative")
        if self.state not in {"requested", "prepared", "published", "failed"}:
            raise ProtectedMergeValidationError("rebase cycle state is unsupported")
        if self.state == "prepared" and not _is_full_commit_sha(self.bridge_sha):
            raise ProtectedMergeValidationError("a prepared cycle requires a full bridge SHA")
        if self.state == "prepared" and not _is_full_commit_sha(self.logical_tip_sha):
            raise ProtectedMergeValidationError("a prepared cycle requires a full logical tip SHA")
        if self.state == "published" and not _is_full_commit_sha(self.published_head_sha):
            raise ProtectedMergeValidationError(
                "a published cycle requires a full published head SHA"
            )
        if self.state == "published" and not _is_full_commit_sha(self.logical_tip_sha):
            raise ProtectedMergeValidationError("a published cycle requires a full logical tip SHA")


@dataclass(frozen=True)
class ProtectedMergeState:
    """Immutable full snapshot of one repository's durable records."""

    repository: str
    generation: int
    merge_attempts: tuple[MergeAttemptRecord, ...]
    rebase_cycles: tuple[tuple[int, tuple[RebaseCycleRecord, ...]], ...]


def _decode(raw: Any) -> tuple[dict[str, MergeAttemptRecord], dict[int, list[RebaseCycleRecord]]]:  # noqa: C901
    """Strictly decode the durable state payload into record mappings."""
    if not isinstance(raw, dict):
        raise ProtectedMergeValidationError("protected-merge state must be an object")
    if raw.get("schema") != SCHEMA:
        raise ProtectedMergeValidationError("protected-merge state schema is unsupported")
    if isinstance(raw.get("generation"), bool) or not isinstance(raw.get("generation"), int):
        raise ProtectedMergeValidationError("protected-merge state generation must be an integer")
    attempts_raw = raw.get("merge_attempts", [])
    cycles_raw = raw.get("rebase_cycles", [])
    if not isinstance(attempts_raw, list) or not isinstance(cycles_raw, list):
        raise ProtectedMergeValidationError("protected-merge state records must be lists")
    attempts: dict[str, MergeAttemptRecord] = {}
    for value in attempts_raw:
        if not isinstance(value, dict):
            raise ProtectedMergeValidationError("merge attempt record must be an object")
        key = MergeAttemptKey(
            repository=cast(str, value.get("repository")),
            pr_number=cast(int, value.get("pr_number")),
            reviewed_head_sha=cast(str, value.get("reviewed_head_sha")),
            base_sha=cast(str, value.get("base_sha")),
            proof_generation=cast(int, value.get("proof_generation")),
            mergeable=cast(str, value.get("mergeable")),
            readiness_status=cast(str, value.get("readiness_status")),
        )
        state = value.get("state")
        if state not in {"requested", "conclusive", "ambiguous"}:
            raise ProtectedMergeValidationError("merge attempt state is unsupported")
        http_status = value.get("http_status")
        if http_status is not None and (
            isinstance(http_status, bool) or not isinstance(http_status, int)
        ):
            raise ProtectedMergeValidationError("merge attempt http_status must be an integer")
        ambiguous = value.get("ambiguous")
        if not isinstance(ambiguous, bool):
            raise ProtectedMergeValidationError("merge attempt ambiguous flag must be a boolean")
        record = MergeAttemptRecord(
            key=key,
            state=cast(Literal["requested", "conclusive", "ambiguous"], state),
            http_status=http_status,
            classification=(
                cast(str, value.get("classification"))
                if value.get("classification") is not None
                else None
            ),
            ambiguous=ambiguous,
        )
        if key.fingerprint in attempts:
            raise ProtectedMergeValidationError("duplicate merge attempt fingerprint in state")
        attempts[key.fingerprint] = record
    cycles: dict[int, list[RebaseCycleRecord]] = {}
    for cycle_value in cycles_raw:
        if not isinstance(cycle_value, dict):
            raise ProtectedMergeValidationError("rebase cycle record must be an object")
        pr_number = _positive_int(cycle_value.get("pr_number"), "rebase cycle pr_number")
        cycle_record = RebaseCycleRecord(
            index=cast(int, cycle_value.get("index")),
            requested_head_sha=_full_sha(
                cycle_value.get("requested_head_sha"), "rebase cycle requested_head_sha"
            ),
            requested_base_sha=_full_sha(
                cycle_value.get("requested_base_sha"), "rebase cycle requested_base_sha"
            ),
            state=cast(
                Literal["requested", "prepared", "published", "failed"],
                cycle_value.get("state"),
            ),
            published_head_sha=(
                _full_sha(cycle_value.get("published_head_sha"), "rebase cycle published_head_sha")
                if cycle_value.get("published_head_sha") is not None
                else None
            ),
            bridge_sha=(
                _full_sha(cycle_value.get("bridge_sha"), "rebase cycle bridge_sha")
                if cycle_value.get("bridge_sha") is not None
                else None
            ),
            logical_tip_sha=(
                _full_sha(cycle_value.get("logical_tip_sha"), "rebase cycle logical_tip_sha")
                if cycle_value.get("logical_tip_sha") is not None
                else None
            ),
            failure_reason=(
                cast(str, cycle_value.get("failure_reason"))
                if cycle_value.get("failure_reason") is not None
                else None
            ),
        )
        family = cycles.setdefault(pr_number, [])
        if cycle_record.index != len(family):
            raise ProtectedMergeValidationError("rebase cycle indexes must be contiguous")
        family.append(cycle_record)
    for family in cycles.values():
        if len(family) > MAX_REBASE_CYCLES:
            raise ProtectedMergeValidationError("rebase cycle family exceeds the durable bound")
    return attempts, cycles


def _encode(
    repository: str,
    generation: int,
    attempts: dict[str, MergeAttemptRecord],
    cycles: dict[int, list[RebaseCycleRecord]],
) -> str:
    """Encode the durable state payload deterministically."""
    attempts_json = [
        {
            "repository": record.key.repository,
            "pr_number": record.key.pr_number,
            "reviewed_head_sha": record.key.reviewed_head_sha,
            "base_sha": record.key.base_sha,
            "proof_generation": record.key.proof_generation,
            "mergeable": record.key.mergeable,
            "readiness_status": record.key.readiness_status,
            "state": record.state,
            "http_status": record.http_status,
            "classification": record.classification,
            "ambiguous": record.ambiguous,
        }
        for _, record in sorted(attempts.items())
    ]
    cycles_json = [
        {
            "pr_number": pr_number,
            "index": record.index,
            "requested_head_sha": record.requested_head_sha,
            "requested_base_sha": record.requested_base_sha,
            "state": record.state,
            "published_head_sha": record.published_head_sha,
            "bridge_sha": record.bridge_sha,
            "logical_tip_sha": record.logical_tip_sha,
            "failure_reason": record.failure_reason,
        }
        for pr_number, family in sorted(cycles.items())
        for record in family
    ]
    return (
        json.dumps(
            {
                "schema": SCHEMA,
                "repository": repository,
                "generation": generation,
                "merge_attempts": attempts_json,
                "rebase_cycles": cycles_json,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


class ProtectedMergeStateStore:
    """Strict, atomic store for one repository's protected merge records."""

    def __init__(self, repo_root: Path, repository: str) -> None:
        """Bind the store to one existing repository checkout."""
        self.repo_root = Path(repo_root)
        self.repository = _required_text(repository, "protected-merge repository")
        self._validate_paths()

    @property
    def state_dir(self) -> Path:
        """Return the repository-confined state directory."""
        return self.repo_root / DEFAULT_STATE_DIR

    @property
    def state_path(self) -> Path:
        """Return the durable state path."""
        return self.state_dir / STATE_NAME

    @property
    def lock_path(self) -> Path:
        """Return the stable sibling lock path."""
        return self.state_dir / LOCK_NAME

    @property
    def path(self) -> Path:
        """Compatibility alias for :attr:`state_path`."""
        return self.state_path

    def _validate_paths(self) -> None:
        """Reject symlink escapes and non-directory repository roots."""
        if self.repo_root.is_symlink() or not self.repo_root.is_dir():
            raise ProtectedMergeRepositoryError(
                f"protected-merge repository root is not a real directory: {self.repo_root}"
            )
        root = self.repo_root.resolve(strict=True)
        for candidate in (
            self.repo_root / "build",
            self.state_dir,
            self.state_path,
            self.lock_path,
        ):
            if candidate.is_symlink():
                raise ProtectedMergeRepositoryError(
                    f"refusing symlinked protected-merge path: {candidate}"
                )
            try:
                resolved = candidate.resolve(strict=False)
            except OSError as exc:
                raise ProtectedMergeRepositoryError(
                    f"cannot resolve protected-merge path: {candidate}"
                ) from exc
            if not resolved.is_relative_to(root):
                raise ProtectedMergeRepositoryError(
                    f"protected-merge path escapes repository: {candidate}"
                )
        for candidate in (self.state_path, self.lock_path):
            if candidate.exists() and not candidate.is_file():
                raise ProtectedMergeRepositoryError(
                    f"protected-merge path is not a regular file: {candidate}"
                )

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Acquire the keyed in-process lock, then the exclusive file lock."""
        key_lock = _key_lock_for(str(self.lock_path.resolve()))
        with key_lock:
            self._validate_paths()
            try:
                with file_lock(self.lock_path, blocking=False, require_exclusive=True):
                    self._validate_paths()
                    yield
            except LockUnavailableError as exc:
                raise ProtectedMergeConflictError(
                    f"protected-merge state is busy for {self.repository}; retry later"
                ) from exc
            except ProtectedMergeStateError:
                raise
            except (OSError, RuntimeError) as exc:
                raise ProtectedMergeRepositoryError(
                    f"cannot lock protected-merge state: {exc}"
                ) from exc

    def _read_unlocked(
        self,
    ) -> tuple[int, dict[str, MergeAttemptRecord], dict[int, list[RebaseCycleRecord]]]:
        self._validate_paths()
        path = self.state_path
        if not path.exists():
            return 0, {}, {}
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise ProtectedMergeValidationError(
                f"protected-merge state permissions are too broad: {path}"
            )
        try:
            with path.open(encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ProtectedMergeValidationError(
                f"cannot read protected-merge state {path}: {exc}"
            ) from exc
        repository = raw.get("repository") if isinstance(raw, dict) else None
        if repository != self.repository:
            raise ProtectedMergeValidationError(
                "protected-merge state repository binding does not match the current repository"
            )
        attempts, cycles = _decode(raw)
        return cast(int, raw["generation"]), attempts, cycles

    def _write_unlocked(
        self,
        generation: int,
        attempts: dict[str, MergeAttemptRecord],
        cycles: dict[int, list[RebaseCycleRecord]],
    ) -> None:
        """Atomically write a validated state with owner-only permissions."""
        self._validate_paths()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._validate_paths()
        write_secure(
            self.state_path,
            _encode(self.repository, generation, attempts, cycles),
            permissions=0o600,
        )

    def load(self) -> ProtectedMergeState | None:
        """Strictly load the durable state, returning ``None`` when absent."""
        with self._locked():
            path = self.state_path
            if not path.exists():
                return None
            generation, attempts, cycles = self._read_unlocked()
        return ProtectedMergeState(
            repository=self.repository,
            generation=generation,
            merge_attempts=tuple(attempts[key] for key in sorted(attempts)),
            rebase_cycles=tuple(
                (pr_number, tuple(family)) for pr_number, family in sorted(cycles.items())
            ),
        )

    strict_load = load

    def claim_merge_attempt(self, key: MergeAttemptKey) -> MergeAttemptGrant:
        """Claim the one merge request allowed for a readiness fingerprint.

        The winner gets a durable ``requested`` record before any merge
        request.  Every other in-process or cross-process claimant for the
        same fingerprint gets a reconcile-only grant.  A fingerprint that
        carries the permanent ambiguous flag is never granted again.
        """
        if key.repository != self.repository:
            raise ProtectedMergeValidationError(
                "merge attempt repository does not match the store binding"
            )
        with self._locked():
            generation, attempts, cycles = self._read_unlocked()
            existing = attempts.get(key.fingerprint)
            if existing is not None:
                return MergeAttemptGrant(granted=False, reconcile_only=True, record=existing)
            record = MergeAttemptRecord(key=key, state="requested")
            attempts[key.fingerprint] = record
            self._write_unlocked(generation + 1, attempts, cycles)
            return MergeAttemptGrant(granted=True, reconcile_only=False, record=record)

    def record_claim_response(
        self, key: MergeAttemptKey, status: int, classification: str
    ) -> MergeAttemptRecord:
        """Record a conclusive 200, 409, or 405 merge response for a claim."""
        if status not in _CONCLUSIVE_HTTP_STATUSES:
            raise ProtectedMergeValidationError(
                "merge claim response status must be one of 200, 409, or 405"
            )
        _required_text(classification, "merge claim classification")
        with self._locked():
            generation, attempts, cycles = self._read_unlocked()
            existing = attempts.get(key.fingerprint)
            if existing is None or existing.state != "requested" or existing.ambiguous:
                raise ProtectedMergeConflictError(
                    "merge claim is absent or is no longer in the requested state"
                )
            record = MergeAttemptRecord(
                key=key,
                state="conclusive",
                http_status=status,
                classification=classification,
            )
            attempts[key.fingerprint] = record
            self._write_unlocked(generation + 1, attempts, cycles)
            return record

    def mark_claim_ambiguous(self, key: MergeAttemptKey) -> MergeAttemptRecord:
        """Permanently mark a claim whose merge response was lost or malformed."""
        with self._locked():
            generation, attempts, cycles = self._read_unlocked()
            existing = attempts.get(key.fingerprint)
            if existing is None or existing.state != "requested":
                raise ProtectedMergeConflictError(
                    "merge claim is absent or is no longer in the requested state"
                )
            record = MergeAttemptRecord(key=key, state="ambiguous", ambiguous=True)
            attempts[key.fingerprint] = record
            self._write_unlocked(generation + 1, attempts, cycles)
            return record

    def claim_rebase_cycle(
        self,
        pr_number: int,
        head_sha: str,
        base_sha: str,
    ) -> RebaseCycleRecord:
        """Claim one of the durable rebase cycles for a pull request.

        A repeated observation of the same head and base returns the existing
        record without consuming a cycle.  A ninth distinct cycle raises
        :class:`ProtectedRebaseCyclesExhausted`.
        """
        _positive_int(pr_number, "rebase cycle pr_number")
        _full_sha(head_sha, "rebase cycle head_sha")
        _full_sha(base_sha, "rebase cycle base_sha")
        with self._locked():
            generation, attempts, cycles = self._read_unlocked()
            family = list(cycles.get(pr_number, ()))
            if family:
                last = family[-1]
                if (
                    last.requested_head_sha == head_sha
                    and last.requested_base_sha == base_sha
                    and last.state in {"requested", "prepared", "published"}
                ):
                    return last
            if len(family) >= MAX_REBASE_CYCLES:
                raise ProtectedRebaseCyclesExhausted(
                    f"protected rebase cycle bound reached for PR #{pr_number}"
                )
            record = RebaseCycleRecord(
                index=len(family),
                requested_head_sha=head_sha,
                requested_base_sha=base_sha,
                state="requested",
            )
            family.append(record)
            cycles[pr_number] = family
            self._write_unlocked(generation + 1, attempts, cycles)
            return record

    def _cycle_locked(
        self,
        cycles: dict[int, list[RebaseCycleRecord]],
        pr_number: int,
        index: int,
    ) -> tuple[list[RebaseCycleRecord], RebaseCycleRecord]:
        family = cycles.get(pr_number)
        if family is None or index < 0 or index >= len(family):
            raise ProtectedMergeConflictError("rebase cycle record is absent")
        return family, family[index]

    def mark_cycle_prepared(
        self,
        pr_number: int,
        index: int,
        bridge_sha: str,
        logical_tip_sha: str,
    ) -> RebaseCycleRecord:
        """Record the built bridge for a requested cycle before publication."""
        _full_sha(bridge_sha, "rebase cycle bridge_sha")
        _full_sha(logical_tip_sha, "rebase cycle logical_tip_sha")
        with self._locked():
            generation, attempts, cycles = self._read_unlocked()
            family, current = self._cycle_locked(cycles, pr_number, index)
            if current.state != "requested":
                raise ProtectedMergeConflictError("rebase cycle is not in the requested state")
            record = RebaseCycleRecord(
                index=current.index,
                requested_head_sha=current.requested_head_sha,
                requested_base_sha=current.requested_base_sha,
                state="prepared",
                bridge_sha=bridge_sha,
                logical_tip_sha=logical_tip_sha,
            )
            family[index] = record
            self._write_unlocked(generation + 1, attempts, cycles)
            return record

    def mark_cycle_published(
        self,
        pr_number: int,
        index: int,
        published_head_sha: str,
    ) -> RebaseCycleRecord:
        """Record a successful ordinary push of the prepared bridge."""
        _full_sha(published_head_sha, "rebase cycle published_head_sha")
        with self._locked():
            generation, attempts, cycles = self._read_unlocked()
            family, current = self._cycle_locked(cycles, pr_number, index)
            if current.state != "prepared":
                raise ProtectedMergeConflictError("rebase cycle is not in the prepared state")
            record = RebaseCycleRecord(
                index=current.index,
                requested_head_sha=current.requested_head_sha,
                requested_base_sha=current.requested_base_sha,
                state="published",
                published_head_sha=published_head_sha,
                bridge_sha=current.bridge_sha,
                logical_tip_sha=current.logical_tip_sha,
            )
            family[index] = record
            self._write_unlocked(generation + 1, attempts, cycles)
            return record

    def mark_cycle_failed(
        self,
        pr_number: int,
        index: int,
        reason: str,
    ) -> RebaseCycleRecord:
        """Record a terminal failure for a requested or prepared cycle."""
        _required_text(reason, "rebase cycle failure reason")
        with self._locked():
            generation, attempts, cycles = self._read_unlocked()
            family, current = self._cycle_locked(cycles, pr_number, index)
            if current.state not in {"requested", "prepared"}:
                raise ProtectedMergeConflictError("rebase cycle is not in a non-terminal state")
            record = RebaseCycleRecord(
                index=current.index,
                requested_head_sha=current.requested_head_sha,
                requested_base_sha=current.requested_base_sha,
                state="failed",
                bridge_sha=current.bridge_sha,
                logical_tip_sha=current.logical_tip_sha,
                failure_reason=reason,
            )
            family[index] = record
            self._write_unlocked(generation + 1, attempts, cycles)
            return record

    def _verify_published_cycle(
        self,
        pr_number: int,
        record: RebaseCycleRecord,
        remote: str | None,
    ) -> RebaseCycleRecord:
        """Verify a published cycle matches the remote head."""
        if remote != record.published_head_sha:
            raise ProtectedMergeUncertainError(
                f"published rebase cycle {record.index} for PR #{pr_number} "
                "does not match the remote head"
            )
        return record

    def _verify_prepared_cycle(
        self,
        pr_number: int,
        record: RebaseCycleRecord,
        remote: str | None,
        bridge_validator: Callable[[str], tuple[int, int, str, str, str]],
    ) -> tuple[RebaseCycleRecord, bool]:
        """Verify a prepared cycle against the remote.

        Returns the verified record and a boolean indicating if it was upgraded
        to published.
        """
        if remote == record.bridge_sha:
            try:
                bridge_sha: str = record.bridge_sha  # type: ignore[assignment]
                validated = bridge_validator(bridge_sha)
                v_pr, v_cycle, v_base, v_prev, v_tip = validated
                if (
                    v_pr != pr_number
                    or v_cycle != record.index + 1
                    or v_base != record.requested_base_sha
                    or v_prev != record.requested_head_sha
                    or v_tip != record.logical_tip_sha
                ):
                    raise ProtectedMergeUncertainError(
                        f"bridge metadata mismatch for cycle {record.index} on PR #{pr_number}"
                    )
            except ProtectedMergeValidationError:
                raise
            except Exception as exc:
                raise ProtectedMergeUncertainError(
                    f"bridge metadata validation failed for cycle "
                    f"{record.index} on PR #{pr_number}: {exc}"
                ) from exc
            upgraded = RebaseCycleRecord(
                index=record.index,
                requested_head_sha=record.requested_head_sha,
                requested_base_sha=record.requested_base_sha,
                state="published",
                published_head_sha=record.bridge_sha,
                bridge_sha=record.bridge_sha,
                logical_tip_sha=record.logical_tip_sha,
            )
            return upgraded, True
        if remote == record.requested_head_sha:
            return record, False
        raise ProtectedMergeUncertainError(
            f"prepared rebase cycle {record.index} for PR #{pr_number} "
            "neither matches the requested head nor the bridge on the remote"
        )

    def reconcile_on_open(
        self,
        remote_head_lookup: Callable[[int], str | None],
        bridge_validator: Callable[[str], tuple[int, int, str, str, str]],
    ) -> tuple[RebaseCycleRecord, ...]:
        """Verify non-terminal cycles against the remote without any mutation.

        A ``published`` cycle must equal the remote head.  A ``prepared``
        cycle publishes nothing: it confirms that the remote still holds the
        requested head, upgrades to published when the remote already holds
        the bridge and the bridge metadata validates, or fails closed.  A
        ``requested`` cycle has no prepared bridge by construction and is
        uncertain.

        Args:
            remote_head_lookup: Callable that returns the remote head SHA for a PR number.
            bridge_validator: Required callable that takes a bridge SHA and returns
                a tuple of (pr_number, cycle, base_sha, previous_head_sha, logical_tip_sha)
                parsed from the bridge commit message. The metadata is validated against
                the stored cycle before any upgrade from prepared to published.

        """
        with self._locked():
            generation, attempts, cycles = self._read_unlocked()
            verified: list[RebaseCycleRecord] = []
            changed = False
            for pr_number, family in cycles.items():
                for position, record in enumerate(family):
                    if record.state == "failed":
                        continue
                    if record.state == "requested":
                        raise ProtectedMergeUncertainError(
                            f"rebase cycle {record.index} for PR #{pr_number} was requested "
                            "but no prepared bridge is recorded"
                        )
                    remote = remote_head_lookup(pr_number)
                    if record.state == "published":
                        verified.append(self._verify_published_cycle(pr_number, record, remote))
                        continue
                    if record.state == "prepared":
                        upgraded_record, was_upgraded = self._verify_prepared_cycle(
                            pr_number, record, remote, bridge_validator
                        )
                        if was_upgraded:
                            family[position] = upgraded_record
                            changed = True
                        verified.append(upgraded_record)
                        continue
            if changed:
                self._write_unlocked(generation + 1, attempts, cycles)
            return tuple(verified)
