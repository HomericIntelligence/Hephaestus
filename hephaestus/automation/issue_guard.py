"""Durable, lease-backed ownership guards for issue-scoped automation.

The visible ``state:in-progress`` label is intentionally only a contention
signal.  The authoritative claim is a compare-and-confirm state machine stored
in no-tree-change commits on the issue's implementation branch. Branch updates
are non-forced, so two children of the same observed record cannot both become
the current owner.
"""
# The store implements a deliberately broad public protocol surface; its
# individual methods mirror transport operations and are documented by the
# protocol above.
# ruff: noqa: D102, D105, D107

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol, Self

from hephaestus.automation.state_labels import (
    ALL_STATE_LABELS,
    STATE_IN_PROGRESS,
)
from hephaestus.github.client import gh_call

logger = logging.getLogger(__name__)

_BASE_LEASE = timedelta(hours=4)
_RENEW_BEFORE = timedelta(minutes=30)
_RECOVERY_GRACE = timedelta(minutes=10)
_SHUTDOWN_MARGIN = timedelta(minutes=5)
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
_REPOSITORY_RE = re.compile(r"[^/\s]+/[^/\s]+\Z")
_GUARD_REASON_MAX = 512
_REF_READBACK_ATTEMPTS = 4
_REF_READBACK_DELAY_S = 0.25
_SIGNATURE_READBACK_ATTEMPTS = 4
_GUARD_HISTORY_LIMIT = 4096
_COMPARE_PAGE_SIZE = 100
_GUARD_COMMIT_SUBJECT = "chore(automation): record issue guard state"
_GUARD_COMMIT_SIGNOFF = (
    "Signed-off-by: Hephaestus Automation <hephaestus-automation@users.noreply.github.com>"
)


class GuardError(RuntimeError):
    """Base class for a failed or unavailable issue guard."""


class GuardUnavailableError(GuardError):
    """GitHub could not provide enough evidence to proceed safely."""


class GuardLostError(GuardError):
    """The current process no longer owns the durable guard."""


class GuardConflictError(GuardError):
    """A non-forced ref operation lost a compare-and-swap race."""


class GuardPhase(StrEnum):
    """Phases recorded in the implementation branch history."""

    ACQUIRING = "acquiring"
    ACTIVE = "active"
    RELEASING = "releasing"
    RELEASED = "released"
    RECOVERING = "recovering"
    RECOVERED = "recovered"


def _require_uuid(value: uuid.UUID, field: str) -> None:
    if not isinstance(value, uuid.UUID):
        raise ValueError(f"{field} must be a UUID")


def _require_sha(value: str, field: str) -> None:
    if not isinstance(value, str) or _FULL_SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a full lowercase commit SHA")


