"""Typed Athena skill jobs for host-owned Mnemosyne execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

AthenaSkillKind = Literal["advise", "learn"]


@dataclass(frozen=True)
class AthenaSkillRequest:
    """Closed request schema for Athena-equivalent skill execution."""

    kind: AthenaSkillKind | str
    repo: str
    issue: int | str
    agent: str
    model: str
    cwd: Path
    timeout_s: int
    payload: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False


@dataclass(frozen=True)
class AthenaSkillResult:
    """Closed result schema returned by host-owned Athena execution."""

    kind: str
    context: str = ""
    receipt: dict[str, Any] = field(default_factory=dict)
    delivery_receipt: dict[str, Any] | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Return True when the result has no failure envelope."""
        return self.error is None


class AthenaSkillExecutor(Protocol):
    """Worker-facing executor for typed Athena skill requests."""

    def execute(self, request: AthenaSkillRequest) -> AthenaSkillResult:
        """Execute a typed Athena skill request."""


def build_athena_skill_request(
    *,
    kind: str,
    repo: str,
    issue: int | str,
    agent: str,
    model: str,
    cwd: Path,
    timeout_s: int,
    payload: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> AthenaSkillRequest:
    """Build the provider-neutral request shape used by direct and pipeline paths."""
    return AthenaSkillRequest(
        kind=kind,
        repo=repo,
        issue=issue,
        agent=agent,
        model=model,
        cwd=cwd,
        timeout_s=timeout_s,
        payload=dict(payload or {}),
        dry_run=dry_run,
    )


@dataclass(frozen=True)
class AthenaSkillJob:
    """Worker job that runs advise/learn through host-owned contracts."""

    request: AthenaSkillRequest
    descr: str = ""

    @property
    def repo(self) -> str:
        """Repository used by worker claim logging."""
        return self.request.repo

    @property
    def issue(self) -> int | str:
        """Issue identifier used by worker claim logging."""
        return self.request.issue

    @property
    def timeout_s(self) -> int:
        """Timeout carried by the underlying request."""
        return self.request.timeout_s
