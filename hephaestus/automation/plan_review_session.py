"""Durable conversation journal for iterative plan review.

The journal is provider-neutral.  It records the opaque provider session id
before model output is interpreted and keeps review/amendment artifacts in an
append-only, digest-checked transcript.  A missing or corrupt active record is
therefore a recovery error, never permission to start a replacement session.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from hephaestus.agents.model_selection import MODEL_REASONING_EFFORTS
from hephaestus.agents.pi_session import AgentSessionBinding, PiSessionBindingError
from hephaestus.io.utils import write_secure
from hephaestus.utils.file_lock import file_lock

SCHEMA_VERSION = 1
type ArtifactKind = Literal["review", "amendment"]


class PlanReviewSessionError(RuntimeError):
    """Base error for invalid plan-review journal operations."""


class PlanReviewSessionLostError(PlanReviewSessionError):
    """Report that an established review conversation cannot be resumed."""


@dataclass(frozen=True)
class PlanReviewArtifact:
    """One immutable entry in a plan-review transcript."""

    sequence: int
    kind: ArtifactKind
    round_index: int
    plan_revision: int
    plan_fingerprint: str
    path: str
    digest: str


@dataclass(frozen=True)
class PlanReviewSession:
    """Durable identity and latest state of one planning cycle."""

    schema_version: int
    repo: str
    issue: int
    cycle_id: str
    provider: str
    reviewer_model: str
    reviewer_config: dict[str, object]
    reviewer_config_fingerprint: str
    canonical_cwd: str
    session_key: str
    session_id: str | None
    session_binding: str | None
    round_index: int
    plan_revision: int
    plan_fingerprint: str
    state: str
    artifacts: tuple[PlanReviewArtifact, ...]
    created_at: str
    updated_at: str


class PlanReviewSessionStore:
    """Persist strict plan-review identities and append-only artifacts."""

    def __init__(self, state_dir_provider: Callable[[], Path]) -> None:
        """Use ``state_dir_provider`` as the plan-review journal root."""
        self._state_dir_provider = state_dir_provider

    @property
    def root(self) -> Path:
        """Return the current plan-review state directory."""
        return self._state_dir_provider() / "plan-review"

    @staticmethod
    def _identity_digest(repo: str, issue: int) -> str:
        return sha256(f"{repo}\0{issue}".encode()).hexdigest()

    def active_path(self, repo: str, issue: int) -> Path:
        """Return the active-cycle pointer path for an issue."""
        return self.root / f"active-{self._identity_digest(repo, issue)}.json"

    def record_path(self, cycle_id: str) -> Path:
        """Return the durable record path for a cycle."""
        return self.root / f"cycle-{cycle_id}.json"

    def artifact_path(self, cycle_id: str, sequence: int) -> Path:
        """Return the immutable transcript artifact path."""
        return self.root / "artifacts" / cycle_id / f"{sequence:04d}.txt"

    def start_cycle(
        self,
        *,
        repo: str,
        issue: int,
        provider: str,
        model: str,
        reviewer_config: Mapping[str, object],
        cwd: Path,
        plan_revision: int,
        plan_fingerprint: str,
        reset: bool = False,
    ) -> PlanReviewSession:
        """Create or recover the active cycle, optionally resetting it."""
        pointer = self.active_path(repo, issue)
        with file_lock(pointer.with_suffix(".lock"), require_exclusive=True):
            if reset:
                try:
                    current = self.recover_active(repo=repo, issue=issue)
                except PlanReviewSessionLostError:
                    current = None
            else:
                current = self.recover_active(repo=repo, issue=issue)
            if current is not None and not reset:
                return current
            if current is not None:
                self._update(current, state="reset")
            cycle_id = str(uuid4())
            now = datetime.now(UTC).isoformat()
            config = dict(reviewer_config)
            config_digest = sha256(
                json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            record = PlanReviewSession(
                schema_version=SCHEMA_VERSION,
                repo=repo,
                issue=issue,
                cycle_id=cycle_id,
                provider=provider,
                reviewer_model=model,
                reviewer_config=config,
                reviewer_config_fingerprint=config_digest,
                canonical_cwd=str(Path(cwd).resolve()),
                session_key=f"plan-reviewer-cycle-{cycle_id}",
                session_id=None,
                session_binding=None,
                round_index=0,
                plan_revision=plan_revision,
                plan_fingerprint=plan_fingerprint,
                state="starting",
                artifacts=(),
                created_at=now,
                updated_at=now,
            )
            self._write_record(record)
            write_secure(pointer, json.dumps({"cycle_id": cycle_id}, sort_keys=True) + "\n")
            return record

    def recover_active(self, *, repo: str, issue: int) -> PlanReviewSession | None:
        """Recover an active cycle; fail closed when its pointer is damaged."""
        pointer = self.active_path(repo, issue)
        try:
            raw = json.loads(pointer.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise PlanReviewSessionLostError("active review session pointer is unreadable") from exc
        cycle_id = raw.get("cycle_id") if isinstance(raw, dict) else None
        if not isinstance(cycle_id, str) or not cycle_id:
            raise PlanReviewSessionLostError("active review session pointer is invalid")
        try:
            record = self.load(cycle_id)
        except FileNotFoundError as exc:
            raise PlanReviewSessionLostError("active review session record is missing") from exc
        if record.repo != repo or record.issue != issue:
            raise PlanReviewSessionLostError("active review session identity does not match")
        if record.state in {"reset", "completed", "recovery-required"}:
            raise PlanReviewSessionLostError(
                f"active review session is not resumable ({record.state})"
            )
        return record

    def load(self, cycle_id: str) -> PlanReviewSession:
        """Load and validate one cycle record."""
        raw = json.loads(self.record_path(cycle_id).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            raise PlanReviewSessionLostError("review session record schema is invalid")
        if raw.get("cycle_id") != cycle_id:
            raise PlanReviewSessionLostError("review session cycle identity does not match")
        try:
            artifacts = tuple(PlanReviewArtifact(**entry) for entry in raw.pop("artifacts"))
            record = PlanReviewSession(artifacts=artifacts, **raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise PlanReviewSessionLostError("review session record is invalid") from exc
        self._validate_identity(record)
        self._validate_provider_state(record)
        self._validate_artifacts(record)
        if tuple(a.sequence for a in artifacts) != tuple(range(len(artifacts))):
            raise PlanReviewSessionLostError("review session artifact sequence is invalid")
        return record

    @staticmethod
    def _validate_identity(record: PlanReviewSession) -> None:
        """Reject malformed cycle identity and progress metadata."""
        required_text = (
            record.repo,
            record.cycle_id,
            record.provider,
            record.reviewer_config_fingerprint,
            record.canonical_cwd,
            record.session_key,
            record.plan_fingerprint,
            record.created_at,
            record.updated_at,
        )
        if not all(isinstance(value, str) and value for value in required_text):
            raise PlanReviewSessionLostError("review session record text fields are invalid")
        if not isinstance(record.reviewer_model, str):
            raise PlanReviewSessionLostError("reviewer model is invalid")
        if not isinstance(record.issue, int) or isinstance(record.issue, bool):
            raise PlanReviewSessionLostError("review session issue identity is invalid")
        if record.state not in {"starting", "active", "reset", "completed", "recovery-required"}:
            raise PlanReviewSessionLostError("review session state is invalid")
        if (
            not isinstance(record.round_index, int)
            or record.round_index < 0
            or not isinstance(record.plan_revision, int)
            or record.plan_revision < 1
        ):
            raise PlanReviewSessionLostError("review session round or revision is invalid")

    @staticmethod
    def _validate_provider_state(record: PlanReviewSession) -> None:
        """Reject malformed reviewer configuration and provider binding."""
        if record.session_id is not None and (
            not isinstance(record.session_id, str) or not record.session_id
        ):
            raise PlanReviewSessionLostError("reviewer session id is invalid")
        if not isinstance(record.reviewer_config, dict):
            raise PlanReviewSessionLostError("reviewer configuration is invalid")
        selection_format = record.reviewer_config.get("model_selection_format")
        if selection_format is not None and (
            not isinstance(selection_format, int)
            or isinstance(selection_format, bool)
            or selection_format != 1
        ):
            raise PlanReviewSessionLostError("reviewer model selection format is invalid")
        if selection_format == 1:
            reasoning_effort = record.reviewer_config.get("reasoning_effort")
            if not isinstance(
                reasoning_effort, str
            ) or reasoning_effort not in MODEL_REASONING_EFFORTS | {""}:
                raise PlanReviewSessionLostError("reviewer reasoning effort is invalid")
        config_digest = sha256(
            json.dumps(
                record.reviewer_config,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if config_digest != record.reviewer_config_fingerprint:
            raise PlanReviewSessionLostError("reviewer configuration digest mismatch")
        if record.session_binding is not None:
            if not isinstance(record.session_binding, str):
                raise PlanReviewSessionLostError("reviewer session binding is invalid")
            try:
                binding = AgentSessionBinding.from_json(record.session_binding)
            except PiSessionBindingError as exc:
                raise PlanReviewSessionLostError("reviewer session binding is invalid") from exc
            if binding.session_id != record.session_id:
                raise PlanReviewSessionLostError("reviewer session binding identity mismatch")

    def _validate_artifacts(self, record: PlanReviewSession) -> None:
        """Reject malformed or non-canonical transcript references."""
        for artifact in record.artifacts:
            typed_values = (
                isinstance(artifact.sequence, int),
                not isinstance(artifact.sequence, bool),
                isinstance(artifact.round_index, int),
                isinstance(artifact.plan_revision, int),
                isinstance(artifact.plan_fingerprint, str),
                isinstance(artifact.path, str),
                isinstance(artifact.digest, str),
            )
            if not all(typed_values):
                raise PlanReviewSessionLostError("review session artifact metadata is invalid")
            bounded_values = (
                artifact.sequence >= 0,
                artifact.kind in {"review", "amendment"},
                artifact.round_index >= 0,
                artifact.plan_revision >= 1,
                bool(artifact.plan_fingerprint),
                len(artifact.digest) == 64,
            )
            if not all(bounded_values):
                raise PlanReviewSessionLostError("review session artifact metadata is invalid")
            expected = str(
                self.artifact_path(record.cycle_id, artifact.sequence).relative_to(self.root)
            )
            if artifact.path != expected:
                raise PlanReviewSessionLostError("review session artifact path is invalid")

    def bind_session(
        self,
        cycle_id: str,
        session_id: str,
        session_binding: AgentSessionBinding | None = None,
    ) -> PlanReviewSession:
        """Checkpoint the provider identity before parsing reviewer output."""
        if not session_id:
            raise ValueError("reviewer session id must be non-empty")
        with file_lock(self.record_path(cycle_id).with_suffix(".lock"), require_exclusive=True):
            record = self.load(cycle_id)
            if record.session_id not in {None, session_id}:
                raise PlanReviewSessionLostError("provider returned a different reviewer session")
            binding_json = session_binding.to_json() if session_binding is not None else None
            if record.session_binding not in {None, binding_json}:
                raise PlanReviewSessionLostError("provider returned a different reviewer binding")
            return self._update(
                record,
                session_id=session_id,
                session_binding=binding_json or record.session_binding,
                state="active",
            )

    def append_artifact(
        self,
        cycle_id: str,
        *,
        kind: ArtifactKind,
        content: str,
        round_index: int,
        plan_revision: int,
        plan_fingerprint: str,
    ) -> PlanReviewSession:
        """Append one digest-checked transcript entry idempotently."""
        record_path = self.record_path(cycle_id)
        with file_lock(record_path.with_suffix(".lock"), require_exclusive=True):
            record = self.load(cycle_id)
            digest = sha256(content.encode("utf-8")).hexdigest()
            if record.artifacts:
                latest = record.artifacts[-1]
                if (
                    latest.kind == kind
                    and latest.round_index == round_index
                    and latest.plan_revision == plan_revision
                    and latest.plan_fingerprint == plan_fingerprint
                    and latest.digest == digest
                ):
                    return record
            sequence = len(record.artifacts)
            path = self.artifact_path(cycle_id, sequence)
            write_secure(path, content)
            artifact = PlanReviewArtifact(
                sequence=sequence,
                kind=kind,
                round_index=round_index,
                plan_revision=plan_revision,
                plan_fingerprint=plan_fingerprint,
                path=str(path.relative_to(self.root)),
                digest=digest,
            )
            return self._update(
                record,
                artifacts=(*record.artifacts, artifact),
                round_index=round_index,
                plan_revision=plan_revision,
                plan_fingerprint=plan_fingerprint,
            )

    def transcript(self, cycle_id: str) -> str:
        """Return the complete verified transcript for a cycle."""
        record = self.load(cycle_id)
        sections: list[str] = []
        for artifact in record.artifacts:
            path = self.root / artifact.path
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise PlanReviewSessionLostError("review transcript artifact is missing") from exc
            if sha256(content.encode("utf-8")).hexdigest() != artifact.digest:
                raise PlanReviewSessionLostError("review transcript artifact digest mismatch")
            sections.append(
                f"## {artifact.kind} round={artifact.round_index} "
                f"revision={artifact.plan_revision} fingerprint={artifact.plan_fingerprint}\n"
                f"{content}"
            )
        return "\n\n".join(sections)

    def mark_recovery_required(self, cycle_id: str) -> PlanReviewSession:
        """Make provider session loss durable and non-resumable."""
        return self._update(self.load(cycle_id), state="recovery-required")

    def complete(self, cycle_id: str) -> PlanReviewSession:
        """Mark a cycle complete and remove its active pointer."""
        record = self._update(self.load(cycle_id), state="completed")
        pointer = self.active_path(record.repo, record.issue)
        with suppress(FileNotFoundError):
            pointer.unlink()
        return record

    def _update(self, record: PlanReviewSession, **changes: Any) -> PlanReviewSession:
        raw = asdict(record)
        raw.update(changes)
        raw["updated_at"] = datetime.now(UTC).isoformat()
        artifacts = tuple(
            entry if isinstance(entry, PlanReviewArtifact) else PlanReviewArtifact(**entry)
            for entry in raw.pop("artifacts")
        )
        updated = PlanReviewSession(artifacts=artifacts, **raw)
        self._write_record(updated)
        return updated

    def _write_record(self, record: PlanReviewSession) -> None:
        write_secure(
            self.record_path(record.cycle_id),
            json.dumps(asdict(record), indent=2, sort_keys=True) + "\n",
        )
