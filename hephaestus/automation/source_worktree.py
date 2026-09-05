"""Deterministic, revision-bound worktrees for source-reading agents."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from hephaestus.agents.workspace import (
    SourceLane,
    WorkspaceBinding,
    WorkspaceBindingError,
    validate_workspace_binding,
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
from hephaestus.config.child_environments import build_git_signing_env
from hephaestus.io.utils import write_secure
from hephaestus.utils.file_lock import file_lock
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

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible receipt."""
        return {
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
            )
        except (TypeError, ValueError) as exc:
            raise SourceWorkspaceError(f"invalid source workspace receipt: {exc}") from exc
        if receipt.schema_version != 1 or receipt.generation < 1:
            raise SourceWorkspaceError("unsupported source workspace receipt")
        return receipt


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env=build_git_signing_env(),
    )


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
        common_raw = _git(self.repo_root, "rev-parse", "--git-common-dir").stdout.strip()
        common = Path(common_raw)
        if not common.is_absolute():
            common = self.repo_root / common
        self.common_dir = common.resolve(strict=True)
        digest = hashlib.sha256(str(self.common_dir).encode()).hexdigest()[:16]
        self.repository_identity = f"{repository}:{digest}"
        self.base_dir = (base_dir or self.repo_root / "build" / ".worktrees").resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.common_dir / "hephaestus-source-workspaces"

    def path_for(self, item_number: int, lane: SourceLane) -> Path:
        """Return the deterministic physical path for a lane."""
        return self.base_dir / source_worktree_name(item_number, lane.value)

    def ownership_key(self, item_number: int, lane: SourceLane) -> str:
        """Return the repository-qualified internal ownership key."""
        return f"{self.repository_identity}:{item_number}:{lane.value}"

    @contextmanager
    def implementation_writer_handoff(
        self, item_number: int
    ) -> Iterator[ImplementationWriterHandoff]:
        """Hold the implementation lane for one complete writer handoff."""
        if isinstance(item_number, bool) or not isinstance(item_number, int):
            raise SourceWorkspaceError("implementation writer handoff item number is invalid")
        with implementation_writer_handoff(
            self.repo_root,
            item_number,
            self._lane_lock_path(item_number, SourceLane.IMPLEMENTATION),
        ) as handoff:
            yield handoff

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
        target = _git(self.repo_root, "rev-parse", f"{revision}^{{commit}}").stdout.strip()
        path = self.path_for(item_number, lane)
        with file_lock(self._lane_lock_path(item_number, lane), require_exclusive=True):
            old = self._read_receipt(item_number, lane)
            self._reject_foreign_owner(old, item_number, lane)
            if path.exists() and self._is_dirty(path):
                raise SourceWorkspaceError(f"source workspace is dirty and preserved: {path}")
            desired_detached = lane is SourceLane.REVIEW or branch is None
            physical_revision = self._head_revision(path) if path.exists() else None
            physical_branch = self._head_branch(path) if path.exists() else None
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
                old.generation
                if can_reuse and old is not None
                else old.generation + 1
                if old
                else 1
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
                )
            verified_revision = self._head_revision(path)
            verified_branch = self._head_branch(path)
            verified_checkout_matches = (
                verified_branch is None
                if desired_detached
                else verified_branch == f"refs/heads/{branch}"
            )
            if verified_revision != target:
                raise SourceWorkspaceError(
                    "source workspace could not reach the requested revision"
                )
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
                validate_workspace_binding(binding)
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
        expected_path = self.path_for(item_number, lane).resolve()
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
        if path.resolve() != expected_path:
            raise SourceWorkspaceError(
                "implementation writer path does not match the deterministic lane"
            )
        old = self._read_receipt(item_number, lane)
        self._reject_foreign_owner(old, item_number, lane)
        if old is not None and old.path.resolve() != expected_path:
            raise SourceWorkspaceError("incompatible source workspace receipt")
        if old is not None and not old.detached and old.branch != branch and authority is None:
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
        if old is not None and old.detached:
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
        return binding

    def authorize_direct_implementation_writer_transition(  # noqa: C901
        self,
        item_number: int,
        *,
        branch: str,
        base_sha: str,
        handoff: ImplementationWriterHandoff | None,
    ) -> None:
        """Arm one exact predecessor transition for a direct writer."""
        lane = SourceLane.IMPLEMENTATION
        expected_path = self.path_for(item_number, lane).resolve()
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
        old = self._read_receipt(item_number, lane)
        self._reject_foreign_owner(old, item_number, lane)
        target = _git(self.repo_root, "rev-parse", f"{base_sha}^{{commit}}").stdout.strip()
        if target != base_sha:
            raise SourceWorkspaceError("direct implementation writer base is invalid")
        receipt_path = self._receipt_path(item_number, lane)
        if old is None:
            if expected_path.exists():
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
            return
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
        if self._is_dirty(expected_path):
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
        physical_revision = self._head_revision(expected_path)
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
        physical_branch = self._head_branch(expected_path)
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
        if not old.detached and old.branch == branch:
            raise SourceWorkspaceError(
                "implementation writer predecessor is not stale",
                recovery=self._unproven_recovery(
                    item_number=item_number,
                    path=expected_path,
                    receipt_path=receipt_path,
                ),
            )
        try:
            handoff._arm_direct_transition(
                path=expected_path,
                predecessor_generation=old.generation,
                predecessor_revision=physical_revision,
                predecessor_branch=physical_branch,
                branch=branch,
                base_sha=base_sha,
            )
        except RuntimeError as exc:
            raise SourceWorkspaceError(str(exc)) from exc

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
            receipt_path=receipt_path.resolve(),
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

    @contextmanager
    def acquire(self, binding: WorkspaceBinding, *, allowed_tools: str = "") -> Iterator[Path]:
        """Hold the lane lease while validating and using a source workspace."""
        if binding.item_number is None or binding.lane is None:
            raise SourceWorkspaceError("source workspace binding is incomplete")
        with file_lock(
            self._lane_lock_path(binding.item_number, binding.lane),
            require_exclusive=True,
        ):
            receipt = self._read_receipt(binding.item_number, binding.lane)
            if receipt is None or self._binding(receipt) != binding:
                raise SourceWorkspaceError("source workspace receipt no longer matches binding")
            try:
                yield validate_workspace_binding(binding, allowed_tools=allowed_tools)
            except WorkspaceBindingError as exc:
                raise SourceWorkspaceError(str(exc)) from exc

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
    ) -> None:
        """Remove one clean terminal lane and its receipt under the lane lock."""
        with file_lock(self._lane_lock_path(item_number, lane), require_exclusive=True):
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
            if receipt.path.exists() and self._is_dirty(receipt.path):
                raise SourceWorkspaceError(
                    f"source workspace is dirty and preserved: {receipt.path}"
                )
            if physical_cleanup is not None:
                physical_cleanup()
            else:
                with file_lock(WorktreeManager.git_metadata_lock_path(self.repo_root)):
                    result = _git(
                        self.repo_root,
                        "worktree",
                        "remove",
                        str(receipt.path),
                        check=False,
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
        with file_lock(WorktreeManager.git_metadata_lock_path(self.repo_root)):
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
    ) -> None:
        with file_lock(WorktreeManager.git_metadata_lock_path(self.repo_root)):
            exists = branch is not None and (
                _git(
                    self.repo_root,
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{branch}",
                    check=False,
                ).returncode
                == 0
            )
            if exists and not owns_branch:
                raise SourceWorkspaceError("source workspace branch is not owned by this lane")
            if path.exists():
                removed = _git(self.repo_root, "worktree", "remove", str(path), check=False)
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
            added = _git(self.repo_root, *args, check=False)
            if added.returncode:
                if branch is not None and exists:
                    raise SourceWorkspaceError(
                        "source workspace branch could not be synchronized safely"
                    )
                raise SourceWorkspaceError(added.stderr.strip() or "worktree creation failed")

    @staticmethod
    def _is_dirty(path: Path) -> bool:
        result = _git(path, "status", "--porcelain", "--untracked-files=all", check=False)
        if result.returncode:
            raise SourceWorkspaceError(f"cannot inspect source workspace: {path}")
        return bool(result.stdout)

    @staticmethod
    def _head_revision(path: Path) -> str:
        """Return the physical worktree HEAD or fail before any replacement."""
        result = _git(path, "rev-parse", "HEAD", check=False)
        if result.returncode:
            raise SourceWorkspaceError(f"cannot inspect source workspace revision: {path}")
        return result.stdout.strip()

    @staticmethod
    def _head_branch(path: Path) -> str | None:
        """Return the physical local branch ref, or ``None`` for detached HEAD."""
        result = _git(path, "symbolic-ref", "-q", "HEAD", check=False)
        if result.returncode == 1:
            return None
        branch = result.stdout.strip()
        if result.returncode or not branch.startswith("refs/heads/"):
            raise SourceWorkspaceError(f"cannot inspect source workspace branch: {path}")
        return branch

    def _binding(self, receipt: SourceWorkspaceReceipt) -> WorkspaceBinding:
        return WorkspaceBinding.source(
            cwd=receipt.path,
            reusable_root=self.repo_root,
            repository=receipt.repository,
            ownership_key=receipt.ownership_key,
            item_number=receipt.item_number,
            lane=receipt.lane,
            revision=receipt.revision,
            generation=receipt.generation,
            detached=receipt.detached,
        )

    def _lane_lock_path(self, item_number: int, lane: SourceLane) -> Path:
        return WorktreeManager.source_lane_lock_path(self.repo_root, item_number, lane.value)

    def _receipt_path(self, item_number: int, lane: SourceLane) -> Path:
        return self.state_dir / f"{item_number}-{lane.value}.json"

    def _read_receipt(self, item_number: int, lane: SourceLane) -> SourceWorkspaceReceipt | None:
        path = self._receipt_path(item_number, lane)
        if not path.exists():
            return None
        if path.is_symlink():
            raise SourceWorkspaceError(f"refusing symlinked source receipt: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
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
        write_secure(
            path,
            json.dumps(receipt.to_dict(), sort_keys=True, indent=2) + "\n",
        )

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
