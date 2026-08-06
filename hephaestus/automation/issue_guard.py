"""Durable, lease-backed ownership guards for issue-scoped automation.

The visible ``state:in-progress`` label is intentionally only a contention
signal.  The authoritative claim is a compare-and-confirm state machine stored
in a per-issue Git ref.  Ref updates are non-forced, so two children of the
same observed record cannot both become the current owner.
"""
# The store implements a deliberately broad public protocol surface; its
# individual methods mirror transport operations and are documented by the
# protocol above.
# ruff: noqa: D102, D105, D107, E501

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
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
_GUARD_REF_PREFIX = "refs/heads/hephaestus/issue-guards/issue-"


class GuardError(RuntimeError):
    """Base class for a failed or unavailable issue guard."""


class GuardUnavailableError(GuardError):
    """GitHub could not provide enough evidence to proceed safely."""


class GuardLostError(GuardError):
    """The current process no longer owns the durable guard."""


class GuardConflictError(GuardError):
    """A non-forced ref operation lost a compare-and-swap race."""


class GuardPhase(StrEnum):
    """Phases recorded in the issue guard ref."""

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
    """Strict version-one record stored in a guard ref commit."""

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
        """Return the canonical JSON representation used as commit text."""
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


@dataclass(frozen=True)
class GuardCredential:
    """The immutable authority identity passed across worker boundaries."""

    repository: str
    issue: int
    claim_id: uuid.UUID
    run_id: uuid.UUID

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository", normalize_repository(self.repository))
        if isinstance(self.issue, bool) or not isinstance(self.issue, int) or self.issue <= 0:
            raise ValueError("issue must be a positive integer")
        _require_uuid(self.claim_id, "claim_id")
        _require_uuid(self.run_id, "run_id")


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

    def read_labels(self, repository: str, issue: int) -> Sequence[str]: ...

    def add_label(self, repository: str, issue: int, label: str) -> None: ...

    def remove_label(self, repository: str, issue: int, label: str) -> None: ...

    def read_ref(self, repository: str, issue: int) -> GuardSnapshot | None: ...

    def default_tip(self, repository: str) -> tuple[str, str]: ...

    def create_commit(
        self, repository: str, tree: str, parents: Sequence[str], message: str
    ) -> tuple[str, datetime]: ...

    def create_ref(self, repository: str, issue: int, oid: str) -> None: ...

    def update_ref(self, repository: str, issue: int, oid: str, expected_oid: str) -> None: ...

    def actor(self) -> str: ...


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
    ) -> None:
        self.store = store
        self.run_id = run_id or uuid.uuid4()
        self.actor = actor

    def _actor(self) -> str:
        actor = self.actor or self.store.actor()
        if not isinstance(actor, str) or not actor.strip():
            raise GuardUnavailableError("authenticated GitHub actor is unavailable")
        return actor

    @staticmethod
    def _credential(record: GuardRecord) -> GuardCredential:
        return GuardCredential(record.repository, record.issue, record.claim_id, record.run_id)

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
            repository, previous.tree, [previous.oid], record.to_json()
        )
        try:
            self.store.update_ref(repository, issue, oid, previous.oid)
        except GuardConflictError:
            raise
        except Exception as exc:
            raise GuardConflictError("guard ref update lost its compare-and-swap") from exc
        current = self.store.read_ref(repository, issue)
        if current is None or current.oid != oid or current.record != record:
            raise GuardLostError("guard ref read-back did not confirm the child record")
        return current

    def acquire(  # noqa: C901
        self, repository: str, issue: int, work_stage: str
    ) -> GuardHandle | None:
        """Claim an issue, returning ``None`` when another claim owns it."""
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
        oid, _ = self.store.create_commit(repository, tree, parents, record.to_json())
        try:
            if current is None:
                self.store.create_ref(repository, issue, oid)
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
            logger.warning("issue guard acquisition left recoverable state for %s#%s", repository, issue)
            raise

    def confirm(self, credential: GuardCredential, minimum_valid_for: timedelta) -> GuardHandle:
        """Re-read and prove ownership before a dispatch or mutation."""
        repository = normalize_repository(credential.repository)
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
            or current.oid != handle.oid
            or current.record.phase not in {GuardPhase.ACTIVE, GuardPhase.ACQUIRING}
            or not self._owns(current.record, handle.credential)
            or STATE_IN_PROGRESS not in labels
        ):
            raise GuardLostError("refusing to release a guard not owned by this run")
        if current.record.lease_expires_at <= _server_now(self.store):
            raise GuardLostError("expired guard requires operator recovery")
        before_plan = set(labels.intersection(ALL_STATE_LABELS))
        if handle.plan_labels and before_plan != set(handle.plan_labels):
            raise GuardLostError("plan-state labels changed while guard was held")
        releasing = replace_record(
            current.record,
            phase=GuardPhase.RELEASING,
            predecessor_oid=current.oid,
            reason=reason,
        )
        releasing_snapshot = self._child(repository, issue, current, releasing)
        self.store.remove_label(repository, issue, STATE_IN_PROGRESS)
        after = set(self.store.read_labels(repository, issue))
        if STATE_IN_PROGRESS in after or set(after.intersection(ALL_STATE_LABELS)) != before_plan:
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
        if STATE_IN_PROGRESS not in labels:
            raise GuardError("guard ref and label are inconsistent")
        before_plan = set(labels.intersection(ALL_STATE_LABELS))
        recovering = replace_record(
            current.record,
            phase=GuardPhase.RECOVERING,
            predecessor_oid=current.oid,
            reason=reason,
        )
        recovering_snapshot = self._child(repository, issue, current, recovering)
        self.store.remove_label(repository, issue, STATE_IN_PROGRESS)
        after = set(self.store.read_labels(repository, issue))
        if STATE_IN_PROGRESS in after or set(after.intersection(ALL_STATE_LABELS)) != before_plan:
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

    def __init__(self, repository: str = "Owner/Repo", *, actor: str = "automation") -> None:
        self.repository = normalize_repository(repository)
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
        record = GuardRecord.from_json(message)
        self._commits[oid] = (record, tree, message)
        return oid, self.now

    def create_ref(self, repository: str, issue: int, oid: str) -> None:
        key = (repository, issue)
        if key in self.refs:
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
        call: Callable[..., subprocess.CompletedProcess[str]] = gh_call,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.repository = normalize_repository(repository)
        self._call = call
        self._env = dict(env) if env is not None else None
        self._last_server_time: datetime | None = None

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
        return f"repos/{self.repository}/{suffix}"

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
        self._request(
            ["--method", "DELETE", self._path(f"issues/{issue}/labels/{label}")]
        )

    def _commit(self, repository: str, oid: str) -> tuple[str, datetime]:
        status, body = self._request([self._path(f"git/commits/{oid}")])
        if status != 200 or not isinstance(body, dict):
            raise GuardUnavailableError("guard commit response was not successful")
        tree = body.get("tree")
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        message = body.get("message")
        if not isinstance(tree_sha, str) or not isinstance(message, str):
            raise GuardUnavailableError("guard commit response was malformed")
        return tree_sha, self.server_now()

    def read_ref(self, repository: str, issue: int) -> GuardSnapshot | None:
        ref = f"heads/hephaestus/issue-guards/issue-{issue}"
        status, body = self._request([self._path(f"git/ref/{ref}")], check=False)
        if status == 404:
            return None
        if status != 200 or not isinstance(body, dict):
            raise GuardUnavailableError("guard ref response was not successful")
        obj = body.get("object")
        oid = obj.get("sha") if isinstance(obj, dict) else None
        if not isinstance(oid, str):
            raise GuardUnavailableError("guard ref response was malformed")
        tree, server_time = self._commit(repository, oid)
        status, commit_body = self._request([self._path(f"git/commits/{oid}")])
        message = commit_body.get("message") if isinstance(commit_body, dict) else None
        if status != 200 or not isinstance(message, str):
            raise GuardUnavailableError("guard record commit message was malformed")
        try:
            record = GuardRecord.from_json(message)
        except ValueError as exc:
            raise GuardUnavailableError("guard ref contains a malformed record") from exc
        return GuardSnapshot(oid=oid, record=record, tree=tree, server_time=server_time)

    def default_tip(self, repository: str) -> tuple[str, str]:
        status, body = self._request([self._path("")])
        branch = body.get("default_branch") if isinstance(body, dict) else None
        if status != 200 or not isinstance(branch, str) or not branch:
            raise GuardUnavailableError("repository default branch metadata was malformed")
        status, body = self._request([self._path(f"git/ref/heads/{branch}")])
        obj = body.get("object") if isinstance(body, dict) else None
        oid = obj.get("sha") if isinstance(obj, dict) else None
        if status != 200 or not isinstance(oid, str):
            raise GuardUnavailableError("default branch ref was malformed")
        tree, _ = self._commit(repository, oid)
        return oid, tree

    def create_commit(
        self, repository: str, tree: str, parents: Sequence[str], message: str
    ) -> tuple[str, datetime]:
        args = [
            "--method",
            "POST",
            self._path("git/commits"),
            "-f",
            f"message={message}",
            "-f",
            f"tree={tree}",
        ]
        for parent in parents:
            args.extend(["-f", f"parents[]={parent}"])
        status, body = self._request(args)
        oid = body.get("sha") if isinstance(body, dict) else None
        if status not in {200, 201} or not isinstance(oid, str):
            raise GuardUnavailableError("guard commit creation failed")
        return oid, self.server_now()

    def create_ref(self, repository: str, issue: int, oid: str) -> None:
        status, _ = self._request(
            [
                "--method",
                "POST",
                self._path("git/refs"),
                "-f",
                f"ref={_GUARD_REF_PREFIX}{issue}",
                "-f",
                f"sha={oid}",
            ],
            check=False,
        )
        if status in {409, 422}:
            raise GuardConflictError("guard ref already exists")
        if status not in {200, 201}:
            raise GuardUnavailableError("guard ref creation failed")

    def update_ref(self, repository: str, issue: int, oid: str, expected_oid: str) -> None:
        status, _ = self._request(
            [
                "--method",
                "PATCH",
                self._path(f"git/refs/heads/hephaestus/issue-guards/issue-{issue}"),
                "-f",
                f"sha={oid}",
                "-F",
                "force=false",
            ],
            check=False,
        )
        if status in {409, 422}:
            raise GuardConflictError("guard ref update lost its non-force CAS")
        if status not in {200, 201}:
            raise GuardUnavailableError("guard ref update failed")

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
