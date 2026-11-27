"""Session-id-first learn requests for an existing harness session (issue #3518).

A bare session id supplies no repository, issue, or workspace. This module
expresses that request and states the source-evidence decision explicitly.
It does not create a delivery receipt and does not weaken the queue-owned
evidence rules in :mod:`hephaestus.automation.mnemosyne_skill_host`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from hephaestus.agents.workspace import WorkspaceBinding, WorkspaceKind

SOURCE_EVIDENCE_REASON = "learning_deferred:source_evidence_required"
SOURCE_EVIDENCE_RECOVERY = (
    "Run learning through the automation queue, where merged-PR and check "
    "evidence bind the source. A CLI session id alone cannot bind source evidence."
)


@dataclass(frozen=True, slots=True)
class LearnSessionRequest:
    """One learn request that starts from an opaque harness session id."""

    session_id: str
    agent: str
    cwd: Path
    prompt: str
    model: str = ""
    timeout: int | None = None
    repo: str | None = None
    issue: int | str | None = None
    workspace: WorkspaceBinding | None = None

    @property
    def source_evidence(self) -> tuple[str, int | str, WorkspaceBinding] | None:
        """Return bound source evidence, or None when the request has none."""
        if not self.repo or self.issue is None or self.issue == "" or self.workspace is None:
            return None
        return (self.repo, self.issue, self.workspace)


class LearningEvidenceError(RuntimeError):
    """Source evidence cannot be bound for a session-id-first learn request."""

    failure_kind = SOURCE_EVIDENCE_REASON
    recovery = SOURCE_EVIDENCE_RECOVERY

    def __init__(self, detail: str) -> None:
        """Keep the failure kind and a specific diagnostic reason."""
        super().__init__(f"{SOURCE_EVIDENCE_REASON}: {detail}")


@dataclass(frozen=True, slots=True)
class LearnDeliveryState:
    """The explicit delivery decision for one session learn turn."""

    state: Literal["bound", "deferred"]
    reason: str | None = None
    recovery: str | None = None


def require_source_evidence(request: LearnSessionRequest) -> None:
    """Fail closed when the request has no bound source evidence."""
    evidence = request.source_evidence
    if evidence is None:
        raise LearningEvidenceError("a session id supplies no repository, issue, or workspace")
    repo, issue, workspace = evidence
    if workspace.kind is not WorkspaceKind.SOURCE:
        raise LearningEvidenceError("the workspace binding is not a source binding")
    if workspace.cwd != request.cwd:
        raise LearningEvidenceError("the workspace cwd does not match the request cwd")
    if workspace.repository != repo:
        raise LearningEvidenceError("the workspace repository does not match the request repo")
    if workspace.item_number != issue and str(workspace.item_number) != str(issue):
        raise LearningEvidenceError("the workspace item does not match the request issue")


def session_learn_delivery(request: LearnSessionRequest) -> LearnDeliveryState:
    """Return the delivery state for one session-id-first learn request."""
    try:
        require_source_evidence(request)
    except LearningEvidenceError as exc:
        return LearnDeliveryState("deferred", exc.failure_kind, exc.recovery)
    return LearnDeliveryState("bound")
