"""Provider-neutral workspace contracts for agent execution.

An agent receives a typed workspace binding rather than an ambient directory.
Source bindings are fail-closed: they identify one deterministic item lane and
the exact revision and generation that must still be present when execution
starts.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
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
        return {
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

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Self:
        """Parse a strict workspace binding representation."""
        unknown = set(payload) - _FIELDS
        missing = _FIELDS - set(payload)
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
    if binding.schema_version != 1:
        raise WorkspaceBindingError("unsupported workspace binding schema")
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


def validate_workspace_binding(binding: WorkspaceBinding, *, allowed_tools: str = "") -> Path:
    """Validate a binding immediately before an agent invocation.

    Returns the canonical directory on success. No source-capable invocation
    may proceed from a session-only directory or the reusable checkout.
    """
    _validate_shape(binding)
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
    if status:
        raise WorkspaceBindingError("source workspace is dirty")
    symbolic = _run_git(canonical, "symbolic-ref", "-q", "HEAD", check=False)
    if binding.detached != (symbolic.returncode != 0):
        raise WorkspaceBindingError("workspace detached/branch state changed")
    return canonical
