"""Provider-neutral workspace contracts for agent execution.

An agent receives a typed workspace binding rather than an ambient directory.
Source bindings are fail-closed: they identify one deterministic item lane and
the exact revision and generation that must still be present when execution
starts.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, fields
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Self

from hephaestus.config.child_environments import build_git_child_env
from hephaestus.utils.worktree_identity import source_worktree_name


class WorkspaceBindingError(RuntimeError):
    """Raised when a workspace binding is malformed or no longer valid."""


class WorkspaceKind(StrEnum):
    """Supported execution-directory trust classes."""

    SOURCE = "source"
    SESSION_ONLY = "session-only"
    EXTERNAL = "external"


class SourceLane(StrEnum):
    """The two source-reading lanes owned by an issue or pull request."""

    IMPLEMENTATION = "impl"
    REVIEW = "review"


_SOURCE_TOOLS = frozenset({"Read", "Glob", "Grep", "Write", "Edit", "Bash", "Agent"})
_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "cwd",
        "reusable_root",
        "repository",
        "ownership_key",
        "item_number",
        "lane",
        "revision",
        "generation",
        "detached",
    }
)


@dataclass(frozen=True, slots=True)
class DirtyPlanIdentity:
    """Identify independently validated plan and review content."""

    revision: int
    plan_fingerprint: str
    review_fingerprint: str
    allowed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DirtyDirectClaim:
    """Bind one dirty writer turn to its plan, content, and reservation."""

    branch: str
    reservation_base_sha: str
    plan_revision: int
    plan_fingerprint: str
    review_fingerprint: str
    allowed_paths: tuple[str, ...]
    index_sha256: str
    worktree_sha256: str
    untracked_sha256: str
    nonce: str
    state: str

    def to_dict(self) -> dict[str, object]:
        """Return claim data without an execution permit."""
        payload = asdict(self)
        payload["allowed_paths"] = list(self.allowed_paths)
        return payload

    @classmethod
    def from_dict(cls, payload: object) -> Self:
        """Parse the exact version 2 claim schema."""
        if not isinstance(payload, dict) or set(payload) != {field.name for field in fields(cls)}:
            raise WorkspaceBindingError("dirty claim schema mismatch")
        paths = payload["allowed_paths"]
        if not isinstance(paths, list) or not paths or any(not isinstance(p, str) for p in paths):
            raise WorkspaceBindingError("dirty claim scope is invalid")
        if any(
            not path
            or "\\" in path
            or "\0" in path
            or PurePosixPath(path).is_absolute()
            or PureWindowsPath(path).drive
            or PurePosixPath(path).as_posix() != path
            or any(part in {"", ".", "..", ".git"} for part in path.split("/"))
            for path in paths
        ) or len(set(paths)) != len(paths):
            raise WorkspaceBindingError("dirty claim scope is invalid")
        if type(payload["plan_revision"]) is not int or payload["plan_revision"] < 1:
            raise WorkspaceBindingError("dirty claim plan revision is invalid")
        for name in (
            "plan_fingerprint",
            "review_fingerprint",
            "index_sha256",
            "worktree_sha256",
            "untracked_sha256",
        ):
            if (
                not isinstance(payload[name], str)
                or re.fullmatch(r"[0-9a-f]{64}", payload[name]) is None
            ):
                raise WorkspaceBindingError("dirty claim digest is invalid")
        if (
            not isinstance(payload["reservation_base_sha"], str)
            or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", payload["reservation_base_sha"])
            is None
            or not isinstance(payload["nonce"], str)
            or re.fullmatch(r"[0-9a-f]{32}", payload["nonce"]) is None
            or not isinstance(payload["branch"], str)
            or re.fullmatch(r"[1-9][0-9]*-auto-impl-direct-[0-9a-f]{32}", payload["branch"]) is None
            or payload["state"] not in ("armed", "consumed")
        ):
            raise WorkspaceBindingError("dirty claim identity is invalid")
        return cls(**{**payload, "allowed_paths": tuple(paths)})

    def content_snapshot(self) -> dict[str, str]:
        """Return the three exact content digests."""
        return {
            "index_sha256": self.index_sha256,
            "worktree_sha256": self.worktree_sha256,
            "untracked_sha256": self.untracked_sha256,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceBinding:
    """Immutable execution-directory binding validated at invocation time."""

    kind: WorkspaceKind
    cwd: Path
    reusable_root: Path | None = None
    repository: str | None = None
    ownership_key: str | None = None
    item_number: int | None = None
    lane: SourceLane | None = None
    revision: str | None = None
    generation: int = 0
    detached: bool = False
    schema_version: int = 1
    dirty_claim: DirtyDirectClaim | None = None

    @classmethod
    def source(
        cls,
        *,
        cwd: Path,
        reusable_root: Path,
        repository: str,
        ownership_key: str,
        item_number: int,
        lane: SourceLane,
        revision: str,
        generation: int,
        detached: bool,
    ) -> Self:
        """Create a source workspace binding."""
        return cls(
            kind=WorkspaceKind.SOURCE,
            cwd=cwd,
            reusable_root=reusable_root,
            repository=repository,
            ownership_key=ownership_key,
            item_number=item_number,
            lane=lane,
            revision=revision,
            generation=generation,
            detached=detached,
        )

    @classmethod
    def session_only(cls, cwd: Path) -> Self:
        """Create a binding that may hold transcripts but not repository source."""
        return cls(kind=WorkspaceKind.SESSION_ONLY, cwd=cwd)

    @classmethod
    def external(cls, cwd: Path) -> Self:
        """Create a binding for an explicitly external, non-repository directory."""
        return cls(kind=WorkspaceKind.EXTERNAL, cwd=cwd)

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON-compatible representation."""
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "cwd": str(self.cwd),
            "reusable_root": str(self.reusable_root) if self.reusable_root else None,
            "repository": self.repository,
            "ownership_key": self.ownership_key,
            "item_number": self.item_number,
            "lane": self.lane.value if self.lane else None,
            "revision": self.revision,
            "generation": self.generation,
            "detached": self.detached,
        }
        if self.schema_version == 2:
            payload["dirty_claim"] = self.dirty_claim.to_dict() if self.dirty_claim else None
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Self:
        """Parse a strict workspace binding representation."""
        expected = _FIELDS | {"dirty_claim"} if payload.get("schema_version") == 2 else _FIELDS
        unknown = set(payload) - expected
        missing = expected - set(payload)
        if unknown:
            raise WorkspaceBindingError(f"workspace binding has unknown fields: {sorted(unknown)}")
        if missing:
            raise WorkspaceBindingError(f"workspace binding is missing fields: {sorted(missing)}")
        try:
            kind = WorkspaceKind(payload["kind"])
            lane_raw = payload["lane"]
            lane = SourceLane(lane_raw) if lane_raw is not None else None
            reusable_raw = payload["reusable_root"]
            binding = cls(
                schema_version=int(payload["schema_version"]),
                kind=kind,
                cwd=Path(str(payload["cwd"])),
                reusable_root=Path(str(reusable_raw)) if reusable_raw is not None else None,
                repository=_optional_str(payload["repository"]),
                ownership_key=_optional_str(payload["ownership_key"]),
                item_number=_optional_int(payload["item_number"]),
                lane=lane,
                revision=_optional_str(payload["revision"]),
                generation=int(payload["generation"]),
                detached=bool(payload["detached"]),
                dirty_claim=(
                    DirtyDirectClaim.from_dict(payload["dirty_claim"])
                    if payload["schema_version"] == 2
                    else None
                ),
            )
        except (TypeError, ValueError) as exc:
            raise WorkspaceBindingError(f"invalid workspace binding: {exc}") from exc
        _validate_shape(binding)
        return binding


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("expected a string or null")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected an integer or null")
    return value