def _require_branch(value: str, field: str = "branch") -> None:
    """Reject branch names that cannot safely identify a production ref."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or value.startswith("refs/")
        or ".." in value
        or "@{" in value
        or any(char.isspace() or char in "~^:?*[\\" for char in value)
    ):
        raise ValueError(f"{field} must be a valid branch name")


def normalize_repository(repository: str) -> str:
    """Validate and return a canonical ``OWNER/REPO`` target."""
    if not isinstance(repository, str) or _REPOSITORY_RE.fullmatch(repository) is None:
        raise ValueError("repository must be an OWNER/REPO slug")
    owner, name = repository.split("/", 1)
    return f"{owner}/{name}"


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("lease_expires_at must be timezone-aware")
    return value.astimezone(UTC)


def _encode_time(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _decode_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("lease_expires_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("lease_expires_at is not valid ISO-8601") from exc
    return _utc(parsed)


@dataclass(frozen=True)
class GuardRecord:
    """Strict version-one record stored in an implementation-branch commit."""

    version: Literal[1]
    repository: str
    issue: int
    claim_id: uuid.UUID
    run_id: uuid.UUID
    actor: str
    phase: GuardPhase
    work_stage: str
    lease_expires_at: datetime
    predecessor_oid: str | None
    reason: str

    def __post_init__(self) -> None:
        """Reject ambiguous records before they can become lock authority."""
        if type(self.version) is not int or self.version != 1:
            raise ValueError("guard record version must be 1")
        object.__setattr__(self, "repository", normalize_repository(self.repository))
        if isinstance(self.issue, bool) or not isinstance(self.issue, int) or self.issue <= 0:
            raise ValueError("issue must be a positive integer")
        _require_uuid(self.claim_id, "claim_id")
        _require_uuid(self.run_id, "run_id")
        if not isinstance(self.actor, str) or not self.actor.strip():
            raise ValueError("actor must be non-empty")
        if not isinstance(self.phase, GuardPhase):
            raise ValueError("phase must be a GuardPhase")
        if not isinstance(self.work_stage, str) or not self.work_stage.strip():
            raise ValueError("work_stage must be non-empty")
        object.__setattr__(self, "lease_expires_at", _utc(self.lease_expires_at))
        if self.predecessor_oid is not None:
            _require_sha(self.predecessor_oid, "predecessor_oid")
        if not isinstance(self.reason, str) or len(self.reason) > _GUARD_REASON_MAX:
            raise ValueError("reason must be a bounded string")

    def to_json(self) -> str:
        """Return the canonical JSON representation of this record."""
        return json.dumps(
            {
                "actor": self.actor,
                "claim_id": str(self.claim_id),
                "issue": self.issue,
                "lease_expires_at": _encode_time(self.lease_expires_at),
                "phase": self.phase.value,
                "reason": self.reason,
                "repository": self.repository,
                "run_id": str(self.run_id),
                "version": self.version,
                "work_stage": self.work_stage,
                "predecessor_oid": self.predecessor_oid,
            },
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def to_commit_message(self) -> str:
        """Return canonical guard text wrapped in a PR-policy-compliant message."""
        return f"{_GUARD_COMMIT_SUBJECT}\n\n{self.to_json()}\n\n{_GUARD_COMMIT_SIGNOFF}"

    @classmethod
    def from_json(cls, value: str) -> Self:
        """Parse only exact canonical JSON with the version-one schema."""
        if not isinstance(value, str):
            raise ValueError("guard record JSON must be a string")
        try:
            parsed = json.loads(value)
            canonical = json.dumps(parsed, allow_nan=False, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("guard record is not valid JSON") from exc
        if canonical != value or not isinstance(parsed, dict):
            raise ValueError("guard record JSON is not canonical")
        expected = {
            "actor",
            "claim_id",
            "issue",
            "lease_expires_at",
            "phase",
            "predecessor_oid",
            "reason",
            "repository",
            "run_id",
            "version",
            "work_stage",
        }
        if set(parsed) != expected:
            raise ValueError("guard record has an unexpected schema")
        try:
            record = cls(
                version=parsed["version"],
                repository=parsed["repository"],
                issue=parsed["issue"],
                claim_id=uuid.UUID(parsed["claim_id"]),
                run_id=uuid.UUID(parsed["run_id"]),
                actor=parsed["actor"],
                phase=GuardPhase(parsed["phase"]),
                work_stage=parsed["work_stage"],
                lease_expires_at=_decode_time(parsed["lease_expires_at"]),
                predecessor_oid=parsed["predecessor_oid"],
                reason=parsed["reason"],
            )
            if _encode_time(record.lease_expires_at) != parsed["lease_expires_at"]:
                raise ValueError("lease_expires_at is not canonical")
            return record
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("guard record fields are invalid") from exc

    @classmethod
    def from_commit_message(cls, value: str) -> Self:
        """Parse the current commit envelope or a legacy raw-JSON record."""
        if not isinstance(value, str):
            raise ValueError("guard commit message must be a string")
        if value.startswith("{"):
            return cls.from_json(value)
        prefix = f"{_GUARD_COMMIT_SUBJECT}\n\n"
        suffix = f"\n\n{_GUARD_COMMIT_SIGNOFF}"
        if not value.startswith(prefix) or not value.endswith(suffix):
            raise ValueError("guard commit message has an invalid envelope")
        return cls.from_json(value[len(prefix) : -len(suffix)])


@dataclass(frozen=True)
class GuardCredential:
    """The immutable authority identity passed across worker boundaries."""

    repository: str
    issue: int
    claim_id: uuid.UUID
    run_id: uuid.UUID
    branch: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository", normalize_repository(self.repository))
        if isinstance(self.issue, bool) or not isinstance(self.issue, int) or self.issue <= 0:
            raise ValueError("issue must be a positive integer")
        _require_uuid(self.claim_id, "claim_id")
        _require_uuid(self.run_id, "run_id")
        _require_branch(self.branch)


@dataclass(frozen=True)
class GuardSnapshot:
    """Current ref state and its GitHub server-time observation."""

    oid: str
    record: GuardRecord
    tree: str
    server_time: datetime

    def __post_init__(self) -> None:
        _require_sha(self.oid, "oid")
        _require_sha(self.tree, "tree")
        object.__setattr__(self, "server_time", _utc(self.server_time))


@dataclass(frozen=True)
class GuardHandle:
    """A confirmed owner handle retained by a coordinator work item."""

    credential: GuardCredential
    oid: str
    record: GuardRecord
    plan_labels: frozenset[str]

    def __post_init__(self) -> None:
        _require_sha(self.oid, "oid")
        if self.record.claim_id != self.credential.claim_id:
            raise ValueError("handle record and credential claim differ")
        if self.record.run_id != self.credential.run_id:
            raise ValueError("handle record and credential run differ")


class GuardStore(Protocol):
    """Storage and issue-label operations required by :class:`IssueGuard`."""

    def read_labels(self, repository: str, issue: int) -> Sequence[str]:
        raise NotImplementedError

    def add_label(self, repository: str, issue: int, label: str) -> None:
        raise NotImplementedError

    def remove_label(self, repository: str, issue: int, label: str) -> None:
        raise NotImplementedError

    def bind_branch(self, branch: str) -> None:
        raise NotImplementedError

    def read_ref(self, repository: str, issue: int) -> GuardSnapshot | None:
        raise NotImplementedError

    def default_tip(self, repository: str) -> tuple[str, str]:
        raise NotImplementedError

    def create_commit(
        self, repository: str, tree: str, parents: Sequence[str], message: str
    ) -> tuple[str, datetime]:
        raise NotImplementedError

    def create_ref(
        self,
        repository: str,
        issue: int,
        oid: str,
        *,
        expected_oid: str | None = None,
    ) -> None:
        raise NotImplementedError

    def update_ref(self, repository: str, issue: int, oid: str, expected_oid: str) -> None:
        raise NotImplementedError

    def actor(self) -> str:
        raise NotImplementedError


def _server_now(store: GuardStore) -> datetime:
    """Read a server-derived clock when the store exposes one."""
    method = getattr(store, "server_now", None)
    if not callable(method):
        raise GuardUnavailableError("guard store has no server-time source")
    value = method()
    return _utc(value)


class IssueGuard:
    """Acquire, confirm, renew, release, and recover one issue guard."""

    def __init__(
        self,
        store: GuardStore,
        *,
        run_id: uuid.UUID | None = None,
        actor: str | None = None,
        branch: str | None = None,
    ) -> None:
        self.store = store
        self.run_id = run_id or uuid.uuid4()
        self.actor = actor
        configured_branch = branch or getattr(store, "branch_name", None)
        self.branch = configured_branch if isinstance(configured_branch, str) else ""
        if self.branch:
            _require_branch(self.branch)
            self.store.bind_branch(self.branch)

    def bind_branch(self, branch: str) -> None:
        """Bind this service to the exact implementation branch."""
        _require_branch(branch)
        self.branch = branch
        self.store.bind_branch(branch)

    def _branch(self) -> str:
        if not self.branch:
            raise GuardUnavailableError("issue guard requires the implementation branch")
        _require_branch(self.branch)
        return self.branch

    def _actor(self) -> str:
        actor = self.actor or self.store.actor()
        if not isinstance(actor, str) or not actor.strip():
            raise GuardUnavailableError("authenticated GitHub actor is unavailable")
        return actor

    def _credential(self, record: GuardRecord) -> GuardCredential:
        return GuardCredential(
            record.repository,
            record.issue,
            record.claim_id,
            record.run_id,
            self._branch(),
        )

    @staticmethod
    def _lease_for(minimum_valid_for: timedelta) -> timedelta:
        if minimum_valid_for < timedelta(0):
            raise ValueError("minimum_valid_for must not be negative")
        return max(_BASE_LEASE, minimum_valid_for + _RECOVERY_GRACE + _SHUTDOWN_MARGIN)

    def _child(
        self,
        repository: str,
        issue: int,
        previous: GuardSnapshot,
        record: GuardRecord,
    ) -> GuardSnapshot:
        oid, _server_time = self.store.create_commit(
            repository, previous.tree, [previous.oid], record.to_commit_message()
        )
        try:
            self.store.update_ref(repository, issue, oid, previous.oid)
        except GuardConflictError:
            raise
        except Exception as exc:
            raise GuardConflictError(
                "implementation branch update lost its compare-and-swap"
            ) from exc
        for attempt in range(_REF_READBACK_ATTEMPTS):
            current = self.store.read_ref(repository, issue)
            if current is not None and current.oid == oid and current.record == record:
                return current
            if attempt + 1 < _REF_READBACK_ATTEMPTS:
                time.sleep(_REF_READBACK_DELAY_S)
        raise GuardLostError("implementation branch read-back did not confirm the child record")

    def acquire(  # noqa: C901
        self, repository: str, issue: int, work_stage: str
    ) -> GuardHandle | None:
        """Claim an issue, returning ``None`` when another claim owns it."""
        self._branch()
        repository = normalize_repository(repository)
        if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0:
            raise ValueError("issue must be a positive integer")
        if not isinstance(work_stage, str) or not work_stage.strip():
            raise ValueError("work_stage must be non-empty")
        labels = set(self.store.read_labels(repository, issue))
        if STATE_IN_PROGRESS in labels:
            return None
        current = self.store.read_ref(repository, issue)
        if current is not None and (
            current.record.repository != repository
            or current.record.issue != issue
            or current.record.phase not in {GuardPhase.RELEASED, GuardPhase.RECOVERED}
            or STATE_IN_PROGRESS in labels
        ):
            return None
        now = _server_now(self.store)
        tree: str
        parents: list[str]
        if current is None:
            base_oid, tree = self.store.default_tip(repository)
            _require_sha(base_oid, "default branch tip")
            _require_sha(tree, "default branch tree")
            parents = [base_oid]
            predecessor = base_oid
        else:
            tree = current.tree
            parents = [current.oid]
            predecessor = current.oid
        claim_id = uuid.uuid4()
        record = GuardRecord(
            version=1,
            repository=repository,
            issue=issue,
            claim_id=claim_id,
            run_id=self.run_id,
            actor=self._actor(),
            phase=GuardPhase.ACQUIRING,
            work_stage=work_stage,
            lease_expires_at=now + self._lease_for(timedelta(0)),
            predecessor_oid=predecessor,
            reason="automation acquisition",
        )
        oid, _ = self.store.create_commit(repository, tree, parents, record.to_commit_message())
        try:
            if current is None:
                self.store.create_ref(repository, issue, oid, expected_oid=parents[0])
            else:
                self.store.update_ref(repository, issue, oid, current.oid)
        except GuardConflictError:
            return None
        except Exception as exc:
            # GitHub returns 409/422 for an already-created ref.  Do not infer
            # that this process won; a fresh read is the only safe outcome.
            logger.info("issue guard acquisition lost for %s#%s: %s", repository, issue, exc)
            return None
        acquired = self.store.read_ref(repository, issue)
        if acquired is None or acquired.oid != oid or acquired.record != record:
            return None
        try:
            self.store.add_label(repository, issue, STATE_IN_PROGRESS)
            labels = set(self.store.read_labels(repository, issue))
            if STATE_IN_PROGRESS not in labels:
                raise GuardUnavailableError("guard label read-back was absent")
            active = replace_record(
                record,
                phase=GuardPhase.ACTIVE,
                predecessor_oid=oid,
                reason="automation claim active",
            )
            active_snapshot = self._child(repository, issue, acquired, active)
            confirmed = self.store.read_ref(repository, issue)
            final_labels = set(self.store.read_labels(repository, issue))
            if (
                confirmed is None
                or confirmed.oid != active_snapshot.oid
                or confirmed.record != active
                or STATE_IN_PROGRESS not in final_labels
            ):
                raise GuardLostError("active issue guard read-back failed")
            return GuardHandle(
                credential=self._credential(active),
                oid=confirmed.oid,
                record=active,
                plan_labels=frozenset(final_labels.intersection(ALL_STATE_LABELS)),
            )
        except Exception:
            # Best effort only: a failed read-back must never clear a claim
            # after ownership has become ambiguous.
            logger.warning(
                "issue guard acquisition left recoverable state for %s#%s", repository, issue
            )
            raise

    def confirm(self, credential: GuardCredential, minimum_valid_for: timedelta) -> GuardHandle:
        """Re-read and prove ownership before a dispatch or mutation."""
        repository = normalize_repository(credential.repository)
        self.bind_branch(credential.branch)
        current = self.store.read_ref(repository, credential.issue)
        labels = set(self.store.read_labels(repository, credential.issue))
        if current is None or current.record.phase is not GuardPhase.ACTIVE:
            raise GuardLostError("issue guard is not active")
        if not self._owns(current.record, credential) or STATE_IN_PROGRESS not in labels:
            raise GuardLostError("issue guard ownership confirmation failed")
        now = _server_now(self.store)
        if current.record.lease_expires_at <= now + minimum_valid_for:
            raise GuardLostError("issue guard lease is expired or too short")
        return GuardHandle(
            credential=credential,
            oid=current.oid,
            record=current.record,
            plan_labels=frozenset(labels.intersection(ALL_STATE_LABELS)),
        )

    @staticmethod
    def _owns(record: GuardRecord, credential: GuardCredential) -> bool:
        return (
            record.repository == credential.repository
            and record.issue == credential.issue
            and record.claim_id == credential.claim_id
            and record.run_id == credential.run_id
        )

    def renew(self, handle: GuardHandle, minimum_valid_for: timedelta) -> GuardHandle:
        """Extend an owner lease through a non-forced ref child update."""
        confirmed = self.confirm(handle.credential, timedelta(0))
        now = _server_now(self.store)
        if confirmed.record.lease_expires_at > now + minimum_valid_for:
            return confirmed
        record = replace_record(
            confirmed.record,
            phase=GuardPhase.ACTIVE,
            predecessor_oid=confirmed.oid,
            lease_expires_at=now + self._lease_for(minimum_valid_for),
            reason="automation lease renewed",
        )
        current = self.store.read_ref(handle.credential.repository, handle.credential.issue)
        if current is None or current.oid != confirmed.oid:
            raise GuardLostError("guard changed before renewal")
        self._child(handle.credential.repository, handle.credential.issue, current, record)
        return self.confirm(handle.credential, minimum_valid_for)

    def release(self, handle: GuardHandle, reason: str) -> None:
        """Release only the active claim represented by *handle*."""
        if not isinstance(reason, str) or not reason.strip() or len(reason) > _GUARD_REASON_MAX:
            raise ValueError("release reason must be a bounded non-empty string")
        repository = handle.credential.repository
        issue = handle.credential.issue
        current = self.store.read_ref(repository, issue)
        labels = set(self.store.read_labels(repository, issue))
        if (
            current is None
            or not self._owns(current.record, handle.credential)
            or STATE_IN_PROGRESS not in labels
        ):
            raise GuardLostError("refusing to release a guard not owned by this run")
        resuming_release = (
            current.record.phase is GuardPhase.RELEASING
            and current.record.predecessor_oid == handle.oid
        )
        if not resuming_release and current.record.phase not in {
            GuardPhase.ACTIVE,
            GuardPhase.ACQUIRING,
        }:
            raise GuardLostError("refusing to release a guard not owned by this run")
        if current.record.lease_expires_at <= _server_now(self.store):
            raise GuardLostError("expired guard requires operator recovery")
        # A guarded stage may legitimately replace its plan label.  Compare
        # the labels immediately before removing ownership, not the snapshot
        # retained by the acquisition handle.
        before_release_plan = set(labels.intersection(ALL_STATE_LABELS))
        if resuming_release:
            releasing = current.record
            releasing_snapshot = current
        else:
            releasing = replace_record(
                current.record,
                phase=GuardPhase.RELEASING,
                predecessor_oid=current.oid,
                reason=reason,
            )
            releasing_snapshot = self._child(repository, issue, current, releasing)
        self.store.remove_label(repository, issue, STATE_IN_PROGRESS)
        after = set(self.store.read_labels(repository, issue))
        if (
            STATE_IN_PROGRESS in after
            or set(after.intersection(ALL_STATE_LABELS)) != before_release_plan
        ):
            raise GuardLostError("guard release label read-back failed")
        released = replace_record(
            releasing,
            phase=GuardPhase.RELEASED,
            predecessor_oid=releasing_snapshot.oid,
            lease_expires_at=releasing.lease_expires_at,
            reason=reason,
        )
        self._child(repository, issue, releasing_snapshot, released)

    def recover(
        self,
        repository: str,
        issue: int,
        *,
        expected_claim: uuid.UUID,
        expected_oid: str,
        reason: str,
        actor: str,
    ) -> GuardSnapshot:
        """Recover an expired claim with explicit expected-identity CAS."""
        repository = normalize_repository(repository)
        _require_sha(expected_oid, "expected_oid")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("recovery actor must be non-empty")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > _GUARD_REASON_MAX:
            raise ValueError("recovery reason must be a bounded non-empty string")
        current = self.store.read_ref(repository, issue)
        labels = set(self.store.read_labels(repository, issue))
        if current is None or current.oid != expected_oid:
            raise GuardConflictError("expected guard OID no longer matches")
        if current.record.claim_id != expected_claim:
            raise GuardConflictError("expected guard claim no longer matches")
        if current.record.phase not in {
            GuardPhase.ACTIVE,
            GuardPhase.ACQUIRING,
            GuardPhase.RELEASING,
        }:
            raise GuardError("guard is already terminal")
        now = _server_now(self.store)
        if now <= current.record.lease_expires_at + _RECOVERY_GRACE:
            raise GuardError("guard recovery grace period has not elapsed")
        # An abandoned acquisition can be recovered before its guard label is
        # added; preserve whatever plan-state label was live at recovery time.
        before_recovery_plan = set(labels.intersection(ALL_STATE_LABELS))
        recovering = replace_record(
            current.record,
            phase=GuardPhase.RECOVERING,
            predecessor_oid=current.oid,
            reason=reason,
        )
        recovering_snapshot = self._child(repository, issue, current, recovering)
        if STATE_IN_PROGRESS in labels:
            self.store.remove_label(repository, issue, STATE_IN_PROGRESS)
        after = set(self.store.read_labels(repository, issue))
        if (
            STATE_IN_PROGRESS in after
            or set(after.intersection(ALL_STATE_LABELS)) != before_recovery_plan
        ):
            raise GuardLostError("recovery label read-back failed")
        recovered = replace_record(
            recovering,
            phase=GuardPhase.RECOVERED,
            predecessor_oid=recovering_snapshot.oid,
            actor=actor,
            reason=reason,
        )
        return self._child(repository, issue, recovering_snapshot, recovered)


def replace_record(record: GuardRecord, **changes: Any) -> GuardRecord:
    """Create a validated record replacement without exposing mutable state."""
    values: dict[str, Any] = {
        "version": record.version,
        "repository": record.repository,
        "issue": record.issue,
        "claim_id": record.claim_id,
        "run_id": record.run_id,
        "actor": record.actor,
        "phase": record.phase,
        "work_stage": record.work_stage,
        "lease_expires_at": record.lease_expires_at,
        "predecessor_oid": record.predecessor_oid,
        "reason": record.reason,
    }
    values.update(changes)
    return GuardRecord(**values)


class InMemoryGuardStore:
    """Deterministic store useful for unit tests and local protocol checks."""

    def __init__(
        self,
        repository: str = "Owner/Repo",
        *,
        actor: str = "automation",
        branch: str = "test-auto-impl",
    ) -> None:
        self.repository = normalize_repository(repository)
        _require_branch(branch)
        self.branch_name = branch
        self.labels: dict[tuple[str, int], set[str]] = {}
        self.refs: dict[tuple[str, int], GuardSnapshot] = {}
        self.now = datetime.now(UTC)
        self.actor_name = actor
        self._counter = 0
        self._commits: dict[str, tuple[GuardRecord | None, str, str]] = {}

    def _sha(self) -> str:
        self._counter += 1
        return f"{self._counter:040x}"

    def server_now(self) -> datetime:
        return self.now

    def read_labels(self, repository: str, issue: int) -> Sequence[str]:
        return tuple(self.labels.setdefault((repository, issue), set()))

    def add_label(self, repository: str, issue: int, label: str) -> None:
        self.labels.setdefault((repository, issue), set()).add(label)

    def remove_label(self, repository: str, issue: int, label: str) -> None:
        self.labels.setdefault((repository, issue), set()).discard(label)

    def bind_branch(self, branch: str) -> None:
        _require_branch(branch)
        self.branch_name = branch

    def read_ref(self, repository: str, issue: int) -> GuardSnapshot | None:
        return self.refs.get((repository, issue))

    def default_tip(self, repository: str) -> tuple[str, str]:
        oid, tree = self._sha(), self._sha()
        self._commits[oid] = (None, tree, "")
        return oid, tree

    def create_commit(
        self, repository: str, tree: str, parents: Sequence[str], message: str
    ) -> tuple[str, datetime]:
        oid = self._sha()
        record = GuardRecord.from_commit_message(message)
        self._commits[oid] = (record, tree, message)
        return oid, self.now

    def create_ref(
        self,
        repository: str,
        issue: int,
        oid: str,
        *,
        expected_oid: str | None = None,
    ) -> None:
        key = (repository, issue)
        if key in self.refs and expected_oid != self.refs[key].oid:
            raise GuardConflictError("ref already exists")
        record, tree, _message = self._commits[oid]
        if record is None:
            raise GuardUnavailableError("ref target is not a guard commit")
        self.refs[key] = GuardSnapshot(oid, record, tree, self.now)

    def update_ref(self, repository: str, issue: int, oid: str, expected_oid: str) -> None:
        current = self.refs.get((repository, issue))
        if current is None or current.oid != expected_oid:
            raise GuardConflictError("ref changed")
        record, tree, _message = self._commits[oid]
        if record is None:
            raise GuardUnavailableError("ref target is not a guard commit")
        self.refs[(repository, issue)] = GuardSnapshot(
            oid=oid,
            record=record,
            tree=tree,
            server_time=self.now,
        )

    def actor(self) -> str:
        return self.actor_name


def assert_recovery_secret_absent(environ: Mapping[str, str] | None = None) -> None:
    """Reject normal automation when an operator recovery credential leaked in."""
    values = os.environ if environ is None else environ
    if values.get("HEPHAESTUS_GUARD_RECOVERY_TOKEN"):
        raise GuardUnavailableError(
            "operator recovery credentials must not be present in normal automation"
        )


def _parse_http_response(stdout: str) -> tuple[int, dict[str, str], dict[str, Any] | None]:
    """Parse ``gh api --include`` output and require a usable server date."""
    matches = list(re.finditer(r"^HTTP/\S+\s+(\d{3})\b", stdout, re.MULTILINE))
    if not matches:
        raise GuardUnavailableError("GitHub response did not include an HTTP status")
    start = matches[-1].start()
    header_end = re.search(r"\r?\n\r?\n", stdout[start:])
    if header_end is None:
        raise GuardUnavailableError("GitHub response did not include complete headers")
    header_text = stdout[start : start + header_end.start()]
    headers: dict[str, str] = {}
    for line in header_text.splitlines()[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    date = headers.get("date")
    if not date:
        raise GuardUnavailableError("GitHub response did not include a Date header")
    try:
        parsedate_to_datetime(date).astimezone(UTC)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GuardUnavailableError("GitHub Date header was invalid") from exc
    body_text = stdout[start + header_end.end() :].strip()
    try:
        body = json.loads(body_text) if body_text else None
    except json.JSONDecodeError as exc:
        raise GuardUnavailableError("GitHub response body was invalid JSON") from exc
    return int(matches[-1].group(1)), headers, body if isinstance(body, dict) else None


class GitHubIssueGuardStore:
    """GitHub REST implementation of :class:`GuardStore`."""

    def __init__(
        self,
        repository: str,
        *,
        branch: str | None = None,
        call: Callable[..., subprocess.CompletedProcess[str]] = gh_call,
        git_call: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        env: Mapping[str, str] | None = None,
        staging_root: Path | None = None,
    ) -> None:
        self.repository = normalize_repository(repository)
        self.branch_name = branch
        if branch is not None:
            _require_branch(branch)
        self._call = call
        self._git_call = git_call
        self._env = dict(env) if env is not None else None
        self._staging_root = staging_root or Path.cwd() / "build"
        self._last_server_time: datetime | None = None
        self._staged_commit: (
            tuple[str, tuple[str, ...], tempfile.TemporaryDirectory[str]] | None
        ) = None

    def _request(self, args: list[str], *, check: bool = True) -> tuple[int, dict[str, Any] | None]:
        result = self._call(
            ["api", "--include", *args],
            check=check,
            retry_on_rate_limit=False,
            max_retries=1,
            env=self._env,
        )
        status, _headers, body = _parse_http_response(result.stdout or "")
        # The parser intentionally does not make Date an object-level field;
        # retain the server clock for lease decisions and test inspection.
        date = _headers["date"]
        self._last_server_time = parsedate_to_datetime(date).astimezone(UTC)
        return status, body

    def server_now(self) -> datetime:
        if self._last_server_time is None:
            raise GuardUnavailableError("no server-time observation is available")
        return self._last_server_time

    def _path(self, suffix: str) -> str:
        root = f"repos/{self.repository}"
        return f"{root}/{suffix}" if suffix else root

    def bind_branch(self, branch: str) -> None:
        """Bind all guard operations to the implementation branch."""
        _require_branch(branch)
        self.branch_name = branch

    def _branch(self) -> str:
        branch = self.branch_name
        if not isinstance(branch, str) or not branch.strip():
            raise GuardUnavailableError("guard store has no implementation branch")
        _require_branch(branch)
        return branch

    def _ref_oid(self) -> str | None:
        """Read the implementation branch tip, returning ``None`` if absent."""
        status, body = self._request(
            [self._path(f"git/ref/heads/{self._branch()}")],
            check=False,
        )
        if status == 404:
            return None
        if status != 200 or not isinstance(body, dict):
            raise GuardUnavailableError("implementation branch response was not successful")
        obj = body.get("object")
        oid = obj.get("sha") if isinstance(obj, dict) else None
        if not isinstance(oid, str):
            raise GuardUnavailableError("implementation branch response was malformed")
        return oid

    def read_labels(self, repository: str, issue: int) -> Sequence[str]:
        status, body = self._request([self._path(f"issues/{issue}")])
        if status != 200 or not isinstance(body, dict):
            raise GuardUnavailableError("issue label response was not successful")
        labels = body.get("labels")
        if not isinstance(labels, list):
            raise GuardUnavailableError("issue label response was malformed")
        return tuple(
            str(label["name"])
            for label in labels
            if isinstance(label, dict) and isinstance(label.get("name"), str)
        )

    def add_label(self, repository: str, issue: int, label: str) -> None:
        self._request(
            [
                "--method",
                "POST",
                self._path(f"issues/{issue}/labels"),
                "-f",
                f"labels[]={label}",
            ]
        )

    def remove_label(self, repository: str, issue: int, label: str) -> None:
        self._request(["--method", "DELETE", self._path(f"issues/{issue}/labels/{label}")])

    def _commit_metadata(
        self, repository: str, oid: str
    ) -> tuple[str, str, tuple[str, ...], datetime]:
        status, body = self._request([self._path(f"git/commits/{oid}")])
        if status != 200 or not isinstance(body, dict):
            raise GuardUnavailableError("implementation branch commit response was not successful")
        tree = body.get("tree")
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        message = body.get("message")
        parents_data = body.get("parents")
        parents: list[str] = []
        if isinstance(parents_data, list):
            for parent in parents_data:
                parent_sha = parent.get("sha") if isinstance(parent, dict) else None
                if isinstance(parent_sha, str):
                    parents.append(parent_sha)
        if not isinstance(tree_sha, str) or not isinstance(message, str):
            raise GuardUnavailableError("implementation branch commit response was malformed")
        return tree_sha, message, tuple(parents), self.server_now()

    def _guard_record(self, oid: str, head_message: str) -> GuardRecord | None:
        """Find the newest guard record without walking shared main history."""
        record = self._record_from_message(head_message)
        if record is not None:
            return record
        for candidate in reversed(self._commits_since_default(oid)):
            commit = candidate.get("commit") if isinstance(candidate, dict) else None
            candidate_message = commit.get("message") if isinstance(commit, dict) else None
            if not isinstance(candidate_message, str):
                raise GuardUnavailableError("branch comparison commit metadata was malformed")
            record = self._record_from_message(candidate_message)
            if record is not None:
                return record
        return None

    @staticmethod
    def _record_from_message(message: str) -> GuardRecord | None:
        try:
            return GuardRecord.from_commit_message(message)
        except ValueError:
            if message.lstrip().startswith("{") or message.startswith(_GUARD_COMMIT_SUBJECT):
                raise GuardUnavailableError(
                    "implementation branch contains a malformed guard record"
                ) from None
            return None

    def _default_branch_oid(self) -> str:
        status, body = self._request([self._path("")])
        branch = body.get("default_branch") if isinstance(body, dict) else None
        if status != 200 or not isinstance(branch, str) or not branch:
            raise GuardUnavailableError("repository default branch metadata was malformed")
        status, body = self._request([self._path(f"git/ref/heads/{branch}")])
        obj = body.get("object") if isinstance(body, dict) else None
        oid = obj.get("sha") if isinstance(obj, dict) else None
        if status != 200 or not isinstance(oid, str):
            raise GuardUnavailableError("default branch ref was malformed")
        _require_sha(oid, "default branch tip")
        return oid

    def _commits_since_default(self, oid: str) -> list[dict[str, Any]]:
        """Return branch-only commits through bounded compare pagination."""
        base_oid = self._default_branch_oid()
        if base_oid == oid:
            return []
        commits: list[dict[str, Any]] = []
        total: int | None = None
        page = 1
        while total is None or len(commits) < total:
            endpoint = f"compare/{base_oid}...{oid}?per_page={_COMPARE_PAGE_SIZE}&page={page}"
            status, body = self._request([self._path(endpoint)])
            page_commits = body.get("commits") if isinstance(body, dict) else None
            observed_total = body.get("total_commits") if isinstance(body, dict) else None
            if (
                status != 200
                or not isinstance(page_commits, list)
                or isinstance(observed_total, bool)
                or not isinstance(observed_total, int)
                or observed_total < 0
                or observed_total > _GUARD_HISTORY_LIMIT
                or (total is not None and observed_total != total)
            ):
                raise GuardUnavailableError("branch comparison response was malformed or too deep")
            total = observed_total
            commits.extend(item for item in page_commits if isinstance(item, dict))
            if len(commits) > total or (len(commits) < total and not page_commits):
                raise GuardUnavailableError("branch comparison pagination was incomplete")
            page += 1
        return commits

    def read_ref(self, repository: str, issue: int) -> GuardSnapshot | None:
        oid = self._ref_oid()
        if oid is None:
            return None
        tree, message, _parents, server_time = self._commit_metadata(repository, oid)
        record = self._guard_record(oid, message)
        if record is None:
            return None
        return GuardSnapshot(oid=oid, record=record, tree=tree, server_time=server_time)

    def default_tip(self, repository: str) -> tuple[str, str]:
        branch_oid = self._ref_oid()
        if branch_oid is not None:
            tree, _message, _parents, _server_time = self._commit_metadata(repository, branch_oid)
            return branch_oid, tree
        oid = self._default_branch_oid()
        tree, _message, _parents, _server_time = self._commit_metadata(repository, oid)
        return oid, tree

    def create_commit(
        self, repository: str, tree: str, parents: Sequence[str], message: str
    ) -> tuple[str, datetime]:
        """Stage a locally signed guard commit for an exact-lease push."""
        normalize_repository(repository)
        _require_sha(tree, "tree")
        parent_oids = tuple(parents)
        if not parent_oids:
            raise ValueError("guard commits require at least one parent")
        for parent in parent_oids:
            _require_sha(parent, "parent")
        GuardRecord.from_commit_message(message)
        if self._staged_commit is not None:
            raise GuardUnavailableError("a signed guard commit is already staged")

        self._staging_root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(
            prefix="hephaestus-issue-guard-", dir=self._staging_root
        )
        repository_path = Path(temporary.name)
        remote = f"https://github.com/{self.repository}.git"
        environment = os.environ.copy()
        if self._env is not None:
            environment.update(self._env)

        def run_git(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return self._git_call(
                args,
                check=True,
                capture_output=True,
                text=True,
                env=environment,
                **kwargs,
            )

        try:
            run_git(["git", "init", "--bare", str(repository_path)])
            run_git(["git", "-C", str(repository_path), "remote", "add", "origin", remote])
            run_git(
                [
                    "git",
                    "-C",
                    str(repository_path),
                    "fetch",
                    "--no-tags",
                    "--depth=1",
                    "origin",
                    *parent_oids,
                ]
            )
            commit_args = ["git", "-C", str(repository_path), "commit-tree", "-S", tree]
            for parent in parent_oids:
                commit_args.extend(["-p", parent])
            created = run_git(commit_args, input=message)
            oid = created.stdout.strip()
            _require_sha(oid, "signed guard commit")
            run_git(["git", "-C", str(repository_path), "verify-commit", oid])
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            temporary.cleanup()
            raise GuardUnavailableError("signed guard commit creation failed") from exc

        self._staged_commit = (oid, parent_oids, temporary)
        return oid, self.server_now()

    def create_ref(
        self,
        repository: str,
        issue: int,
        oid: str,
        *,
        expected_oid: str | None = None,
    ) -> None:
        current_oid = self._ref_oid()
        if current_oid is not None and current_oid != expected_oid:
            self._discard_staged_commit()
            raise GuardConflictError("implementation branch already exists or changed")
        lease = current_oid or ""
        self._publish_staged_commit(oid, lease)

    def update_ref(self, repository: str, issue: int, oid: str, expected_oid: str) -> None:
        _require_sha(expected_oid, "expected_oid")
        self._publish_staged_commit(oid, expected_oid)

    def _discard_staged_commit(self) -> None:
        staged = self._staged_commit
        self._staged_commit = None
        if staged is not None:
            staged[2].cleanup()

    def _publish_staged_commit(self, oid: str, expected_oid: str) -> None:
        """Push one staged commit with a server-enforced exact-head lease."""
        staged = self._staged_commit
        if staged is None or staged[0] != oid or expected_oid not in {"", *staged[1]}:
            self._discard_staged_commit()
            raise GuardUnavailableError("signed guard commit staging did not match the CAS")
        repository_path = Path(staged[2].name)
        branch_ref = f"refs/heads/{self._branch()}"
        environment = os.environ.copy()
        if self._env is not None:
            environment.update(self._env)
        try:
            self._push_staged_ref(
                repository_path,
                branch_ref,
                expected_oid=expected_oid,
                target_oid=oid,
                environment=environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            observed = self._ref_oid()
            self._discard_staged_commit()
            if observed != expected_oid:
                raise GuardConflictError(
                    "implementation branch changed before its signed CAS update"
                ) from exc
            raise GuardUnavailableError("signed guard commit push failed") from exc

        try:
            self._confirm_remote_signature(oid)
        except GuardUnavailableError:
            self._rollback_unverified_commit(
                repository_path,
                branch_ref,
                rejected_oid=oid,
                expected_oid=expected_oid,
                environment=environment,
            )
            raise
        finally:
            self._discard_staged_commit()

    def _push_staged_ref(
        self,
        repository_path: Path,
        branch_ref: str,
        *,
        expected_oid: str,
        target_oid: str,
        environment: Mapping[str, str],
    ) -> None:
        refspec = f"{target_oid}:{branch_ref}" if target_oid else f":{branch_ref}"
        self._git_call(
            [
                "git",
                "-C",
                str(repository_path),
                "push",
                f"--force-with-lease={branch_ref}:{expected_oid}",
                "origin",
                refspec,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )

    def _rollback_unverified_commit(
        self,
        repository_path: Path,
        branch_ref: str,
        *,
        rejected_oid: str,
        expected_oid: str,
        environment: Mapping[str, str],
    ) -> None:
        """Remove an unverified commit through a lease pinned to its exact OID."""
        try:
            self._push_staged_ref(
                repository_path,
                branch_ref,
                expected_oid=rejected_oid,
                target_oid=expected_oid,
                environment=environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            observed = self._ref_oid()
            if observed == (expected_oid or None):
                return
            if observed != rejected_oid:
                raise GuardConflictError(
                    "implementation branch changed before unverified guard rollback"
                ) from exc
            raise GuardUnavailableError(
                "unverified guard commit remained after rollback failed"
            ) from exc

    def _confirm_remote_signature(self, oid: str) -> None:
        """Require GitHub to verify the newly published guard signature."""
        for attempt in range(_SIGNATURE_READBACK_ATTEMPTS):
            status, body = self._request([self._path(f"git/commits/{oid}")])
            verification = body.get("verification") if isinstance(body, dict) else None
            if status == 200 and isinstance(verification, dict):
                if verification.get("verified") is True:
                    return
                reason = verification.get("reason")
                if reason not in {"gpgverify_unavailable"}:
                    raise GuardUnavailableError(
                        f"GitHub rejected the guard commit signature: {reason or 'unknown'}"
                    )
            if attempt + 1 < _SIGNATURE_READBACK_ATTEMPTS:
                time.sleep(_REF_READBACK_DELAY_S)
        raise GuardUnavailableError("GitHub did not verify the guard commit signature")

    def actor(self) -> str:
        status, body = self._request(["user"])
        login = body.get("login") if isinstance(body, dict) else None
        if status != 200 or not isinstance(login, str) or not login:
            raise GuardUnavailableError("authenticated GitHub actor was unavailable")
        return login


__all__ = [
    "_BASE_LEASE",
    "_RECOVERY_GRACE",
    "_RENEW_BEFORE",
    "_SHUTDOWN_MARGIN",
    "GitHubIssueGuardStore",
    "GuardConflictError",
    "GuardCredential",
    "GuardError",
    "GuardHandle",
    "GuardLostError",
    "GuardPhase",
    "GuardRecord",
    "GuardSnapshot",
    "GuardUnavailableError",
    "InMemoryGuardStore",
    "IssueGuard",
    "assert_recovery_secret_absent",
    "normalize_repository",
    "replace_record",
]
