"""Deterministic, revision-bound worktrees for source-reading agents."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Self

from hephaestus.agents.workspace import (
    SourceLane,
    WorkspaceBinding,
    WorkspaceBindingError,
    validate_workspace_binding,
)
from hephaestus.automation.worktree_manager import WorktreeManager
from hephaestus.config.child_environments import build_git_signing_env
from hephaestus.io.utils import write_secure
from hephaestus.utils.file_lock import file_lock
from hephaestus.utils.worktree_identity import source_worktree_name


class SourceWorkspaceError(RuntimeError):
    """Raised when a source lane cannot be prepared safely."""


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
                self._replace_worktree(path, target, branch=None if desired_detached else branch)
            receipt = SourceWorkspaceReceipt(
                repository=self.repository,
                repository_identity=self.repository_identity,
                ownership_key=self.ownership_key(item_number, lane),
                item_number=item_number,
                lane=lane,
                path=path.resolve(),
                revision=target,
                generation=generation,
                detached=desired_detached,
                branch=None if desired_detached else branch,
                obligations=old.obligations if old is not None else (),
            )
            self._write_receipt(receipt)
            binding = self._binding(receipt)
            try:
                validate_workspace_binding(binding)
            except WorkspaceBindingError as exc:
                raise SourceWorkspaceError(str(exc)) from exc
            return binding

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

    def cleanup(self, item_number: int, lane: SourceLane) -> None:
        """Remove a clean terminal lane; preserve dirty or obligated state."""
        with file_lock(self._lane_lock_path(item_number, lane), require_exclusive=True):
            receipt = self._require_receipt(item_number, lane)
            self._reject_foreign_owner(receipt, item_number, lane)
            if receipt.obligations:
                raise SourceWorkspaceError("source workspace still has active obligations")
            if receipt.path.exists() and self._is_dirty(receipt.path):
                raise SourceWorkspaceError(
                    f"source workspace is dirty and preserved: {receipt.path}"
                )
            with file_lock(WorktreeManager.git_metadata_lock_path(self.repo_root)):
                result = _git(
                    self.repo_root,
                    "worktree",
                    "remove",
                    str(receipt.path),
                    check=False,
                )
                if result.returncode and receipt.path.exists():
                    raise SourceWorkspaceError(result.stderr.strip() or "worktree cleanup failed")
            self._receipt_path(item_number, lane).unlink(missing_ok=True)

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

    def _replace_worktree(self, path: Path, revision: str, branch: str | None) -> None:
        with file_lock(WorktreeManager.git_metadata_lock_path(self.repo_root)):
            if path.exists():
                removed = _git(self.repo_root, "worktree", "remove", str(path), check=False)
                if removed.returncode:
                    raise SourceWorkspaceError(removed.stderr.strip() or "worktree removal failed")
            args = ["worktree", "add"]
            if branch is None:
                args.extend(["--detach", str(path), revision])
            else:
                exists = (
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
                if exists:
                    args.extend([str(path), branch])
                else:
                    args.extend(["-b", branch, str(path), revision])
            added = _git(self.repo_root, *args, check=False)
            if added.returncode:
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
        return self.state_dir / f"{item_number}-{lane.value}.lock"

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
        if receipt is not None and receipt.ownership_key != self.ownership_key(item_number, lane):
            raise SourceWorkspaceError(
                f"source workspace is owned by another repository: {receipt.ownership_key}"
            )