def _validate_shape(binding: WorkspaceBinding) -> None:
    if binding.schema_version not in (1, 2):
        raise WorkspaceBindingError("unsupported workspace binding schema")
    if binding.schema_version == 1 and binding.dirty_claim is not None:
        raise WorkspaceBindingError("version 1 binding cannot carry a dirty claim")
    if binding.schema_version == 2:
        claim = binding.dirty_claim
        if (
            claim is None
            or binding.kind is not WorkspaceKind.SOURCE
            or binding.lane is not SourceLane.IMPLEMENTATION
            or binding.detached
            or not claim.branch.startswith(f"{binding.item_number}-auto-impl-direct-")
            or (claim.state == "armed" and claim.reservation_base_sha != binding.revision)
        ):
            raise WorkspaceBindingError("dirty claim does not match the workspace")
        DirtyDirectClaim.from_dict(claim.to_dict())
    if binding.generation < 0:
        raise WorkspaceBindingError("workspace generation must be non-negative")
    if binding.kind is WorkspaceKind.SOURCE and any(
        value is None
        for value in (
            binding.reusable_root,
            binding.repository,
            binding.ownership_key,
            binding.item_number,
            binding.lane,
            binding.revision,
        )
    ):
        raise WorkspaceBindingError("source workspace binding is incomplete")


