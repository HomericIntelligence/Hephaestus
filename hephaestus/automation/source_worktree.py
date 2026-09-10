"""Deterministic, revision-bound worktrees for source-reading agents."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Self, cast

from hephaestus.agents.workspace import (
    DirtyDirectClaim,
    DirtyPlanIdentity,
    DirtySourceOperation,
    SourceLane,
    WorkspaceBinding,
    WorkspaceBindingError,
    _dirty_workspace_permit,
    validate_workspace_binding,
)
from hephaestus.automation.git_runtime import (
    current_operation_shutdown,
    operation_deadline,
    operation_file_lock,
    remaining_operation_timeout,
)
from hephaestus.automation.implementation_writer import (
    ImplementationWriterHandoff,
    implementation_writer_handoff,
)
from hephaestus.automation.worktree_manager import (
    ImplementationWriterAuthority,
    WorktreeManager,
    consume_implementation_writer_authority,
)
from hephaestus.automation.worktree_snapshot import (
    _dirty_worktree_content_snapshot,
    _dirty_worktree_snapshot_evidence,
)
from hephaestus.config.child_environments import build_git_signing_env
from hephaestus.io.utils import write_secure
from hephaestus.utils.file_lock import LockUnavailableError, file_lock
from hephaestus.utils.helpers import run_subprocess
from hephaestus.utils.worktree_identity import source_worktree_name


class SourceWorkspaceRecoveryKind(StrEnum):
    """Classify a source-workspace condition that needs operator recovery."""

    DIRTY_WORKTREE = "dirty_worktree"
    REVISION_DRIFT = "revision_drift"
    BRANCH_MISMATCH = "branch_mismatch"
    FOREIGN_OWNER = "foreign_owner"
    RECEIPT_PATH_MISSING = "receipt_path_missing"
    UNPROVEN_PREDECESSOR = "unproven_predecessor"
    DURABLE_OBLIGATIONS = "durable_obligations"


@dataclass(frozen=True, slots=True)
class SourceWorkspaceRecovery:
    """Describe one safe manual action for a rejected source workspace."""

    kind: SourceWorkspaceRecoveryKind
    item_number: int
    path: Path
    receipt_path: Path
    manual_action: str

    def to_dict(self) -> dict[str, object]:
        """Return the bounded recovery record for a worker result."""
        return {
            "kind": self.kind.value,
            "item_number": self.item_number,
            "path": str(self.path),
            "receipt_path": str(self.receipt_path),
            "manual_action": self.manual_action,
        }


class SourceWorkspaceError(RuntimeError):
    """Raised when a source lane cannot be prepared safely."""

    def __init__(
        self,
        message: str,
        *,
        recovery: SourceWorkspaceRecovery | None = None,
    ) -> None:
        """Initialize the error and its optional operator recovery record."""
        super().__init__(message)
        self.recovery = recovery


@dataclass(frozen=True, slots=True)
class SourceWorkspaceTerminalReference:
    """Identify a failure snapshot without granting writer authority."""

    identity: str
    content_sha256: str

    def to_dict(self) -> dict[str, str]:
        """Return the bounded transport fields."""
        return {"identity": self.identity, "content_sha256": self.content_sha256}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        """Reject malformed terminal references."""
        if (
            not isinstance(value, dict)
            or set(value) != {"identity", "content_sha256"}
            or not isinstance(value["identity"], str)
            or re.fullmatch(r"[1-9][0-9]*-impl-terminal\.json", value["identity"]) is None
            or not _terminal_digest_valid(value["content_sha256"])
        ):
            raise SourceWorkspaceError("source workspace terminal reference is invalid")
        return cls(value["identity"], value["content_sha256"])


@dataclass(frozen=True, slots=True)
class SourceWorkspaceTerminalView:
    """Return the verified terminal result and preservation action."""

    phase: str
    outcome: str
    cause: str
    action: str
    path: Path
    requested_branch: str | None
    requested_base_sha: str | None
    reservation_disposition: str = "preserve"


class SourceWorkspaceTerminalError(SourceWorkspaceError):
    """Carry a creation failure through the locked terminal capture."""

    def __init__(
        self,
        message: str,
        *,
        requested_branch: str | None = None,
        requested_base_sha: str | None = None,
        recovery: SourceWorkspaceRecovery | None = None,
    ) -> None:
        """Keep the request and failure without granting recovery authority."""
        super().__init__(message, recovery=recovery)
        self.requested_branch = requested_branch
        self.requested_base_sha = requested_base_sha
        self.terminal_reference: SourceWorkspaceTerminalReference | None = None
        self.path: Path | None = None
        self.preserve = True


def _terminal_digest_valid(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _terminal_json_digest(value: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _terminal_json_object(data: bytes) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate terminal evidence key")
            result[key] = value
        return result

    result = json.loads(data.decode("utf-8"), object_pairs_hook=unique_pairs)
    if not isinstance(result, dict):
        raise ValueError("terminal evidence must be an object")
    return result


_SOURCE_EVIDENCE_MAX_BYTES = 64 * 1024


def _terminal_read_bytes(path: Path) -> bytes:
    """Read one bounded regular file without following links."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    nonblock = getattr(os, "O_NONBLOCK", None)
    if (
        not isinstance(nofollow, int)
        or not nofollow
        or not isinstance(nonblock, int)
        or not nonblock
    ):
        raise SourceWorkspaceError("secure source workspace evidence reads are unavailable")
    if path.parent.is_symlink() or path.parent.resolve() != path.parent:
        raise SourceWorkspaceError("source workspace evidence directory is invalid")
    descriptor = os.open(path, os.O_RDONLY | nofollow | nonblock)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _SOURCE_EVIDENCE_MAX_BYTES:
            raise SourceWorkspaceError("source workspace evidence file is invalid")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, _SOURCE_EVIDENCE_MAX_BYTES + 1 - size)
            if not chunk:
                break
            size += len(chunk)
            if size > _SOURCE_EVIDENCE_MAX_BYTES:
                raise SourceWorkspaceError("source workspace evidence is too large")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = path.lstat()

        def identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        if identity(before) != identity(after) or identity(after) != identity(current):
            raise SourceWorkspaceError("source workspace evidence changed during read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


class SourceWorkspacePreparationCause(StrEnum):
    """Stable causes for bounded source-workspace preparation failures."""

    LANE_LOCK_UNAVAILABLE = "lane_lock_unavailable"
    GIT_METADATA_LOCK_UNAVAILABLE = "git_metadata_lock_unavailable"
    GIT_TIMEOUT = "git_timeout"


class SourceWorkspacePreparationError(SourceWorkspaceError):
    """Raised when bounded preparation cannot complete before its deadline."""

    def __init__(self, cause: SourceWorkspacePreparationCause, detail: str = "") -> None:
        """Initialize a classified preparation failure."""
        self.cause = cause
        message = cause.value if not detail else f"{cause.value}: {detail}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _PreparationDeadline:
    """Absolute deadline and monotonic clock for one bounded preparation."""

    expires_at: float
    monotonic: Callable[[], float]
    shutdown: threading.Event | None = None

    def remaining(self) -> float:
        """Return the remaining subprocess time or raise a stable timeout."""
        if self.shutdown is not None and self.shutdown.is_set():
            raise InterruptedError("source workspace operation cancelled")
        remaining = self.expires_at - self.monotonic()
        if remaining <= 0:
            raise SourceWorkspacePreparationError(
                SourceWorkspacePreparationCause.GIT_TIMEOUT,
                "source workspace preparation deadline expired",
            )
        return remaining


def _is_direct_implementation_branch(item_number: int, branch: str | None) -> bool:
    """Return whether *branch* is the exact managed direct-writer form."""
    return (
        branch is not None
        and re.fullmatch(rf"{item_number}-auto-impl-direct-[0-9a-f]{{32}}", branch) is not None
    )


@dataclass(frozen=True, slots=True)
class SourceWorkspaceReceipt:
    """Durable ownership and revision record for one source lane."""

    repository: str
    repository_identity: str
    ownership_key: str
    item_number: int
    lane: SourceLane
    path: Path
    revision: str
    generation: int
    detached: bool
    branch: str | None
    obligations: tuple[str, ...] = ()
    schema_version: int = 1
    dirty_claim: DirtyDirectClaim | None = None

    def to_binding(self, repo_root: Path) -> WorkspaceBinding:
        """Return the source binding described by these immutable facts."""
        return replace(
            WorkspaceBinding.source(
                cwd=self.path,
                reusable_root=repo_root,
                repository=self.repository,
                ownership_key=self.ownership_key,
                item_number=self.item_number,
                lane=self.lane,
                revision=self.revision,
                generation=self.generation,
                detached=self.detached,
            ),
            schema_version=self.schema_version,
            dirty_claim=self.dirty_claim,
        )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible receipt."""
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "repository": self.repository,
            "repository_identity": self.repository_identity,
            "ownership_key": self.ownership_key,
            "item_number": self.item_number,
            "lane": self.lane.value,
            "path": str(self.path),
            "revision": self.revision,
            "generation": self.generation,
            "detached": self.detached,
            "branch": self.branch,
            "obligations": list(self.obligations),
        }
        if self.schema_version == 2:
            payload["dirty_claim"] = self.dirty_claim.to_dict() if self.dirty_claim else None
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Self:
        """Parse a durable receipt with a closed schema."""
        fields = {
            "schema_version",
            "repository",
            "repository_identity",
            "ownership_key",
            "item_number",
            "lane",
            "path",
            "revision",
            "generation",
            "detached",
            "branch",
            "obligations",
        }
        if payload.get("schema_version") == 2:
            fields.add("dirty_claim")
        if set(payload) != fields:
            raise SourceWorkspaceError("source workspace receipt schema mismatch")
        try:
            branch = payload["branch"]
            receipt = cls(
                schema_version=int(payload["schema_version"]),
                repository=str(payload["repository"]),
                repository_identity=str(payload["repository_identity"]),
                ownership_key=str(payload["ownership_key"]),
                item_number=int(payload["item_number"]),
                lane=SourceLane(payload["lane"]),
                path=Path(str(payload["path"])),
                revision=str(payload["revision"]),
                generation=int(payload["generation"]),
                detached=bool(payload["detached"]),
                branch=str(branch) if branch is not None else None,
                obligations=tuple(str(value) for value in payload["obligations"]),
                dirty_claim=(
                    DirtyDirectClaim.from_dict(payload["dirty_claim"])
                    if payload["schema_version"] == 2
                    else None
                ),
            )
        except (TypeError, ValueError) as exc:
            raise SourceWorkspaceError(f"invalid source workspace receipt: {exc}") from exc
        if receipt.schema_version not in (1, 2) or receipt.generation < 1:
            raise SourceWorkspaceError("unsupported source workspace receipt")
        return receipt


def _git(
    cwd: Path,
    *args: str,
    check: bool = True,
    deadline: _PreparationDeadline | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run Git, applying the shared preparation deadline when supplied."""
    try:
        timeout = remaining_operation_timeout(
            deadline.remaining() if deadline is not None else None
        )
        if timeout is not None:
            shutdown = deadline.shutdown if deadline is not None else current_operation_shutdown()
            cancellation: dict[str, Any] = {"shutdown": shutdown} if shutdown is not None else {}
            return run_subprocess(
                ["git", *args],
                cwd=cwd,
                check=check,
                timeout=timeout,
                env=build_git_signing_env(),
                log_on_error=False,
                track_process_group=True,
                **cancellation,
            )
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=check,
            capture_output=True,
            text=True,
            env=build_git_signing_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        if deadline is None:  # pragma: no cover - subprocess timeout is caller-owned here
            raise
        raise SourceWorkspacePreparationError(
            SourceWorkspacePreparationCause.GIT_TIMEOUT,
            "Git operation exceeded the source workspace preparation deadline",
        ) from exc


_TRANSITION_PHASES = frozenset(
    {
        "prepared",
        "predecessor_removing",
        "successor_creating",
        "successor_created",
        "authority_minted",
        "receipt_pending",
    }
)


@dataclass(frozen=True, slots=True)
class _ImplementationWriterTransitionJournal:
    """Closed-schema durable record for one writer handoff."""

    repository: str
    repository_identity: str
    ownership_key: str
    item_number: int
    lane: SourceLane
    transition: str
    phase: str
    predecessor: SourceWorkspaceReceipt
    successor: SourceWorkspaceReceipt
    target_ref_revision: str | None
    journal_digest: str
    schema_version: int = 1

    def _digest_payload(self) -> dict[str, object]:
        """Return the immutable journal fields used for its digest."""
        return {
            "schema_version": self.schema_version,
            "repository": self.repository,
            "repository_identity": self.repository_identity,
            "ownership_key": self.ownership_key,
            "item_number": self.item_number,
            "lane": self.lane.value,
            "transition": self.transition,
            "predecessor": self.predecessor.to_dict(),
            "successor": self.successor.to_dict(),
            "target_ref_revision": self.target_ref_revision,
        }

    def to_dict(self) -> dict[str, object]:
        """Return the JSON-compatible journal representation."""
        return {
            **self._digest_payload(),
            "phase": self.phase,
            "journal_digest": self.journal_digest,
        }

    @classmethod
    def create(
        cls,
        *,
        repository: str,
        repository_identity: str,
        ownership_key: str,
        item_number: int,
        predecessor: SourceWorkspaceReceipt,
        successor: SourceWorkspaceReceipt,
        transition: str,
        target_ref_revision: str | None,
    ) -> Self:
        """Create a prepared journal with a stable identity digest."""
        journal = cls(
            repository=repository,
            repository_identity=repository_identity,
            ownership_key=ownership_key,
            item_number=item_number,
            lane=SourceLane.IMPLEMENTATION,
            transition=transition,
            phase="prepared",
            predecessor=predecessor,
            successor=successor,
            target_ref_revision=target_ref_revision,
            journal_digest="",
        )
        digest = hashlib.sha256(
            json.dumps(journal._digest_payload(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return replace(journal, journal_digest=digest)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Self:
        """Parse and validate the closed transition-journal schema."""
        fields = {
            "schema_version",
            "repository",
            "repository_identity",
            "ownership_key",
            "item_number",
            "lane",
            "transition",
            "phase",
            "predecessor",
            "successor",
            "target_ref_revision",
            "journal_digest",
        }
        if set(payload) != fields:
            raise SourceWorkspaceError("source workspace transition schema mismatch")
        receipt_fields = {
            "schema_version",
            "repository",
            "repository_identity",
            "ownership_key",
            "item_number",
            "lane",
            "path",
            "revision",
            "generation",
            "detached",
            "branch",
            "obligations",
        }

        def valid_receipt_payload(value: object) -> bool:
            return (
                isinstance(value, dict)
                and set(value) == receipt_fields
                and type(value["schema_version"]) is int
                and type(value["item_number"]) is int
                and type(value["generation"]) is int
                and type(value["detached"]) is bool
                and all(
                    isinstance(value[name], str)
                    for name in (
                        "repository",
                        "repository_identity",
                        "ownership_key",
                        "lane",
                        "path",
                        "revision",
                    )
                )
                and (value["branch"] is None or isinstance(value["branch"], str))
                and isinstance(value["obligations"], list)
                and all(isinstance(item, str) for item in value["obligations"])
            )

        if (
            type(payload["schema_version"]) is not int
            or type(payload["item_number"]) is not int
            or not all(
                isinstance(payload[name], str)
                for name in (
                    "repository",
                    "repository_identity",
                    "ownership_key",
                    "lane",
                    "transition",
                    "phase",
                    "journal_digest",
                )
            )
            or (
                payload["target_ref_revision"] is not None
                and not isinstance(payload["target_ref_revision"], str)
            )
            or not valid_receipt_payload(payload["predecessor"])
            or not valid_receipt_payload(payload["successor"])
        ):
            raise SourceWorkspaceError("invalid source workspace transition")
        try:
            journal = cls(
                schema_version=payload["schema_version"],
                repository=str(payload["repository"]),
                repository_identity=str(payload["repository_identity"]),
                ownership_key=str(payload["ownership_key"]),
                item_number=payload["item_number"],
                lane=SourceLane(payload["lane"]),
                transition=str(payload["transition"]),
                phase=str(payload["phase"]),
                predecessor=SourceWorkspaceReceipt.from_dict(payload["predecessor"]),
                successor=SourceWorkspaceReceipt.from_dict(payload["successor"]),
                target_ref_revision=payload["target_ref_revision"],
                journal_digest=str(payload["journal_digest"]),
            )
        except (TypeError, ValueError) as exc:
            raise SourceWorkspaceError(f"invalid source workspace transition: {exc}") from exc
        if (
            journal.schema_version != 1
            or journal.lane is not SourceLane.IMPLEMENTATION
            or journal.transition not in {"direct", "adopted"}
            or journal.phase not in _TRANSITION_PHASES
            or not re.fullmatch(r"[0-9a-f]{64}", journal.journal_digest)
            or (
                journal.target_ref_revision is not None
                and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", journal.target_ref_revision)
                is None
            )
        ):
            raise SourceWorkspaceError("unsupported source workspace transition")
        expected_digest = hashlib.sha256(
            json.dumps(journal._digest_payload(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if journal.journal_digest != expected_digest:
            raise SourceWorkspaceError("source workspace transition digest mismatch")
        return journal


class SourceWorkspaceManager:
    """Create, rebind, validate, and clean the two item source lanes."""

    def __init__(
        self,
        repo_root: Path,
        *,
        repository: str,
        base_dir: Path | None = None,
    ) -> None:
        """Initialize ownership state for one reusable repository checkout."""
        self.repo_root = repo_root.resolve(strict=True)
        self.repository = repository
        self.common_dir = WorktreeManager.git_metadata_lock_path(self.repo_root).parent.resolve(
            strict=True
        )
        digest = hashlib.sha256(str(self.common_dir).encode()).hexdigest()[:16]
        self.repository_identity = f"{repository}:{digest}"
        self.base_dir = (base_dir or self.repo_root / "build" / ".worktrees").resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.common_dir / "hephaestus-source-workspaces"

    def path_for(self, item_number: int, lane: SourceLane) -> Path:
        """Return the deterministic physical path for a lane."""
        return self.base_dir / source_worktree_name(item_number, lane.value)

    def _implementation_path(self, item_number: int) -> Path:
        """Return the lexical implementation path, or reject a symlink."""
        path = self.path_for(item_number, SourceLane.IMPLEMENTATION)
        if (
            self.base_dir.is_symlink()
            or self.base_dir.resolve() != self.base_dir
            or path.is_symlink()
        ):
            raise SourceWorkspaceError("implementation writer transition path is invalid")
        return path

    def ownership_key(self, item_number: int, lane: SourceLane) -> str:
        """Return the repository-qualified internal ownership key."""
        return f"{self.repository_identity}:{item_number}:{lane.value}"

    @contextmanager
    def implementation_writer_handoff(
        self, item_number: int, *, deadline: _PreparationDeadline | None = None
    ) -> Iterator[ImplementationWriterHandoff]:
        """Hold the implementation lane for one complete writer handoff."""
        if isinstance(item_number, bool) or not isinstance(item_number, int):
            raise SourceWorkspaceError("implementation writer handoff item number is invalid")
        if deadline is None:
            deadline = _PreparationDeadline(time.monotonic() + 45.0, time.monotonic)
        with (
            operation_deadline(deadline.expires_at, shutdown=deadline.shutdown),
            implementation_writer_handoff(
                self.repo_root,
                item_number,
                self._lane_lock_path(item_number, SourceLane.IMPLEMENTATION),
                remaining_timeout=deadline.remaining,
                shutdown=deadline.shutdown,
            ) as handoff,
        ):
            try:
                self._reconcile_writer_transition(item_number, finalize_exact_successor=True)
                yield handoff
            except BaseException as exc:
                terminal = exc if isinstance(exc, SourceWorkspaceTerminalError) else None
                journal_path = self._transition_path(item_number)
                if terminal is None and (journal_path.exists() or journal_path.is_symlink()):
                    terminal = SourceWorkspaceTerminalError(str(exc))
                if terminal is not None:
                    terminal.path = self.path_for(item_number, SourceLane.IMPLEMENTATION)
                    try:
                        terminal.terminal_reference = self._capture_terminal_failure(
                            item_number, terminal
                        )
                    except (OSError, ValueError, SourceWorkspaceError):
                        terminal.terminal_reference = None
                    if terminal is not exc:
                        raise terminal from exc
                raise
            else:
                self._reconcile_writer_transition(item_number, finalize_exact_successor=False)

    @staticmethod
    def guard_branch(item_number: int) -> str:
        """Return the stable control-plane branch name; it owns no worktree."""
        return f"auto-{item_number}-guard"

    def prepare(
        self,
        item_number: int,
        lane: SourceLane,
        revision: str,
        *,
        branch: str | None = None,
    ) -> WorkspaceBinding:
        """Prepare a clean lane at exactly ``revision``, reusing its path."""
        return self._prepare(
            item_number,
            lane,
            revision,
            branch=branch,
            deadline=None,
        )

    def prepare_bounded(
        self,
        item_number: int,
        lane: SourceLane,
        revision: str,
        *,
        branch: str | None = None,
        deadline: _PreparationDeadline | None = None,
    ) -> WorkspaceBinding:
        """Prepare a source lane with nonblocking locks and a hard deadline."""
        if deadline is None:
            monotonic = time.monotonic
            deadline = _PreparationDeadline(monotonic() + 45.0, monotonic)
        return self._prepare(
            item_number,
            lane,
            revision,
            branch=branch,
            deadline=deadline,
        )

    def _prepare(
        self,
        item_number: int,
        lane: SourceLane,
        revision: str,
        *,
        branch: str | None,
        deadline: _PreparationDeadline | None,
    ) -> WorkspaceBinding:
        """Share preparation logic between blocking and bounded callers."""
        target = _git(
            self.repo_root,
            "rev-parse",
            f"{revision}^{{commit}}",
            deadline=deadline,
        ).stdout.strip()
        path = self.path_for(item_number, lane)
        lock_path = self._lane_lock_path(item_number, lane)
        lock_options: dict[str, Any] = {"require_exclusive": True}
        if deadline is not None:
            lock_options["blocking"] = False
        try:
            lane_lock = file_lock(lock_path, **lock_options)
            with lane_lock:
                return self._prepare_locked(
                    item_number,
                    lane,
                    target,
                    path,
                    branch=branch,
                    deadline=deadline,
                )
        except LockUnavailableError as exc:
            if deadline is None:  # pragma: no cover - blocking acquisition does not use this path
                raise
            raise SourceWorkspacePreparationError(
                SourceWorkspacePreparationCause.LANE_LOCK_UNAVAILABLE,
                f"source lane lock is unavailable: {lock_path}",
            ) from exc

    def _prepare_locked(
        self,
        item_number: int,
        lane: SourceLane,
        target: str,
        path: Path,
        *,
        branch: str | None,
        deadline: _PreparationDeadline | None,
    ) -> WorkspaceBinding:
        """Prepare one lane while its source lock is held."""
        old = self._read_receipt(item_number, lane)
        self._reject_foreign_owner(old, item_number, lane)
        if path.exists() and self._is_dirty(path, deadline=deadline):
            raise SourceWorkspaceError(f"source workspace is dirty and preserved: {path}")
        desired_detached = lane is SourceLane.REVIEW or branch is None
        physical_revision = self._head_revision(path, deadline=deadline) if path.exists() else None
        physical_branch = self._head_branch(path, deadline=deadline) if path.exists() else None
        physical_checkout_matches = (
            physical_branch is None
            if desired_detached
            else physical_branch == f"refs/heads/{branch}"
        )
        can_reuse = (
            old is not None
            and path.exists()
            and old.path.resolve() == path.resolve()
            and old.revision == target
            and physical_revision == target
            and old.detached == desired_detached
            and old.branch == branch
            and physical_checkout_matches
        )
        generation = (
            old.generation if can_reuse and old is not None else old.generation + 1 if old else 1
        )
        path_already_at_target = (
            path.exists()
            and physical_revision == target
            and old is not None
            and old.path.resolve() == path.resolve()
            and old.detached == desired_detached
            and old.branch == branch
            and physical_checkout_matches
        )
        if not can_reuse and not path_already_at_target:
            owns_branch = (
                old is not None
                and old.path.resolve() == path.resolve()
                and not old.detached
                and old.branch == branch
            )
            self._replace_worktree(
                path,
                target,
                branch=None if desired_detached else branch,
                owns_branch=owns_branch,
                deadline=deadline,
            )
        verified_revision = self._head_revision(path, deadline=deadline)
        verified_branch = self._head_branch(path, deadline=deadline)
        verified_checkout_matches = (
            verified_branch is None
            if desired_detached
            else verified_branch == f"refs/heads/{branch}"
        )
        if verified_revision != target:
            raise SourceWorkspaceError("source workspace could not reach the requested revision")
        if not verified_checkout_matches:
            raise SourceWorkspaceError(
                "source workspace checkout does not match the requested lane"
            )
        receipt = SourceWorkspaceReceipt(
            repository=self.repository,
            repository_identity=self.repository_identity,
            ownership_key=self.ownership_key(item_number, lane),
            item_number=item_number,
            lane=lane,
            path=path.resolve(),
            revision=verified_revision,
            generation=generation,
            detached=desired_detached,
            branch=None if desired_detached else branch,
            obligations=old.obligations if old is not None else (),
        )
        binding = self._binding(receipt)
        try:
            validate_workspace_binding(
                binding,
                remaining_timeout=deadline.remaining if deadline is not None else None,
                shutdown=deadline.shutdown if deadline is not None else None,
            )
        except WorkspaceBindingError as exc:
            raise SourceWorkspaceError(str(exc)) from exc
        self._write_receipt(receipt)
        return binding

    def claim_implementation_writer(  # noqa: C901
        self,
        item_number: int,
        *,
        branch: str,
        path: Path,
        authority: ImplementationWriterAuthority | None = None,
        handoff: ImplementationWriterHandoff | None = None,
    ) -> WorkspaceBinding:
        """Record ownership of one verified implementation writer checkout.

        Only the worker that created the deterministic implementation checkout
        calls this method. Generic preparation continues to reject an
        unrecorded branch, so a caller cannot adopt an arbitrary branch.
        """
        lane = SourceLane.IMPLEMENTATION
        expected_path = self._implementation_path(item_number)
        if handoff is None:
            raise SourceWorkspaceError("implementation writer handoff is missing")
        try:
            handoff._validate(
                self.repo_root,
                item_number,
                self._lane_lock_path(item_number, SourceLane.IMPLEMENTATION),
            )
        except RuntimeError as exc:
            raise SourceWorkspaceError(str(exc)) from exc
        if path != expected_path:
            raise SourceWorkspaceError(
                "implementation writer path does not match the deterministic lane"
            )
        old = self._read_receipt(item_number, lane)
        self._reject_foreign_owner(old, item_number, lane)
        if old is not None and old.path != expected_path:
            raise SourceWorkspaceError("incompatible source workspace receipt")
        transition = self._read_writer_transition(item_number)
        if (
            old is not None
            and not old.detached
            and old.branch != branch
            and transition is None
            and authority is None
        ):
            raise SourceWorkspaceError("incompatible source workspace receipt")
        if not expected_path.exists():
            raise SourceWorkspaceError("implementation writer worktree does not exist")
        if self._is_dirty(expected_path):
            raise SourceWorkspaceError(f"source workspace is dirty and preserved: {expected_path}")
        revision = self._head_revision(expected_path)
        if self._head_branch(expected_path) != f"refs/heads/{branch}":
            raise SourceWorkspaceError(
                "implementation writer checkout does not match the requested branch"
            )
        receipt = SourceWorkspaceReceipt(
            repository=self.repository,
            repository_identity=self.repository_identity,
            ownership_key=self.ownership_key(item_number, lane),
            item_number=item_number,
            lane=lane,
            path=expected_path,
            revision=revision,
            generation=old.generation + 1 if old is not None else 1,
            detached=False,
            branch=branch,
            obligations=old.obligations if old is not None else (),
        )
        binding = self._binding(receipt)
        try:
            validate_workspace_binding(binding)
        except WorkspaceBindingError as exc:
            raise SourceWorkspaceError(str(exc)) from exc
        try:
            predecessor_evidence = consume_implementation_writer_authority(
                authority,
                issue_number=item_number,
                branch=branch,
                path=expected_path,
                revision=revision,
            )
        except RuntimeError as exc:
            raise SourceWorkspaceError(
                f"implementation writer authority is invalid: {exc}"
            ) from exc
        if transition is not None:
            if old != transition.predecessor:
                raise SourceWorkspaceError("implementation writer predecessor receipt changed")
            try:
                handoff._validate_consumed_writer_transition(
                    predecessor_evidence,
                    path=expected_path,
                    predecessor_generation=old.generation,
                    predecessor_revision=old.revision,
                    predecessor_detached=old.detached,
                    predecessor_branch=old.branch,
                    branch=branch,
                    successor_revision=revision,
                    transition=transition.transition,
                    journal_digest=transition.journal_digest,
                )
                handoff._mark_transition_phase("receipt_pending")
            except RuntimeError as exc:
                raise SourceWorkspaceError(str(exc)) from exc
        elif old is not None and old.branch != branch:
            try:
                handoff._validate_consumed_direct_transition(
                    predecessor_evidence,
                    path=expected_path,
                    predecessor_generation=old.generation,
                    predecessor_revision=old.revision,
                    predecessor_branch=(None if old.detached else f"refs/heads/{old.branch}"),
                    branch=branch,
                )
            except RuntimeError as exc:
                raise SourceWorkspaceError("incompatible source workspace receipt") from exc
        elif predecessor_evidence is not None:
            raise SourceWorkspaceError("unexpected implementation writer transition evidence")
        try:
            self._write_receipt(receipt)
        except OSError as exc:
            raise SourceWorkspaceError("cannot record implementation writer receipt") from exc
        if transition is not None:
            try:
                handoff._complete_writer_transition()
            except RuntimeError as exc:
                raise SourceWorkspaceError(str(exc)) from exc
        return binding

    @contextmanager
    def implementation_publication(
        self, item_number: int, *, branch: str, path: Path
    ) -> Iterator[Callable[[str, str], WorkspaceBinding]]:
        """Record a controlled commit only after exact remote publication."""
        with self.implementation_local_commit(item_number, branch=branch, path=path) as record:
            active = True
            consumed = False

            def advance(head: str, remote_head: str) -> WorkspaceBinding:
                nonlocal consumed
                if not active or consumed:
                    raise SourceWorkspaceError("implementation publication authority expired")
                consumed = True
                if remote_head != head:
                    raise SourceWorkspaceError("implementation publication head changed")
                return record(head)

            try:
                yield advance
            finally:
                active = False

    @contextmanager
    def implementation_local_commit(
        self,
        item_number: int,
        *,
        branch: str,
        path: Path,
        expected_binding: WorkspaceBinding | None = None,
        paused_head_sha: str | None = None,
        deadline: _PreparationDeadline | None = None,
    ) -> Iterator[Callable[[str], WorkspaceBinding]]:
        """Hold one writer lease and optionally record its new clean head.

        The expected binding fixes the source receipt for a queued host job.
        A paused rebase also requires its exact head and active branch. The
        returned callback records local state and does not prove publication.
        """
        lane = SourceLane.IMPLEMENTATION
        if deadline is None:
            deadline = _PreparationDeadline(time.monotonic() + 45.0, time.monotonic)
        with (
            self._acquire_lane(item_number, lane, deadline),
            operation_deadline(deadline.expires_at, shutdown=deadline.shutdown),
        ):
            original = self._require_receipt(item_number, lane)
            self._reject_foreign_owner(original, item_number, lane)
            expected_head = original.revision
            expected_branch: str | None = f"refs/heads/{branch}"
            if paused_head_sha is not None:
                if (
                    re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", paused_head_sha) is None
                    or WorktreeManager(
                        repo_root=self.repo_root, base_dir=self.base_dir
                    )._detached_rebase_head_ref(
                        path,
                        common_git_dir=self.common_dir,
                        timeout=max(1, int(deadline.remaining())),
                        shutdown=deadline.shutdown,
                    )
                    != expected_branch
                ):
                    raise SourceWorkspaceError("implementation paused rebase binding changed")
                expected_head = paused_head_sha
                expected_branch = None
            if (
                original.detached
                or (expected_binding is not None and self._binding(original) != expected_binding)
                or original.branch != branch
                or original.path != self._implementation_path(item_number)
                or path != original.path
                or path.is_symlink()
                or not self._path_is_registered_to_repository(path, deadline=deadline)
                or self._head_branch(path, deadline=deadline) != expected_branch
                or self._head_revision(path, deadline=deadline) != expected_head
            ):
                raise SourceWorkspaceError("implementation publication binding changed")
            active = True
            consumed = False

            def advance(head: str) -> WorkspaceBinding:
                nonlocal consumed
                if not active or consumed:
                    raise SourceWorkspaceError("implementation publication authority expired")
                consumed = True
                successor = replace(
                    original,
                    revision=head,
                    generation=original.generation + (head != original.revision),
                )
                if (
                    re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head) is None
                    or self._read_receipt(item_number, lane) != original
                    or not self._physical_matches_receipt(successor, deadline=deadline)
                ):
                    raise SourceWorkspaceError("implementation publication head changed")
                if successor != original:
                    self._write_receipt(successor)
                if self._read_receipt(
                    item_number, lane
                ) != successor or not self._physical_matches_receipt(successor, deadline=deadline):
                    raise SourceWorkspaceError("implementation publication receipt changed")
                return self._binding(successor)

            try:
                yield advance
            finally:
                active = False

    def authorize_direct_implementation_writer_transition(
        self,
        item_number: int,
        *,
        branch: str,
        base_sha: str,
        handoff: ImplementationWriterHandoff | None,
    ) -> bool:
        """Arm one exact controlled predecessor transition for a direct writer."""
        target = _git(self.repo_root, "rev-parse", f"{base_sha}^{{commit}}").stdout.strip()
        if target != base_sha:
            raise SourceWorkspaceError("direct implementation writer base is invalid")
        return self._authorize_writer_transition(
            item_number,
            branch=branch,
            target_revision=target,
            transition="direct",
            handoff=handoff,
        )

    def authorize_adopted_implementation_writer_transition(
        self,
        item_number: int,
        *,
        branch: str,
        expected_head: str,
        handoff: ImplementationWriterHandoff | None,
    ) -> bool:
        """Arm one exact controlled predecessor transition for an adopted writer."""
        target = _git(self.repo_root, "rev-parse", f"{expected_head}^{{commit}}").stdout.strip()
        if target != expected_head:
            raise SourceWorkspaceError("adopted implementation writer head is invalid")
        return self._authorize_writer_transition(
            item_number,
            branch=branch,
            target_revision=target,
            transition="adopted",
            handoff=handoff,
        )

    def _authorize_writer_transition(  # noqa: C901
        self,
        item_number: int,
        *,
        branch: str,
        target_revision: str,
        transition: str,
        handoff: ImplementationWriterHandoff | None,
    ) -> bool:
        """Validate and durably arm one direct or adopted writer transition."""
        lane = SourceLane.IMPLEMENTATION
        expected_path = self._implementation_path(item_number)
        if handoff is None:
            raise SourceWorkspaceError("implementation writer handoff is missing")
        try:
            handoff._validate(
                self.repo_root,
                item_number,
                self._lane_lock_path(item_number, lane),
            )
        except RuntimeError as exc:
            raise SourceWorkspaceError(str(exc)) from exc
        receipt_path = self._receipt_path(item_number, lane)
        try:
            old = self._read_receipt(item_number, lane)
        except (OSError, UnicodeError, subprocess.SubprocessError, SourceWorkspaceError) as exc:
            raise SourceWorkspaceError(
                "implementation writer predecessor is unproven",
                recovery=self._unproven_recovery(
                    item_number=item_number,
                    path=expected_path,
                    receipt_path=receipt_path,
                ),
            ) from exc
        self._reject_foreign_owner(old, item_number, lane)
        if old is None:
            try:
                predecessor_is_registered = self._has_worktree_registration(expected_path)
            except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
                raise SourceWorkspaceError(
                    "implementation writer predecessor is unproven",
                    recovery=self._unproven_recovery(
                        item_number=item_number,
                        path=expected_path,
                        receipt_path=receipt_path,
                    ),
                ) from exc
            if expected_path.exists() or predecessor_is_registered:
                raise SourceWorkspaceError(
                    "implementation writer predecessor is unproven",
                    recovery=self._recovery(
                        SourceWorkspaceRecoveryKind.UNPROVEN_PREDECESSOR,
                        item_number=item_number,
                        path=expected_path,
                        receipt_path=receipt_path,
                        manual_action=(
                            f"Inspect and preserve {expected_path}. Use the approved "
                            "source-workspace cleanup only after you preserve the work. "
                            f"Then rerun issue #{item_number}."
                        ),
                    ),
                )
            return False
        if old.path.resolve() != expected_path:
            raise SourceWorkspaceError(
                "source workspace receipt path is not deterministic",
                recovery=self._recovery(
                    SourceWorkspaceRecoveryKind.UNPROVEN_PREDECESSOR,
                    item_number=item_number,
                    path=expected_path,
                    receipt_path=receipt_path,
                    manual_action=(
                        f"Inspect and preserve {expected_path}. Use the approved "
                        "source-workspace cleanup only after you preserve the work. "
                        f"Then rerun issue #{item_number}."
                    ),
                ),
            )
        if (old.detached and old.branch is not None) or (not old.detached and not old.branch):
            raise SourceWorkspaceError(
                "implementation writer predecessor receipt is unproven",
                recovery=self._unproven_recovery(
                    item_number=item_number,
                    path=expected_path,
                    receipt_path=receipt_path,
                ),
            )
        if old.obligations:
            raise SourceWorkspaceError(
                "implementation writer predecessor has durable obligations",
                recovery=self._recovery(
                    SourceWorkspaceRecoveryKind.DURABLE_OBLIGATIONS,
                    item_number=item_number,
                    path=expected_path,
                    receipt_path=receipt_path,
                    manual_action=(
                        f"Complete or explicitly clear the durable obligations for {expected_path} "
                        "through the owning pipeline. Then rerun "
                        f"issue #{item_number}."
                    ),
                ),
            )
        if not expected_path.exists():
            raise SourceWorkspaceError(
                f"source workspace receipt path is missing: {expected_path}",
                recovery=self._recovery(
                    SourceWorkspaceRecoveryKind.RECEIPT_PATH_MISSING,
                    item_number=item_number,
                    path=expected_path,
                    receipt_path=receipt_path,
                    manual_action=(
                        f"Inspect the worktree registration for {expected_path}. Repair the "
                        "registration if the worktree moved. If it was deleted, preserve "
                        "reachable refs, prune only that stale registration, remove "
                        f"{receipt_path}, and rerun issue #{item_number}."
                    ),
                ),
            )
        try:
            is_dirty = self._is_dirty(expected_path)
            physical_revision = self._head_revision(expected_path)
            physical_branch = self._head_branch(expected_path)
        except (OSError, UnicodeError, subprocess.SubprocessError, SourceWorkspaceError) as exc:
            raise SourceWorkspaceError(
                "implementation writer predecessor is unproven",
                recovery=self._unproven_recovery(
                    item_number=item_number,
                    path=expected_path,
                    receipt_path=receipt_path,
                ),
            ) from exc
        if is_dirty:
            raise SourceWorkspaceError(
                "implementation writer predecessor is invalid because source workspace is "
                f"dirty and preserved: {expected_path}",
                recovery=self._recovery(
                    SourceWorkspaceRecoveryKind.DIRTY_WORKTREE,
                    item_number=item_number,
                    path=expected_path,
                    receipt_path=receipt_path,
                    manual_action=(
                        f"Commit or stash the changes in {expected_path}. Verify that the "
                        f"worktree is clean. Then rerun issue #{item_number}."
                    ),
                ),
            )
        if physical_revision != old.revision:
            raise SourceWorkspaceError(
                "implementation writer predecessor is invalid because source workspace "
                f"revision drifted and is preserved: {expected_path}",
                recovery=self._recovery(
                    SourceWorkspaceRecoveryKind.REVISION_DRIFT,
                    item_number=item_number,
                    path=expected_path,
                    receipt_path=receipt_path,
                    manual_action=self._revision_recovery_action(
                        item_number=item_number,
                        path=expected_path,
                        receipt=old,
                    ),
                ),
            )
        expected_predecessor_branch = (
            None if old.detached else f"refs/heads/{old.branch}" if old.branch else "invalid"
        )
        if physical_branch != expected_predecessor_branch:
            raise SourceWorkspaceError(
                "implementation writer predecessor is invalid because source workspace "
                f"branch does not match its receipt: {expected_path}",
                recovery=self._recovery(
                    SourceWorkspaceRecoveryKind.BRANCH_MISMATCH,
                    item_number=item_number,
                    path=expected_path,
                    receipt_path=receipt_path,
                    manual_action=self._revision_recovery_action(
                        item_number=item_number,
                        path=expected_path,
                        receipt=old,
                    ),
                ),
            )
        target_ref_revision = self._validate_transition_target_ref(
            old,
            branch=branch,
            target_revision=target_revision,
            transition=transition,
        )
        successor = SourceWorkspaceReceipt(
            repository=self.repository,
            repository_identity=self.repository_identity,
            ownership_key=self.ownership_key(item_number, lane),
            item_number=item_number,
            lane=lane,
            path=expected_path,
            revision=target_revision,
            generation=old.generation + 1,
            detached=False,
            branch=branch,
            obligations=old.obligations,
        )
        journal = _ImplementationWriterTransitionJournal.create(
            repository=self.repository,
            repository_identity=self.repository_identity,
            ownership_key=self.ownership_key(item_number, lane),
            item_number=item_number,
            predecessor=old,
            successor=successor,
            transition=transition,
            target_ref_revision=target_ref_revision,
        )
        self._write_writer_transition(journal)
        if not expected_path.exists():
            self._restore_transition_predecessor(journal)
        try:
            handoff._arm_writer_transition(
                path=expected_path,
                predecessor_generation=old.generation,
                predecessor_revision=old.revision,
                predecessor_detached=old.detached,
                predecessor_branch=old.branch,
                successor_branch=branch,
                successor_revision=target_revision,
                transition=transition,
                journal_digest=journal.journal_digest,
                target_ref_revision=target_ref_revision,
                journal_validator=lambda digest: self._validate_writer_transition_digest(
                    item_number, digest
                ),
                phase_writer=lambda digest, phase: self._update_writer_transition_phase(
                    item_number, digest, phase
                ),
                commit_writer=lambda digest: self._remove_writer_transition(item_number, digest),
            )
        except RuntimeError as exc:
            self._remove_writer_transition(item_number, journal.journal_digest)
            raise SourceWorkspaceError(str(exc)) from exc
        return True

    def _validate_transition_target_ref(
        self,
        predecessor: SourceWorkspaceReceipt,
        *,
        branch: str,
        target_revision: str,
        transition: str,
    ) -> str | None:
        """Return the exact permitted pre-transition local target revision."""
        target_ref_revision = self._local_branch_revision(branch)
        if transition == "direct":
            allowed_target_revision = predecessor.revision if predecessor.branch == branch else None
            if target_ref_revision != allowed_target_revision:
                raise SourceWorkspaceError("direct implementation writer target branch is invalid")
        elif target_ref_revision not in {None, predecessor.revision, target_revision}:
            raise SourceWorkspaceError("adopted implementation writer target branch is invalid")
        return target_ref_revision

    def _has_worktree_registration(self, path: Path) -> bool:
        """Return whether Git still registers the exact source path."""
        expected_path = path.resolve()
        result = _git(self.repo_root, "worktree", "list", "--porcelain", "-z")
        return any(
            Path(field.removeprefix("worktree ")).resolve() == expected_path
            for field in result.stdout.split("\0")
            if field.startswith("worktree ")
        )

    def _recovery(
        self,
        kind: SourceWorkspaceRecoveryKind,
        *,
        item_number: int,
        path: Path,
        receipt_path: Path,
        manual_action: str,
    ) -> SourceWorkspaceRecovery:
        """Build one recovery record with normalized paths."""
        return SourceWorkspaceRecovery(
            kind=kind,
            item_number=item_number,
            path=path.resolve(),
            receipt_path=receipt_path.absolute(),
            manual_action=manual_action,
        )

    def _unproven_recovery(
        self, *, item_number: int, path: Path, receipt_path: Path
    ) -> SourceWorkspaceRecovery:
        """Build the recovery record for an unproven predecessor."""
        return self._recovery(
            SourceWorkspaceRecoveryKind.UNPROVEN_PREDECESSOR,
            item_number=item_number,
            path=path,
            receipt_path=receipt_path,
            manual_action=(
                f"Inspect and preserve {path}. Use the approved source-workspace cleanup "
                "only after you preserve the work. "
                f"Then rerun issue #{item_number}."
            ),
        )

    @staticmethod
    def _revision_recovery_action(
        *, item_number: int, path: Path, receipt: SourceWorkspaceReceipt
    ) -> str:
        """Return the recovery action for attached or detached checkout drift."""
        receipt_branch = receipt.branch or "detached"
        return (
            f"Preserve the current checkout at {path}. Restore its branch and HEAD to "
            f"{receipt_branch}@{receipt.revision}, or use the approved source-workspace "
            "cleanup after you preserve the work. "
            f"Then rerun issue #{item_number}."
        )

    def claim_dirty_direct_continuation(
        self, item_number: int, *, claim: DirtyDirectClaim, expected_generation: int
    ) -> WorkspaceBinding:
        """Arm one owned writer after the worker proves plan and PR absence."""
        with file_lock(
            self._lane_lock_path(item_number, SourceLane.IMPLEMENTATION), require_exclusive=True
        ):
            return self._arm_dirty_direct_claim_locked(
                item_number, claim=claim, expected_generation=expected_generation
            )

    def _arm_dirty_direct_claim_locked(
        self, item_number: int, *, claim: DirtyDirectClaim, expected_generation: int
    ) -> WorkspaceBinding:
        """Write a claim while the caller holds the exact source lane lock."""
        lane = SourceLane.IMPLEMENTATION
        DirtyDirectClaim.from_dict(claim.to_dict())
        receipt = self._require_receipt(item_number, lane)
        self._reject_foreign_owner(receipt, item_number, lane)
        if (
            receipt.schema_version != 1
            or receipt.generation != expected_generation
            or claim.state != "armed"
            or receipt.branch != claim.branch
            or receipt.revision != claim.reservation_base_sha
            or not _is_direct_implementation_branch(item_number, receipt.branch)
            or not self._is_dirty(receipt.path)
        ):
            raise SourceWorkspaceError("dirty direct claim does not match its predecessor")
        self._validate_dirty_claim_physical(receipt, claim)
        successor = replace(
            receipt,
            schema_version=2,
            generation=receipt.generation + 1,
            dirty_claim=claim,
            obligations=tuple(dict.fromkeys((*receipt.obligations, "dirty-direct-continuation"))),
        )
        self._write_receipt(successor)
        if self._read_receipt(item_number, lane) != successor:
            raise SourceWorkspaceError("dirty direct claim write is unconfirmed")
        return self._binding(successor)

    @contextmanager
    def dirty_direct_publication(
        self, binding: WorkspaceBinding, *, deadline: _PreparationDeadline | None = None
    ) -> Iterator[Callable[[str], None]]:
        """Hold a consumed writer claim through controlled commit and publication."""
        claim = binding.dirty_claim
        item = binding.item_number
        if binding.schema_version != 2 or claim is None or item is None or claim.state != "armed":
            raise SourceWorkspaceError("dirty direct publication binding is invalid")
        lane = SourceLane.IMPLEMENTATION
        if deadline is None:
            deadline = _PreparationDeadline(time.monotonic() + 45.0, time.monotonic)
        with (
            self._acquire_lane(item, lane, deadline),
            operation_deadline(deadline.expires_at, shutdown=deadline.shutdown),
        ):
            original = self._require_receipt(item, lane)
            self._reject_foreign_owner(original, item, lane)
            if (
                self._binding(original)
                != replace(binding, dirty_claim=replace(claim, state="consumed"))
                or original.path != self._implementation_path(item)
                or not self._path_is_registered_to_repository(original.path)
                or self._head_revision(original.path) != original.revision
                or self._head_branch(original.path) != f"refs/heads/{claim.branch}"
            ):
                raise SourceWorkspaceError("dirty direct publication ownership changed")
            active = True
            advanced = False

            def advance(head: str) -> None:
                nonlocal advanced
                if (
                    not active
                    or advanced
                    or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", head) is None
                ):
                    raise SourceWorkspaceError("dirty direct publication advancement is invalid")
                if self._read_receipt(item, lane) != original:
                    raise SourceWorkspaceError("dirty direct publication receipt changed")
                parents = _git(
                    original.path, "rev-list", "--parents", "-n", "1", head
                ).stdout.split()
                successor = replace(original, revision=head, generation=original.generation + 1)
                if parents != [head, original.revision] or not self._physical_matches_receipt(
                    successor
                ):
                    raise SourceWorkspaceError("dirty direct controlled commit is unconfirmed")
                self._write_receipt(successor)
                if self._read_receipt(item, lane) != successor:
                    raise SourceWorkspaceError("dirty direct commit receipt write is unconfirmed")
                advanced = True

            try:
                yield advance
            finally:
                active = False

    def finish_dirty_direct_publication(
        self,
        item_number: int,
        *,
        expected_head: str,
        pr_number: int,
        expected_binding: WorkspaceBinding | None = None,
        deadline: _PreparationDeadline | None = None,
    ) -> SourceWorkspaceReceipt:
        """Restore a clean v1 receipt after the host verifies strict PR creation."""
        if type(pr_number) is not int or pr_number < 1:
            raise SourceWorkspaceError("dirty direct PR identity is invalid")
        lane = SourceLane.IMPLEMENTATION
        if deadline is None:
            deadline = _PreparationDeadline(time.monotonic() + 45.0, time.monotonic)
        with (
            self._acquire_lane(item_number, lane, deadline),
            operation_deadline(deadline.expires_at, shutdown=deadline.shutdown),
        ):
            original = self._require_receipt(item_number, lane)
            self._reject_foreign_owner(original, item_number, lane)
            if (
                original.schema_version != 2
                or original.dirty_claim is None
                or original.dirty_claim.state != "consumed"
                or original.revision != expected_head
                or original.revision == original.dirty_claim.reservation_base_sha
                or (
                    expected_binding is not None
                    and self._binding(original)
                    != self._published_dirty_binding(expected_binding, expected_head)
                )
                or not self._physical_matches_receipt(original)
            ):
                raise SourceWorkspaceError("dirty direct completed publication changed")
            successor = replace(
                original,
                schema_version=1,
                dirty_claim=None,
                generation=original.generation + 1,
                obligations=tuple(
                    value for value in original.obligations if value != "dirty-direct-continuation"
                ),
            )
            self._write_receipt(successor)
            if self._read_receipt(item_number, lane) != successor:
                raise SourceWorkspaceError("dirty direct completion write is unconfirmed")
            return successor

    @staticmethod
    def _published_dirty_binding(binding: WorkspaceBinding, head: str) -> WorkspaceBinding:
        """Describe the one controlled child of an armed writer binding."""
        claim = binding.dirty_claim
        if binding.schema_version != 2 or claim is None or claim.state != "armed":
            raise SourceWorkspaceError("dirty direct completed binding is invalid")
        return replace(
            binding,
            revision=head,
            generation=binding.generation + 1,
            dirty_claim=replace(claim, state="consumed"),
        )

    def _validate_dirty_claim_physical(
        self,
        receipt: SourceWorkspaceReceipt,
        claim: DirtyDirectClaim,
        *,
        deadline: _PreparationDeadline | None = None,
    ) -> None:
        """Check exact owner, branch, revision, and pending content under the lease."""
        if (
            receipt.detached
            or receipt.branch != claim.branch
            or receipt.revision != claim.reservation_base_sha
            or receipt.path != self._implementation_path(receipt.item_number)
            or receipt.path.resolve(strict=True) != receipt.path
            or not self._path_is_registered_to_repository(receipt.path, deadline=deadline)
            or self._head_revision(receipt.path, deadline=deadline) != receipt.revision
            or self._head_branch(receipt.path, deadline=deadline) != f"refs/heads/{claim.branch}"
            or _dirty_worktree_content_snapshot(
                receipt.path,
                timeout=30,
                shutdown=deadline.shutdown if deadline is not None else None,
            )
            != claim.content_snapshot()
        ):
            raise SourceWorkspaceError("dirty direct claim physical identity changed")

    @contextmanager
    def acquire(
        self,
        binding: WorkspaceBinding,
        *,
        allowed_tools: str = "",
        dirty_plan_identity: DirtyPlanIdentity | None = None,
        deadline: _PreparationDeadline | None = None,
        source_operation: DirtySourceOperation | None = None,
    ) -> Iterator[Path]:
        """Hold the lane lease and consume a dirty claim before provider execution."""
        if binding.item_number is None or binding.lane is None:
            raise SourceWorkspaceError("source workspace binding is incomplete")
        if deadline is None:
            deadline = _PreparationDeadline(time.monotonic() + 45.0, time.monotonic)
        with (
            self._acquire_lane(binding.item_number, binding.lane, deadline),
            operation_deadline(deadline.expires_at, shutdown=deadline.shutdown),
        ):
            receipt = self._read_receipt(binding.item_number, binding.lane)
            if receipt is None or self._binding(receipt) != binding:
                raise SourceWorkspaceError("source workspace receipt no longer matches binding")
            self._reject_foreign_owner(receipt, binding.item_number, binding.lane)
            try:
                if binding.schema_version == 1 and not self._path_is_registered_to_repository(
                    receipt.path, deadline=deadline
                ):
                    raise SourceWorkspaceError("source workspace registration changed")
                if source_operation is not None:
                    validated = self._validate_dirty_operation(
                        receipt, binding, source_operation, deadline
                    )
                    with _dirty_workspace_permit(validated) as permit:
                        yield validate_workspace_binding(
                            validated,
                            allowed_tools=allowed_tools,
                            dirty_permit=permit,
                            remaining_timeout=deadline.remaining,
                            shutdown=deadline.shutdown,
                            expected_branch=receipt.branch if not validated.detached else None,
                        )
                    return
                if binding.schema_version == 1:
                    yield validate_workspace_binding(
                        binding,
                        allowed_tools=allowed_tools,
                        remaining_timeout=deadline.remaining,
                        shutdown=deadline.shutdown,
                        expected_branch=receipt.branch,
                    )
                    return
                claim = receipt.dirty_claim
                if (
                    claim is None
                    or claim.state != "armed"
                    or dirty_plan_identity
                    != DirtyPlanIdentity(
                        claim.plan_revision,
                        claim.plan_fingerprint,
                        claim.review_fingerprint,
                        claim.allowed_paths,
                    )
                ):
                    raise SourceWorkspaceError("dirty direct plan identity changed")
                self._reject_foreign_owner(receipt, binding.item_number, binding.lane)
                self._validate_dirty_claim_physical(receipt, claim, deadline=deadline)
                consumed = replace(receipt, dirty_claim=replace(claim, state="consumed"))
                self._write_receipt(consumed)
                if self._read_receipt(binding.item_number, binding.lane) != consumed:
                    raise SourceWorkspaceError("dirty direct consumption is unconfirmed")
                with _dirty_workspace_permit(binding) as permit:
                    cwd = validate_workspace_binding(
                        binding,
                        allowed_tools=allowed_tools,
                        dirty_permit=permit,
                        remaining_timeout=deadline.remaining,
                        shutdown=deadline.shutdown,
                    )
                    yield cwd
            except WorkspaceBindingError as exc:
                raise SourceWorkspaceError(str(exc)) from exc

    def _validate_dirty_operation(
        self,
        receipt: SourceWorkspaceReceipt,
        binding: WorkspaceBinding,
        operation: DirtySourceOperation,
        deadline: _PreparationDeadline,
    ) -> WorkspaceBinding:
        """Check one dirty operation under the current source-lane lease."""
        if (
            not isinstance(operation, DirtySourceOperation)
            or binding.schema_version != 1
            or binding.lane is not SourceLane.IMPLEMENTATION
            or binding.detached
            or not receipt.branch
            or receipt.path != self._implementation_path(receipt.item_number)
        ):
            raise SourceWorkspaceError("dirty source operation does not match its receipt")
        snapshot = _dirty_worktree_snapshot_evidence(
            receipt.path, timeout=max(1, int(deadline.remaining())), shutdown=deadline.shutdown
        )
        if operation.content_snapshot and snapshot.snapshot != dict(operation.content_snapshot):
            raise SourceWorkspaceError("dirty source content changed")
        if operation.kind == "test-fix" and not set(snapshot.changed_paths).issubset(
            operation.allowed_paths
        ):
            raise SourceWorkspaceError("dirty source contains unrelated files")
        if operation.kind != "rebase-conflict":
            return binding
        manager = WorktreeManager(repo_root=self.repo_root, base_dir=self.base_dir)
        if (
            manager._detached_rebase_head_ref(
                receipt.path,
                common_git_dir=self.common_dir,
                timeout=max(1, int(deadline.remaining())),
                shutdown=deadline.shutdown,
            )
            != f"refs/heads/{receipt.branch}"
        ):
            raise SourceWorkspaceError("paused rebase identity changed")
        return replace(binding, revision=operation.paused_head_sha, detached=True)

    @contextmanager
    def _acquire_lane(
        self, item_number: int, lane: SourceLane, deadline: _PreparationDeadline
    ) -> Iterator[None]:
        """Wait for one lane within its deadline and cancellation request."""
        while True:
            deadline.remaining()
            with ExitStack() as stack:
                try:
                    stack.enter_context(
                        file_lock(
                            self._lane_lock_path(item_number, lane),
                            require_exclusive=True,
                            blocking=False,
                        )
                    )
                except LockUnavailableError:
                    wait_s = min(0.1, deadline.remaining())
                    if deadline.shutdown is None:
                        time.sleep(wait_s)
                    else:
                        deadline.shutdown.wait(wait_s)
                    continue
                deadline.remaining()
                yield
                return

    def add_obligation(self, item_number: int, lane: SourceLane, name: str) -> None:
        """Record a durable source-reading obligation that blocks cleanup."""
        with file_lock(self._lane_lock_path(item_number, lane), require_exclusive=True):
            receipt = self._require_receipt(item_number, lane)
            if name not in receipt.obligations:
                self._write_receipt(replace(receipt, obligations=(*receipt.obligations, name)))

    def finish_obligation(self, item_number: int, lane: SourceLane, name: str) -> None:
        """Mark a durable source-reading obligation terminal."""
        with file_lock(self._lane_lock_path(item_number, lane), require_exclusive=True):
            receipt = self._require_receipt(item_number, lane)
            self._write_receipt(
                replace(
                    receipt,
                    obligations=tuple(value for value in receipt.obligations if value != name),
                )
            )

    def cleanup(
        self,
        item_number: int,
        lane: SourceLane,
        *,
        expected_revision: str | None = None,
        expected_detached: bool | None = None,
        physical_cleanup: Callable[[], None] | None = None,
        deadline: _PreparationDeadline | None = None,
    ) -> None:
        """Remove one clean terminal lane and its receipt under the lane lock."""
        if deadline is None:
            deadline = _PreparationDeadline(
                time.monotonic() + cast(float, remaining_operation_timeout(45.0)),
                time.monotonic,
                current_operation_shutdown(),
            )
        with (
            self._acquire_lane(item_number, lane, deadline),
            operation_deadline(deadline.expires_at, shutdown=deadline.shutdown),
        ):
            receipt = self._read_receipt(item_number, lane)
            if receipt is None:
                if physical_cleanup is None:
                    raise SourceWorkspaceError("source workspace receipt does not exist")
                physical_cleanup()
                return
            self._reject_foreign_owner(receipt, item_number, lane)
            expected_path = self.path_for(item_number, lane).resolve()
            if (
                receipt.repository != self.repository
                or receipt.repository_identity != self.repository_identity
                or receipt.item_number != item_number
                or receipt.lane is not lane
                or receipt.path.resolve() != expected_path
            ):
                raise SourceWorkspaceError("source workspace receipt cleanup identity is invalid")
            if expected_revision is not None and receipt.revision != expected_revision:
                raise SourceWorkspaceError("source workspace receipt revision changed")
            if expected_detached is not None and receipt.detached is not expected_detached:
                raise SourceWorkspaceError("source workspace receipt checkout changed")
            if receipt.obligations:
                raise SourceWorkspaceError("source workspace still has active obligations")
            if receipt.path.exists() and self._is_dirty(receipt.path, deadline=deadline):
                raise SourceWorkspaceError(
                    f"source workspace is dirty and preserved: {receipt.path}"
                )
            if physical_cleanup is not None:
                physical_cleanup()
            else:
                with file_lock(
                    WorktreeManager.git_metadata_lock_path(self.repo_root),
                    blocking=False,
                    require_exclusive=True,
                ):
                    deadline.remaining()
                    result = _git(
                        self.repo_root,
                        "worktree",
                        "remove",
                        str(receipt.path),
                        check=False,
                        deadline=deadline,
                    )
                    if result.returncode and receipt.path.exists():
                        raise SourceWorkspaceError(
                            result.stderr.strip() or "worktree cleanup failed"
                        )
            receipt_path = self._receipt_path(item_number, lane)
            try:
                receipt_path.unlink(missing_ok=True)
            except OSError as exc:
                raise SourceWorkspaceError(
                    f"source workspace receipt removal failed at {receipt_path}: {exc}"
                ) from exc

    def compare_and_swap_guard(
        self, item_number: int, *, expected: str | None, revision: str
    ) -> str:
        """CAS-update the stable guard ref without creating another worktree."""
        new = _git(self.repo_root, "rev-parse", f"{revision}^{{commit}}").stdout.strip()
        ref = f"refs/heads/{self.guard_branch(item_number)}"
        old = expected or ("0" * 40)
        with operation_file_lock(WorktreeManager.git_metadata_lock_path(self.repo_root)):
            result = _git(self.repo_root, "update-ref", ref, new, old, check=False)
        if result.returncode:
            raise SourceWorkspaceError("guard branch compare-and-swap failed")
        return new

    def _replace_worktree(
        self,
        path: Path,
        revision: str,
        branch: str | None,
        *,
        owns_branch: bool,
        deadline: _PreparationDeadline | None = None,
    ) -> None:
        lock_path = WorktreeManager.git_metadata_lock_path(self.repo_root)
        lock_options: dict[str, Any] = {}
        if deadline is not None:
            lock_options = {"blocking": False, "require_exclusive": True}
        try:
            metadata_lock = (
                file_lock(lock_path, **lock_options)
                if deadline is not None
                else operation_file_lock(lock_path)
            )
            with metadata_lock:
                self._replace_worktree_locked(
                    path,
                    revision,
                    branch,
                    owns_branch=owns_branch,
                    deadline=deadline,
                )
        except LockUnavailableError as exc:
            if deadline is None:  # pragma: no cover - blocking acquisition does not use this path
                raise
            raise SourceWorkspacePreparationError(
                SourceWorkspacePreparationCause.GIT_METADATA_LOCK_UNAVAILABLE,
                f"Git metadata lock is unavailable: {lock_path}",
            ) from exc

    def _replace_worktree_locked(
        self,
        path: Path,
        revision: str,
        branch: str | None,
        *,
        owns_branch: bool,
        deadline: _PreparationDeadline | None,
    ) -> None:
        """Replace a worktree while the shared Git metadata lock is held."""
        exists = branch is not None and (
            _git(
                self.repo_root,
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
                check=False,
                deadline=deadline,
            ).returncode
            == 0
        )
        if exists and not owns_branch:
            raise SourceWorkspaceError("source workspace branch is not owned by this lane")
        if path.exists():
            removed = _git(
                self.repo_root,
                "worktree",
                "remove",
                str(path),
                check=False,
                deadline=deadline,
            )
            if removed.returncode:
                raise SourceWorkspaceError(removed.stderr.strip() or "worktree removal failed")
        args = ["worktree", "add"]
        if branch is None:
            args.extend(["--detach", str(path), revision])
        else:
            if exists:
                args.extend(["-B", branch, str(path), revision])
            else:
                args.extend(["-b", branch, str(path), revision])
        added = _git(self.repo_root, *args, check=False, deadline=deadline)
        if added.returncode:
            if branch is not None and exists:
                raise SourceWorkspaceError(
                    "source workspace branch could not be synchronized safely"
                )
            raise SourceWorkspaceError(added.stderr.strip() or "worktree creation failed")

    @staticmethod
    def _is_dirty(path: Path, *, deadline: _PreparationDeadline | None = None) -> bool:
        result = _git(
            path,
            "status",
            "--porcelain",
            "--untracked-files=all",
            check=False,
            deadline=deadline,
        )
        if result.returncode:
            raise SourceWorkspaceError(f"cannot inspect source workspace: {path}")
        return bool(result.stdout)

    @staticmethod
    def _head_revision(path: Path, *, deadline: _PreparationDeadline | None = None) -> str:
        """Return the physical worktree HEAD or fail before any replacement."""
        result = _git(path, "rev-parse", "HEAD", check=False, deadline=deadline)
        if result.returncode:
            raise SourceWorkspaceError(f"cannot inspect source workspace revision: {path}")
        return result.stdout.strip()

    @staticmethod
    def _head_branch(path: Path, *, deadline: _PreparationDeadline | None = None) -> str | None:
        """Return the physical local branch ref, or ``None`` for detached HEAD."""
        result = _git(path, "symbolic-ref", "-q", "HEAD", check=False, deadline=deadline)
        if result.returncode == 1:
            return None
        branch = result.stdout.strip()
        if result.returncode or not branch.startswith("refs/heads/"):
            raise SourceWorkspaceError(f"cannot inspect source workspace branch: {path}")
        return branch

    def _binding(self, receipt: SourceWorkspaceReceipt) -> WorkspaceBinding:
        return receipt.to_binding(self.repo_root)

    def _terminal_path(self, item_number: int) -> Path:
        return self.state_dir / f"{item_number}-impl-terminal.json"

    def _capture_terminal_failure(
        self, item_number: int, failure: SourceWorkspaceTerminalError
    ) -> SourceWorkspaceTerminalReference:
        """Capture the final failure while the handoff holds the lane lock."""
        lane = SourceLane.IMPLEMENTATION
        path = self.path_for(item_number, lane)
        branch, base = failure.requested_branch, failure.requested_base_sha
        pair_valid = self._terminal_request_valid(branch, base)
        if not pair_valid:
            branch, base = None, None
        payload: dict[str, object] = {
            "schema_version": 1,
            "repository": self.repository,
            "repository_identity": self.repository_identity,
            "ownership_key": self.ownership_key(item_number, lane),
            "item_number": item_number,
            "lane": lane.value,
            "path": str(path),
            "transition_identity": None,
            "transition_journal_digest": None,
            "transition_content_sha256": None,
            "phase": "unproven_legacy",
            "source_receipt_sha256": None,
            "requested_branch": branch,
            "requested_base_sha": base,
            "reservation_disposition": "preserve",
            "outcome": "manual_recovery_required",
            "cause": "source_workspace_recovery_receipt_invalid",
            "action": f"Preserve {path} and its records. Obtain valid ownership evidence.",
        }
        source_receipt: SourceWorkspaceReceipt | None = None
        try:
            source_bytes = _terminal_read_bytes(self._receipt_path(item_number, lane))
            payload["source_receipt_sha256"] = hashlib.sha256(source_bytes).hexdigest()
            source_payload = _terminal_json_object(source_bytes)
            source_receipt = SourceWorkspaceReceipt.from_dict(source_payload)
            if _terminal_json_digest(source_payload) != _terminal_json_digest(
                source_receipt.to_dict()
            ):
                raise SourceWorkspaceError("source workspace receipt field types are invalid")
            self._reject_foreign_owner(source_receipt, item_number, lane)
        except (OSError, ValueError, SourceWorkspaceError):
            source_receipt = None
        try:
            journal = self._read_writer_transition(item_number)
            if journal is not None:
                payload.update(
                    transition_identity=self._transition_path(item_number).name,
                    transition_journal_digest=journal.journal_digest,
                    transition_content_sha256=_terminal_json_digest(journal.to_dict()),
                    phase=journal.phase,
                )
                if (
                    pair_valid
                    and source_receipt in (journal.predecessor, journal.successor)
                    and journal.successor.branch is not None
                    and branch == journal.successor.branch
                    and base == journal.successor.revision
                ):
                    payload.update(
                        outcome="incomplete",
                        cause="source_workspace_transition_incomplete",
                        action=self._terminal_phase_action(
                            journal.successor.branch, journal.successor.revision, journal.phase
                        ),
                    )
            elif pair_valid:
                receipt = source_receipt
                if receipt is not None and path.exists():
                    observed_branch = self._head_branch(path)
                    observed_revision = self._head_revision(path)
                    expected_branch = None if receipt.detached else f"refs/heads/{receipt.branch}"
                    if observed_branch != expected_branch or observed_revision != receipt.revision:
                        payload.update(
                            cause="source_workspace_legacy_unproven",
                            action=(
                                f"Preserve {path} and {self._receipt_path(item_number, lane)}. "
                                f"The recorded branch/revision is "
                                f"{expected_branch!r}/{receipt.revision}; "
                                f"the observed branch/revision is "
                                f"{observed_branch!r}/{observed_revision}. "
                                "The transition proof is missing. Obtain the ownership record or "
                                "separately reviewed operator recovery."
                            ),
                        )
        except (OSError, ValueError, SourceWorkspaceError, subprocess.SubprocessError):
            pass
        payload["terminal_content_sha256"] = _terminal_json_digest(payload)
        target = self._terminal_path(item_number)
        if (
            self.state_dir.is_symlink()
            or self.state_dir.resolve() != self.state_dir
            or target.is_symlink()
        ):
            raise SourceWorkspaceError("source workspace terminal path is invalid")
        write_secure(target, json.dumps(payload, sort_keys=True, separators=(",", ":")))
        self._fsync_state_dir()
        reference = SourceWorkspaceTerminalReference(
            target.name, str(payload["terminal_content_sha256"])
        )
        self._read_terminal_failure(item_number, reference)
        return reference

    @staticmethod
    def _terminal_request_valid(branch: object, base: object) -> bool:
        if not isinstance(branch, str) or not branch or len(branch) > 255:
            return False
        if (
            branch.startswith(("-", "/", "."))
            or branch.endswith(("/", "."))
            or any(
                part.startswith(".") or part.endswith(".lock") or not part
                for part in branch.split("/")
            )
            or any(token in branch for token in ("..", "@{", "//"))
            or branch == "@"
            or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", branch)
        ):
            return False
        return (
            isinstance(base, str)
            and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base) is not None
        )

    @staticmethod
    def _terminal_phase_action(branch: str, base: str, phase: str) -> str:
        meanings = {
            "prepared": "authorization is durable",
            "predecessor_removing": "predecessor removal is pending",
            "successor_creating": "writer creation is pending",
            "successor_created": "authority creation is pending",
            "authority_minted": "writer claim is pending",
            "receipt_pending": "source receipt write/readback is pending",
        }
        return (
            f"Preserve all state. Retry the same request {branch} at {base} through the source "
            f"manager only. Durable phase {phase}: {meanings[phase]}."
        )

    def read_terminal_failure(
        self, item_number: int, reference: SourceWorkspaceTerminalReference
    ) -> SourceWorkspaceTerminalView:
        """Verify the terminal snapshot and its exact source and journal content."""
        try:
            with file_lock(self._lane_lock_path(item_number, SourceLane.IMPLEMENTATION)):
                return self._read_terminal_failure(item_number, reference)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise SourceWorkspaceError("source workspace terminal evidence is invalid") from exc

    def _read_terminal_failure(  # noqa: C901
        self, item_number: int, reference: SourceWorkspaceTerminalReference
    ) -> SourceWorkspaceTerminalView:
        if not isinstance(reference, SourceWorkspaceTerminalReference):
            raise SourceWorkspaceError("source workspace terminal reference is invalid")
        SourceWorkspaceTerminalReference.from_dict(reference.to_dict())
        if reference.identity != self._terminal_path(item_number).name:
            raise SourceWorkspaceError("source workspace terminal identity changed")
        payload = _terminal_json_object(_terminal_read_bytes(self._terminal_path(item_number)))
        fields = {
            "schema_version",
            "repository",
            "repository_identity",
            "ownership_key",
            "item_number",
            "lane",
            "path",
            "transition_identity",
            "transition_journal_digest",
            "transition_content_sha256",
            "phase",
            "source_receipt_sha256",
            "requested_branch",
            "requested_base_sha",
            "reservation_disposition",
            "outcome",
            "cause",
            "action",
            "terminal_content_sha256",
        }
        if set(payload) != fields:
            raise SourceWorkspaceError("source workspace terminal schema is invalid")
        digest = payload.pop("terminal_content_sha256")
        if (
            not _terminal_digest_valid(digest)
            or digest != reference.content_sha256
            or digest != _terminal_json_digest(payload)
        ):
            raise SourceWorkspaceError("source workspace terminal content changed")
        lane = SourceLane.IMPLEMENTATION
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != 1
            or type(payload["item_number"]) is not int
            or payload["item_number"] != item_number
            or payload["repository"] != self.repository
            or payload["repository_identity"] != self.repository_identity
            or payload["ownership_key"] != self.ownership_key(item_number, lane)
            or payload["lane"] != lane.value
            or payload["path"] != str(self.path_for(item_number, lane))
            or Path(payload["path"]).resolve() != Path(payload["path"])
            or payload["reservation_disposition"] != "preserve"
        ):
            raise SourceWorkspaceError("source workspace terminal ownership is invalid")
        branch, base = payload["requested_branch"], payload["requested_base_sha"]
        if (branch is not None or base is not None) and not self._terminal_request_valid(
            branch, base
        ):
            raise SourceWorkspaceError("source workspace terminal request is invalid")
        phase, cause, outcome, action = (
            payload[key] for key in ("phase", "cause", "outcome", "action")
        )
        if (
            not isinstance(phase, str)
            or phase not in _TRANSITION_PHASES | {"unproven_legacy"}
            or not isinstance(cause, str)
            or cause
            not in {
                "source_workspace_transition_incomplete",
                "source_workspace_legacy_unproven",
                "source_workspace_recovery_receipt_invalid",
            }
            or not isinstance(action, str)
            or not action
            or len(action) > 8192
            or not isinstance(outcome, str)
            or outcome not in {"incomplete", "manual_recovery_required"}
            or (outcome == "incomplete") != (cause == "source_workspace_transition_incomplete")
            or (cause == "source_workspace_legacy_unproven" and phase != "unproven_legacy")
        ):
            raise SourceWorkspaceError("source workspace terminal result is invalid")
        identity = payload["transition_identity"]
        if identity is not None:
            if identity != self._transition_path(item_number).name:
                raise SourceWorkspaceError("source workspace terminal journal identity changed")
            journal = self._read_writer_transition(item_number)
            if (
                journal is None
                or phase != journal.phase
                or payload["transition_journal_digest"] != journal.journal_digest
                or payload["transition_content_sha256"] != _terminal_json_digest(journal.to_dict())
                or not _terminal_digest_valid(payload["transition_content_sha256"])
                or not _terminal_digest_valid(payload["transition_journal_digest"])
            ):
                raise SourceWorkspaceError("source workspace terminal journal changed")
            if cause == "source_workspace_transition_incomplete" and (
                branch != journal.successor.branch
                or base != journal.successor.revision
                or action != self._terminal_phase_action(branch, base, phase)
            ):
                raise SourceWorkspaceError("source workspace terminal request changed")
        elif (
            payload["transition_journal_digest"] is not None
            or payload["transition_content_sha256"] is not None
            or phase != "unproven_legacy"
            or cause == "source_workspace_transition_incomplete"
        ):
            raise SourceWorkspaceError("source workspace terminal journal proof is missing")
        source_digest = payload["source_receipt_sha256"]
        if (
            not _terminal_digest_valid(source_digest)
            or hashlib.sha256(
                _terminal_read_bytes(self._receipt_path(item_number, lane))
            ).hexdigest()
            != source_digest
        ):
            raise SourceWorkspaceError("source workspace terminal source receipt changed")
        return SourceWorkspaceTerminalView(
            phase, outcome, cause, action, Path(payload["path"]), branch, base
        )

    def _transition_path(self, item_number: int) -> Path:
        """Return the private implementation transition journal path."""
        return self.state_dir / f"{item_number}-impl-transition.json"

    def _read_writer_transition(
        self, item_number: int
    ) -> _ImplementationWriterTransitionJournal | None:
        """Read and validate one pending implementation transition."""
        path = self._transition_path(item_number)
        if path.is_symlink():
            raise SourceWorkspaceError(f"refusing invalid source workspace transition: {path}")
        if not path.exists():
            return None
        if not path.is_file():
            raise SourceWorkspaceError(f"refusing invalid source workspace transition: {path}")
        try:
            payload = _terminal_json_object(_terminal_read_bytes(path))
        except (OSError, ValueError) as exc:
            raise SourceWorkspaceError(f"cannot read source workspace transition: {path}") from exc
        if not isinstance(payload, dict):
            raise SourceWorkspaceError("source workspace transition must be an object")
        journal = _ImplementationWriterTransitionJournal.from_dict(payload)
        expected_path = self._implementation_path(item_number)
        if (
            journal.repository != self.repository
            or journal.repository_identity != self.repository_identity
            or journal.ownership_key != self.ownership_key(item_number, SourceLane.IMPLEMENTATION)
            or journal.item_number != item_number
            or journal.predecessor.path != expected_path
            or journal.successor.path != expected_path
            or journal.predecessor.item_number != item_number
            or journal.successor.item_number != item_number
            or journal.predecessor.lane is not SourceLane.IMPLEMENTATION
            or journal.successor.lane is not SourceLane.IMPLEMENTATION
            or journal.predecessor.repository != journal.repository
            or journal.successor.repository != journal.repository
            or journal.predecessor.repository_identity != journal.repository_identity
            or journal.successor.repository_identity != journal.repository_identity
            or journal.predecessor.ownership_key != journal.ownership_key
            or journal.successor.ownership_key != journal.ownership_key
            or journal.predecessor.generation < 1
            or journal.successor.generation != journal.predecessor.generation + 1
            or journal.predecessor.obligations != journal.successor.obligations
            or journal.successor.detached
            or journal.successor.branch is None
            or (journal.predecessor.detached and journal.predecessor.branch is not None)
            or (not journal.predecessor.detached and journal.predecessor.branch is None)
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", journal.predecessor.revision) is None
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", journal.successor.revision) is None
            or (
                journal.predecessor.branch == journal.successor.branch
                and not journal.predecessor.detached
                and journal.target_ref_revision != journal.predecessor.revision
            )
            or (
                journal.transition == "direct"
                and journal.predecessor.branch != journal.successor.branch
                and journal.target_ref_revision is not None
            )
            or (
                journal.transition == "adopted"
                and journal.target_ref_revision
                not in {
                    None,
                    journal.predecessor.revision,
                    journal.successor.revision,
                }
            )
        ):
            raise SourceWorkspaceError("source workspace transition identity is invalid")
        return journal

    def _write_writer_transition(self, journal: _ImplementationWriterTransitionJournal) -> None:
        """Write and flush a transition journal before Git replacement."""
        if self.state_dir.is_symlink() or (self.state_dir.exists() and not self.state_dir.is_dir()):
            raise SourceWorkspaceError("source workspace transition state directory is invalid")
        path = self._transition_path(journal.item_number)
        write_secure(path, json.dumps(journal.to_dict(), sort_keys=True, indent=2) + "\n")
        self._fsync_state_dir()

    def _validate_writer_transition_digest(self, item_number: int, expected_digest: str) -> None:
        """Require the pending journal to match one armed capability."""
        journal = self._read_writer_transition(item_number)
        if journal is None:
            raise SourceWorkspaceError("source workspace transition journal is missing")
        if journal.journal_digest != expected_digest:
            raise SourceWorkspaceError("source workspace transition journal digest changed")

    def _update_writer_transition_phase(
        self, item_number: int, expected_digest: str, phase: str
    ) -> None:
        """Durably advance a pending transition phase."""
        if phase not in _TRANSITION_PHASES:
            raise SourceWorkspaceError("source workspace transition phase is invalid")
        journal = self._read_writer_transition(item_number)
        if journal is None:
            raise SourceWorkspaceError("source workspace transition journal is missing")
        if journal.journal_digest != expected_digest:
            raise SourceWorkspaceError("source workspace transition journal digest changed")
        self._write_writer_transition(replace(journal, phase=phase))

    def _remove_writer_transition(
        self, item_number: int, expected_digest: str | None = None
    ) -> None:
        """Remove a committed transition journal and flush the directory."""
        path = self._transition_path(item_number)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise SourceWorkspaceError("source workspace transition journal is invalid")
        if expected_digest is not None:
            journal = self._read_writer_transition(item_number)
            if journal is None:
                raise SourceWorkspaceError("source workspace transition journal is missing")
            if journal.journal_digest != expected_digest:
                raise SourceWorkspaceError("source workspace transition journal digest changed")
        try:
            path.unlink(missing_ok=True)
            self._fsync_state_dir()
        except OSError as exc:
            raise SourceWorkspaceError(
                "source workspace transition journal removal failed"
            ) from exc

    def _fsync_state_dir(self) -> None:
        """Flush the state directory after a journal rename or removal."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(self.state_dir, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _reconcile_writer_transition(
        self, item_number: int, *, finalize_exact_successor: bool
    ) -> None:
        """Recover one pending transition before or after a writer handoff."""
        with operation_file_lock(WorktreeManager.git_metadata_lock_path(self.repo_root)):
            self._reconcile_writer_transition_locked(
                item_number,
                finalize_exact_successor=finalize_exact_successor,
            )

    def _reconcile_writer_transition_locked(
        self, item_number: int, *, finalize_exact_successor: bool
    ) -> None:
        """Recover one transition while the repository metadata lock is held."""
        journal = self._read_writer_transition(item_number)
        if journal is None:
            return
        current = self._read_receipt(item_number, SourceLane.IMPLEMENTATION)
        if current is None:
            raise SourceWorkspaceError("source workspace transition predecessor receipt is missing")
        if current == journal.successor:
            if not self._physical_matches_receipt(current):
                raise SourceWorkspaceError("source workspace transition successor is invalid")
            self._remove_writer_transition(item_number, journal.journal_digest)
            return
        if current != journal.predecessor:
            raise SourceWorkspaceError("source workspace transition receipt is stale")
        if (
            finalize_exact_successor
            and journal.phase
            in {
                "successor_created",
                "authority_minted",
                "receipt_pending",
            }
            and self._physical_matches_receipt(journal.successor)
        ):
            self._write_receipt(journal.successor)
            self._remove_writer_transition(item_number, journal.journal_digest)
            return
        try:
            self._restore_transition_predecessor_locked(journal)
        except SourceWorkspaceError:
            if not finalize_exact_successor:
                return
            raise
        self._remove_writer_transition(item_number, journal.journal_digest)

    def _physical_matches_receipt(
        self, receipt: SourceWorkspaceReceipt, *, deadline: _PreparationDeadline | None = None
    ) -> bool:
        """Return whether a checkout exactly matches a durable receipt."""
        if (
            receipt.path.is_symlink()
            or not receipt.path.exists()
            or not self._path_is_registered_to_repository(receipt.path, deadline=deadline)
            or self._is_dirty(receipt.path, deadline=deadline)
        ):
            return False
        branch = self._head_branch(receipt.path, deadline=deadline)
        expected_branch = None if receipt.detached else f"refs/heads/{receipt.branch}"
        return (
            self._head_revision(receipt.path, deadline=deadline) == receipt.revision
            and branch == expected_branch
        )

    def _path_is_registered_to_repository(
        self, path: Path, *, deadline: _PreparationDeadline | None = None
    ) -> bool:
        """Return whether Git binds the exact path to this repository metadata."""
        manager = WorktreeManager(repo_root=self.repo_root, base_dir=self.base_dir)
        execution_options: dict[str, Any] = {}
        if deadline is not None:
            execution_options = {
                "timeout": max(1, int(deadline.remaining())),
                "shutdown": deadline.shutdown,
            }
        try:
            if manager._registered_worktree_at_path(path, **execution_options) is None:
                return False
            common = _git(path, "rev-parse", "--git-common-dir", check=False, deadline=deadline)
            if common.returncode != 0 or not common.stdout.strip():
                return False
            common_path = Path(common.stdout.strip())
            if not common_path.is_absolute():
                common_path = path / common_path
            return common_path.resolve(strict=True) == self.common_dir
        except (InterruptedError, SourceWorkspacePreparationError):
            raise
        except (OSError, RuntimeError):
            return False

    def _require_registered_transition_path(self, path: Path) -> None:
        """Require one recovery path to use this repository metadata."""
        if not self._path_is_registered_to_repository(path):
            raise SourceWorkspaceError(
                "source workspace transition checkout is not registered to this repository"
            )

    def _restore_transition_predecessor(
        self, journal: _ImplementationWriterTransitionJournal
    ) -> None:
        """Restore the exact predecessor after an incomplete replacement."""
        with operation_file_lock(WorktreeManager.git_metadata_lock_path(self.repo_root)):
            self._restore_transition_predecessor_locked(journal)

    def _restore_transition_predecessor_locked(
        self, journal: _ImplementationWriterTransitionJournal
    ) -> None:
        """Restore an exact predecessor while the Git metadata lock is held."""
        predecessor = journal.predecessor
        path = predecessor.path
        if path.is_symlink():
            raise SourceWorkspaceError("source workspace transition path is invalid")
        if path.exists():
            self._require_registered_transition_path(path)
            if self._is_dirty(path):
                raise SourceWorkspaceError("source workspace transition checkout is dirty")
            physical_revision = self._head_revision(path)
            physical_branch = self._head_branch(path)
            if physical_revision == predecessor.revision and physical_branch == (
                None if predecessor.detached else f"refs/heads/{predecessor.branch}"
            ):
                self._restore_transition_target_ref(journal)
                return
            expected_successor_branch = f"refs/heads/{journal.successor.branch}"
            if not (
                physical_revision == journal.successor.revision
                and physical_branch == expected_successor_branch
            ):
                raise SourceWorkspaceError("source workspace transition checkout is ambiguous")
            removed = _git(self.repo_root, "worktree", "remove", str(path), check=False)
            if removed.returncode:
                raise SourceWorkspaceError(
                    removed.stderr.strip() or "source workspace transition removal failed"
                )
        self._restore_transition_target_ref(journal)
        if not predecessor.detached:
            if predecessor.branch is None:
                raise SourceWorkspaceError(
                    "source workspace transition predecessor branch is invalid"
                )
            branch_head = _git(
                self.repo_root,
                "rev-parse",
                "--verify",
                f"refs/heads/{predecessor.branch}",
                check=False,
            )
            if branch_head.returncode or branch_head.stdout.strip() != predecessor.revision:
                raise SourceWorkspaceError("source workspace transition predecessor branch changed")
            holder = self._branch_holder(predecessor.branch)
            if holder is not None and holder.resolve() != path:
                raise SourceWorkspaceError("source workspace transition predecessor branch is held")
        self._replace_worktree_locked(
            path,
            predecessor.revision,
            branch=None if predecessor.detached else predecessor.branch,
            owns_branch=not predecessor.detached,
            deadline=None,
        )
        if not self._physical_matches_receipt(predecessor):
            raise SourceWorkspaceError("source workspace transition predecessor recovery failed")

    def _local_branch_revision(self, branch: str) -> str | None:
        """Return one local branch revision, or ``None`` when it is absent."""
        ref = f"refs/heads/{branch}"
        present = _git(self.repo_root, "show-ref", "--verify", "--quiet", ref, check=False)
        if present.returncode == 1:
            return None
        if present.returncode != 0:
            raise SourceWorkspaceError("cannot inspect source workspace transition target branch")
        result = _git(self.repo_root, "rev-parse", "--verify", ref, check=False)
        if result.returncode != 0:
            raise SourceWorkspaceError("cannot inspect source workspace transition target branch")
        revision = result.stdout.strip()
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision) is None:
            raise SourceWorkspaceError("source workspace transition target branch is invalid")
        return revision

    def _restore_transition_target_ref(
        self, journal: _ImplementationWriterTransitionJournal
    ) -> None:
        """Restore the journal-proved local target ref by compare-and-swap."""
        branch = journal.successor.branch
        if branch is None:  # pragma: no cover - rejected by journal validation
            raise SourceWorkspaceError("source workspace transition target branch is invalid")
        current = self._local_branch_revision(branch)
        prior = journal.target_ref_revision
        if current == prior:
            return
        holder = self._branch_holder(branch)
        if holder is not None:
            raise SourceWorkspaceError(
                f"source workspace transition target branch is held: {holder}"
            )
        if journal.phase not in {
            "successor_creating",
            "successor_created",
            "authority_minted",
            "receipt_pending",
        }:
            raise SourceWorkspaceError("source workspace transition target branch changed")
        if current != journal.successor.revision:
            raise SourceWorkspaceError("source workspace transition target branch changed")
        ref = f"refs/heads/{branch}"
        if prior is None:
            result = _git(
                self.repo_root,
                "update-ref",
                "-d",
                ref,
                current,
                check=False,
            )
        else:
            result = _git(
                self.repo_root,
                "update-ref",
                ref,
                prior,
                current,
                check=False,
            )
        if result.returncode != 0 or self._local_branch_revision(branch) != prior:
            raise SourceWorkspaceError("source workspace transition target branch recovery failed")

    def _branch_holder(self, branch: str) -> Path | None:
        """Return the attached or rebasing worktree that holds a local branch."""
        manager = WorktreeManager(repo_root=self.repo_root, base_dir=self.base_dir)
        try:
            return manager._worktree_holding_branch(branch)
        except RuntimeError as exc:
            raise SourceWorkspaceError("cannot inspect source workspace branch holder") from exc

    def _lane_lock_path(self, item_number: int, lane: SourceLane) -> Path:
        return WorktreeManager.source_lane_lock_path(self.repo_root, item_number, lane.value)

    def _receipt_path(self, item_number: int, lane: SourceLane) -> Path:
        return self.state_dir / f"{item_number}-{lane.value}.json"

    def _read_receipt(self, item_number: int, lane: SourceLane) -> SourceWorkspaceReceipt | None:
        path = self._receipt_path(item_number, lane)
        if path.is_symlink():
            raise SourceWorkspaceError(f"refusing symlinked source receipt: {path}")
        if not path.exists():
            return None
        try:
            payload = json.loads(_terminal_read_bytes(path).decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SourceWorkspaceError(f"cannot read source workspace receipt: {path}") from exc
        if not isinstance(payload, dict):
            raise SourceWorkspaceError("source workspace receipt must be an object")
        return SourceWorkspaceReceipt.from_dict(payload)

    def _require_receipt(self, item_number: int, lane: SourceLane) -> SourceWorkspaceReceipt:
        receipt = self._read_receipt(item_number, lane)
        if receipt is None:
            raise SourceWorkspaceError("source workspace receipt does not exist")
        return receipt

    def _write_receipt(self, receipt: SourceWorkspaceReceipt) -> None:
        path = self._receipt_path(receipt.item_number, receipt.lane)
        content = json.dumps(receipt.to_dict(), sort_keys=True, indent=2) + "\n"
        if len(content.encode("utf-8")) > _SOURCE_EVIDENCE_MAX_BYTES:
            raise SourceWorkspaceError("source workspace receipt exceeds the evidence limit")
        write_secure(path, content)
        self._fsync_state_dir()

    def _reject_foreign_owner(
        self,
        receipt: SourceWorkspaceReceipt | None,
        item_number: int,
        lane: SourceLane,
    ) -> None:
        if receipt is None:
            return
        expected_ownership_key = self.ownership_key(item_number, lane)
        if (
            receipt.ownership_key != expected_ownership_key
            or receipt.repository != self.repository
            or receipt.repository_identity != self.repository_identity
            or receipt.item_number != item_number
            or receipt.lane is not lane
        ):
            path = self.path_for(item_number, lane).resolve()
            receipt_path = self._receipt_path(item_number, lane).resolve()
            raise SourceWorkspaceError(
                f"source workspace is owned by another repository: {receipt.ownership_key}",
                recovery=self._recovery(
                    SourceWorkspaceRecoveryKind.FOREIGN_OWNER,
                    item_number=item_number,
                    path=path,
                    receipt_path=receipt_path,
                    manual_action=(
                        f"Use the repository run that owns {receipt.ownership_key}. Do not "
                        f"change {path} or {receipt_path} from this run."
                    ),
                ),
            )