def _run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env=build_git_child_env(),
    )


@dataclass(slots=True)
class _DirtyPermitRecord:
    """Revoke a permit in every copied context when its lease ends."""

    permit: object
    binding: WorkspaceBinding
    active: bool = True


_DIRTY_PERMITS: ContextVar[tuple[_DirtyPermitRecord, ...]] = ContextVar(
    "dirty_workspace_permits", default=()
)


@contextmanager
def _dirty_workspace_permit(binding: WorkspaceBinding) -> Iterator[object]:
    """Hold a process-local permit after the source manager consumes a claim."""
    permit = object()
    record = _DirtyPermitRecord(permit, binding)
    token = _DIRTY_PERMITS.set((*_DIRTY_PERMITS.get(), record))
    try:
        yield permit
    finally:
        record.active = False
        _DIRTY_PERMITS.reset(token)


def validate_workspace_binding(
    binding: WorkspaceBinding, *, allowed_tools: str = "", dirty_permit: object | None = None
) -> Path:
    """Validate a binding immediately before an agent invocation.

    Returns the canonical directory on success. No source-capable invocation
    may proceed from a session-only directory or the reusable checkout.
    """
    _validate_shape(binding)
    if binding.schema_version == 2 and not any(
        record.active and record.permit is dirty_permit and record.binding is binding
        for record in _DIRTY_PERMITS.get()
    ):
        raise WorkspaceBindingError("dirty workspace requires an active exact permit")
    lexical = binding.cwd.absolute()
    try:
        canonical = binding.cwd.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceBindingError(f"workspace does not exist: {binding.cwd}") from exc
    if lexical != canonical:
        raise WorkspaceBindingError(f"workspace path contains a symlink: {binding.cwd}")
    requested_tools = {part.strip() for part in allowed_tools.split(",") if part.strip()}
    if binding.kind is WorkspaceKind.SESSION_ONLY and requested_tools & _SOURCE_TOOLS:
        raise WorkspaceBindingError("session-only workspace cannot grant a source-reading tool")
    if binding.kind is not WorkspaceKind.SOURCE:
        return canonical

    reusable_root = binding.reusable_root
    item_number = binding.item_number
    lane = binding.lane
    revision = binding.revision
    if reusable_root is None or item_number is None or lane is None or revision is None:
        raise WorkspaceBindingError("source workspace binding is incomplete")
    if canonical == reusable_root.resolve(strict=True):
        raise WorkspaceBindingError("source-reading agent cannot use the reusable repository root")
    expected_name = source_worktree_name(item_number, lane.value)
    if canonical.name != expected_name:
        raise WorkspaceBindingError(
            f"source workspace path must end in {expected_name!r}, got {canonical.name!r}"
        )
    top = Path(_run_git(canonical, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if top != canonical:
        raise WorkspaceBindingError("workspace is not a registered worktree root")
    head = _run_git(canonical, "rev-parse", "HEAD").stdout.strip()
    if head != revision:
        raise WorkspaceBindingError(f"workspace revision changed: expected {revision}, got {head}")
    status = _run_git(canonical, "status", "--porcelain", "--untracked-files=all").stdout
    if status and binding.schema_version != 2:
        raise WorkspaceBindingError("source workspace is dirty")
    _validate_workspace_branch(binding, canonical)
    return canonical


def _validate_workspace_branch(binding: WorkspaceBinding, canonical: Path) -> None:
    """Check attached branch identity before a source invocation."""
    symbolic = _run_git(canonical, "symbolic-ref", "-q", "HEAD", check=False)
    if binding.dirty_claim is not None and symbolic.stdout.strip() != (
        f"refs/heads/{binding.dirty_claim.branch}"
    ):
        raise WorkspaceBindingError("dirty workspace branch changed")
    if binding.detached != (symbolic.returncode != 0):
        raise WorkspaceBindingError("workspace detached/branch state changed")
