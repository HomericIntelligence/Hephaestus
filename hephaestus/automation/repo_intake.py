"""Prepare the isolated repository-intake control plane.

The caller checkout supplies repository identity and the Git common directory.
It is never changed by this module.  The intake checkout is a detached
worktree with a durable ownership receipt and a path outside the caller
checkout, so GitHub reads and later source-worktree creation use a clean,
revision-pinned control plane.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self, TypeGuard

from hephaestus.automation.worktree_manager import WorktreeManager
from hephaestus.io.utils import write_secure
from hephaestus.utils.file_lock import file_lock


class RepoIntakeError(RuntimeError):
    """Raised when an intake checkout cannot be verified safely."""


def is_full_commit_sha(value: object) -> TypeGuard[str]:
    """Return whether *value* is a lower-case full SHA-1 or SHA-256."""
    return bool(
        isinstance(value, str)
        and len(value) in (40, 64)
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True, slots=True)
class RepoIntakeReceipt:
    """Durable ownership and revision proof for one intake checkout."""

    repository: str
    repository_identity: str
    ownership_key: str
    common_dir: Path
    path: Path
    default_branch: str
    revision: str
    generation: int
    detached: bool = True
    branch: str | None = None
    schema_version: int = 1

    def to_dict(self) -> dict[str, object]:
        """Return the receipt in its closed JSON representation."""
        return {
            "schema_version": self.schema_version,
            "repository": self.repository,
            "repository_identity": self.repository_identity,
            "ownership_key": self.ownership_key,
            "common_dir": str(self.common_dir),
            "path": str(self.path),
            "default_branch": self.default_branch,
            "revision": self.revision,
            "generation": self.generation,
            "detached": self.detached,
            "branch": self.branch,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Self:
        """Parse and validate a receipt without accepting unknown fields."""
        fields = {
            "schema_version",
            "repository",
            "repository_identity",
            "ownership_key",
            "common_dir",
            "path",
            "default_branch",
            "revision",
            "generation",
            "detached",
            "branch",
        }
        if set(payload) != fields:
            raise RepoIntakeError("repository-intake receipt schema mismatch")
        if not isinstance(payload["schema_version"], int) or isinstance(
            payload["schema_version"], bool
        ):
            raise RepoIntakeError("repository-intake receipt schema version is invalid")
        if not isinstance(payload["generation"], int) or isinstance(payload["generation"], bool):
            raise RepoIntakeError("repository-intake receipt generation is invalid")
        if not isinstance(payload["detached"], bool):
            raise RepoIntakeError("repository-intake receipt detached flag is invalid")
        string_fields = (
            "repository",
            "repository_identity",
            "ownership_key",
            "common_dir",
            "path",
            "default_branch",
            "revision",
        )
        if any(not isinstance(payload[name], str) or not payload[name] for name in string_fields):
            raise RepoIntakeError("repository-intake receipt contains an invalid value")
        branch = payload["branch"]
        if branch is not None and (not isinstance(branch, str) or not branch):
            raise RepoIntakeError("repository-intake receipt branch is invalid")
        try:
            receipt = cls(
                schema_version=payload["schema_version"],
                repository=payload["repository"],
                repository_identity=payload["repository_identity"],
                ownership_key=payload["ownership_key"],
                common_dir=Path(payload["common_dir"]),
                path=Path(payload["path"]),
                default_branch=payload["default_branch"],
                revision=payload["revision"],
                generation=payload["generation"],
                detached=payload["detached"],
                branch=branch,
            )
        except (TypeError, ValueError) as exc:
            raise RepoIntakeError(f"invalid repository-intake receipt: {exc}") from exc
        if (
            receipt.schema_version != 1
            or receipt.generation < 1
            or not receipt.common_dir.is_absolute()
            or not receipt.path.is_absolute()
            or not receipt.detached
            or receipt.branch is not None
            or not is_full_commit_sha(receipt.revision)
            or not _is_valid_branch(receipt.default_branch)
        ):
            raise RepoIntakeError("repository-intake receipt values are unsafe")
        for candidate in (receipt.common_dir, receipt.path):
            if candidate.is_symlink():
                raise RepoIntakeError("repository-intake receipt contains a symlinked path")
        return receipt


_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_KNOWN_WORKTREE_LINES = ("locked", "prunable")


def _is_valid_branch(value: object) -> TypeGuard[str]:
    """Return whether a branch name is safe as a Git ref component."""
    return bool(
        isinstance(value, str)
        and bool(_BRANCH_RE.fullmatch(value))
        and ".." not in value
        and "@{" not in value
        and not value.endswith("/")
        and not value.endswith(".")
        and not value.endswith(".lock")
        and "//" not in value
        and all(
            component not in {".", ".."} and not component.endswith(".lock")
            for component in value.split("/")
        )
    )


@dataclass(frozen=True, slots=True)
class _WorktreeRecord:
    """Small parsed representation of one registered Git worktree."""

    path: Path
    head: str
    branch: str | None


class RepoIntakeManager:
    """Create, validate, reuse, and safely rebind one intake worktree."""

    def __init__(
        self,
        caller_root: Path,
        *,
        repository: str,
        gh_command: str,
        timeout_s: int,
        git_runner: Callable[..., subprocess.CompletedProcess[str]],
        git_env: dict[str, str],
        remote_config: tuple[str, ...],
    ) -> None:
        """Initialize an intake manager for a caller-selected checkout."""
        if not repository or "/" not in repository:
            raise RepoIntakeError("repository identity is invalid")
        if timeout_s <= 0:
            raise RepoIntakeError("repository-intake timeout is invalid")
        if caller_root.is_symlink():
            raise RepoIntakeError(f"caller checkout is symlinked: {caller_root}")
        try:
            self.caller_root = caller_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RepoIntakeError(f"caller checkout cannot be resolved: {caller_root}") from exc
        if not self.caller_root.is_dir():
            raise RepoIntakeError(f"caller checkout does not exist: {self.caller_root}")
        self.repository = repository
        self.gh_command = gh_command
        self.timeout_s = timeout_s
        self._run_command = git_runner
        self._git_env = dict(git_env)
        self._remote_config = tuple(remote_config)
        self.common_dir = self._resolve_common_dir()
        digest = hashlib.sha256(str(self.common_dir).encode()).hexdigest()[:16]
        self.repository_identity = f"{repository}:{digest}"
        # Keep both the receipt and worktree outside the caller root.  The
        # common directory is shared by linked worktrees, so this path remains
        # stable when the caller is detached or linked elsewhere.
        state_parent = self.common_dir.parent.parent / ".hephaestus-repo-intake"
        self.state_parent = state_parent
        # Keep this path lexical.  Resolving it here would follow an attacker-
        # supplied symlink before validation and make the ownership check
        # inspect a different path than the one selected by the manager.
        self.state_dir = state_parent / digest
        self.worktree_path = self.state_dir / "worktree"
        self.receipt_path = self.state_dir / "receipt.json"
        self.ownership_key = f"{self.repository_identity}:intake"

    def prepare(self) -> RepoIntakeReceipt:
        """Return a verified intake receipt, creating or rebinding as needed."""
        try:
            metadata_lock = WorktreeManager.git_metadata_lock_path(self.caller_root)
            lock_context = file_lock(metadata_lock, require_exclusive=True)
        except (OSError, RuntimeError) as exc:
            raise RepoIntakeError("Git metadata lock is unavailable") from exc
        try:
            with lock_context:
                return self._prepare_locked()
        except RepoIntakeError:
            raise
        except (OSError, RuntimeError) as exc:
            raise RepoIntakeError("repository-intake preparation failed safely") from exc

    def _prepare_locked(self) -> RepoIntakeReceipt:
        """Prepare the intake worktree while the common metadata lock is held."""
        self._validate_origin()
        self._validate_state_paths()
        old = self._read_receipt()
        records = self._worktree_records()
        record = self._validate_existing(old, records)
        default_branch = self._read_default_branch()
        caller_head = self._head(self.caller_root)
        if old is None:
            if record is not None:
                raise RepoIntakeError("unowned repository-intake worktree is preserved")
            self._add_worktree(caller_head)
            records = self._worktree_records()
            record = self._record_for_path(records)
        self._assert_clean_detached(record)
        self._fetch(default_branch)
        target = self._remote_head(default_branch)
        physical_head = self._head(self.worktree_path)
        if physical_head != target:
            self._assert_clean_detached(self._record_for_path(self._worktree_records()))
            self._remove_worktree()
            self._add_worktree(target)
        self._assert_clean_detached(self._record_for_path(self._worktree_records()))
        final_head = self._head(self.worktree_path)
        remote_head = self._remote_head(default_branch)
        if final_head != target or final_head != remote_head:
            raise RepoIntakeError("repository-intake SHA changed during preparation")
        generation = (
            old.generation
            if old is not None
            and old.revision == final_head
            and old.default_branch == default_branch
            else old.generation + 1
            if old is not None
            else 1
        )
        receipt = RepoIntakeReceipt(
            repository=self.repository,
            repository_identity=self.repository_identity,
            ownership_key=self.ownership_key,
            common_dir=self.common_dir,
            path=self.worktree_path.resolve(),
            default_branch=default_branch,
            revision=final_head,
            generation=generation,
        )
        self._write_receipt(receipt)
        return receipt

    def _resolve_common_dir(self) -> Path:
        """Resolve the Git common directory without changing the checkout."""
        raw = self._run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=self.caller_root,
        ).stdout.strip()
        if not raw:
            raise RepoIntakeError("Git common directory is unavailable")
        common = Path(raw)
        if not common.is_absolute():
            common = self.caller_root / common
        try:
            resolved = common.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RepoIntakeError("Git common directory cannot be resolved") from exc
        if not resolved.is_dir():
            raise RepoIntakeError("Git common directory is not a directory")
        return resolved

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a controlled command and map subprocess failures safely."""
        try:
            return self._run_command(
                command,
                cwd=cwd,
                check=check,
                timeout=self.timeout_s,
                env=self._git_env if env is None else env,
                log_errors=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            label = command[1] if len(command) > 1 else command[0]
            raise RepoIntakeError(f"repository-intake {label} failed") from exc

    def _validate_origin(self) -> None:
        """Accept only the expected GitHub origin URL."""
        origin = self._run(
            ["git", "remote", "get-url", "origin"], cwd=self.caller_root
        ).stdout.strip()
        normalized = origin.rstrip("/").removesuffix(".git")
        expected = {
            f"https://github.com/{self.repository}",
            f"ssh://git@github.com/{self.repository}",
            f"git@github.com:{self.repository}",
        }
        if normalized not in expected:
            raise RepoIntakeError(
                f"checkout has unexpected origin; expected origin {self.repository}"
            )

    def _validate_state_paths(self) -> None:
        """Reject symlinked or non-directory intake state containers."""
        if self.state_parent.is_symlink() or (
            self.state_parent.exists() and not self.state_parent.is_dir()
        ):
            raise RepoIntakeError(f"repository-intake state parent is unsafe: {self.state_parent}")
        if self.state_dir.is_symlink() or (self.state_dir.exists() and not self.state_dir.is_dir()):
            raise RepoIntakeError(f"repository-intake state path is unsafe: {self.state_dir}")
        if self.receipt_path.is_symlink() or (
            self.receipt_path.exists() and not self.receipt_path.is_file()
        ):
            raise RepoIntakeError(f"repository-intake receipt path is unsafe: {self.receipt_path}")
        if self.worktree_path.is_symlink():
            raise RepoIntakeError(f"repository-intake worktree is symlinked: {self.worktree_path}")
        self.state_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_dir.mkdir(mode=0o700, exist_ok=True)
        if self.state_parent.stat().st_mode & 0o077 or self.state_dir.stat().st_mode & 0o077:
            raise RepoIntakeError(
                f"repository-intake state permissions are unsafe: {self.state_dir}"
            )

    def _read_receipt(self) -> RepoIntakeReceipt | None:
        """Read the owned receipt, if one exists."""
        if not self.receipt_path.exists():
            return None
        try:
            if self.receipt_path.stat().st_mode & 0o077:
                raise RepoIntakeError("repository-intake receipt permissions are unsafe")
            payload = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        except RepoIntakeError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise RepoIntakeError(
                f"cannot read repository-intake receipt: {self.receipt_path}"
            ) from exc
        if not isinstance(payload, dict):
            raise RepoIntakeError("repository-intake receipt must be an object")
        receipt = RepoIntakeReceipt.from_dict(payload)
        if (
            receipt.repository != self.repository
            or receipt.repository_identity != self.repository_identity
            or receipt.ownership_key != self.ownership_key
            or receipt.common_dir != self.common_dir
            or receipt.path != self.worktree_path.resolve()
        ):
            raise RepoIntakeError("repository-intake receipt ownership does not match")
        return receipt

    def _worktree_records(self) -> tuple[_WorktreeRecord, ...]:
        """Read the registered worktrees from the shared Git metadata."""
        output = self._run(["git", "worktree", "list", "--porcelain"], cwd=self.caller_root).stdout
        records: list[_WorktreeRecord] = []
        current_path: Path | None = None
        current_head: str | None = None
        current_branch: str | None = None

        def finish() -> None:
            if current_path is None or current_head is None or not is_full_commit_sha(current_head):
                raise RepoIntakeError("Git worktree registration is malformed")
            records.append(_WorktreeRecord(current_path, current_head, current_branch))

        for line in output.splitlines():
            if line.startswith("worktree "):
                if current_path is not None:
                    finish()
                current_path = Path(line.removeprefix("worktree "))
                if not current_path.is_absolute():
                    raise RepoIntakeError("Git worktree registration has a relative path")
                current_head = None
                current_branch = None
            elif line.startswith("HEAD "):
                current_head = line.removeprefix("HEAD ")
            elif line.startswith("branch "):
                current_branch = line.removeprefix("branch ")
            elif line == "detached" or line in _KNOWN_WORKTREE_LINES or line.startswith("reason "):
                continue
            elif line:
                raise RepoIntakeError("Git worktree registration contains an unknown record")
        if current_path is not None:
            finish()
        return tuple(records)

    def _record_for_path(self, records: tuple[_WorktreeRecord, ...]) -> _WorktreeRecord | None:
        """Return the unique registration for the intake path."""
        matches = tuple(
            record for record in records if self._same_path(record.path, self.worktree_path)
        )
        if len(matches) > 1:
            raise RepoIntakeError("repository-intake worktree registration is ambiguous")
        return matches[0] if matches else None

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        """Compare worktree paths without requiring both paths to exist."""
        try:
            return left.resolve() == right.resolve()
        except (OSError, RuntimeError):
            return left.absolute() == right.absolute()

    def _validate_existing(
        self,
        receipt: RepoIntakeReceipt | None,
        records: tuple[_WorktreeRecord, ...],
    ) -> _WorktreeRecord | None:
        """Validate the receipt and physical registration before reuse."""
        record = self._record_for_path(records)
        exists = self.worktree_path.exists()
        if receipt is None:
            if exists or record is not None:
                raise RepoIntakeError("unowned repository-intake worktree is preserved")
            return None
        if not exists or record is None:
            raise RepoIntakeError(
                "repository-intake receipt and worktree registration do not match"
            )
        if record.branch is not None or not receipt.detached or receipt.branch is not None:
            raise RepoIntakeError("repository-intake worktree is branch-backed")
        if not is_full_commit_sha(record.head):
            raise RepoIntakeError("repository-intake worktree has a malformed HEAD")
        if record.head != receipt.revision:
            raise RepoIntakeError("repository-intake receipt and worktree HEAD do not match")
        return record

    def _assert_clean_detached(self, record: _WorktreeRecord | None) -> None:
        """Require the owned intake worktree to be clean and detached."""
        if record is None:
            raise RepoIntakeError("repository-intake worktree is not registered")
        status = self._run(
            ["git", "-c", "core.fsmonitor=false", "status", "--porcelain", "--untracked-files=all"],
            cwd=self.worktree_path,
        ).stdout.strip()
        if status:
            raise RepoIntakeError(
                f"repository-intake worktree is dirty and preserved: {self.worktree_path}"
            )
        branch = self._run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=self.worktree_path,
            check=False,
        )
        if branch.returncode == 0 and branch.stdout.strip():
            raise RepoIntakeError(f"repository-intake worktree is attached: {self.worktree_path}")
        if branch.returncode != 1:
            raise RepoIntakeError("repository-intake detached-head proof failed")

    def _read_default_branch(self) -> str:
        """Read the default branch through the trusted GitHub executable."""
        result = self._run(
            [self.gh_command, "api", f"repos/{self.repository}", "--jq", ".default_branch"],
            cwd=self.worktree_path if self.worktree_path.exists() else self.caller_root,
        )
        branch = result.stdout.strip()
        if not _is_valid_branch(branch):
            raise RepoIntakeError(f"repository default branch is invalid: {self.repository}")
        return branch

    def _head(self, checkout: Path) -> str:
        """Read and validate one worktree HEAD."""
        head = self._run(["git", "rev-parse", "HEAD"], cwd=checkout).stdout.strip()
        if not is_full_commit_sha(head):
            raise RepoIntakeError(f"repository-intake returned malformed HEAD: {checkout}")
        return head

    def _add_worktree(self, revision: str) -> None:
        """Add a detached worktree at an exact full revision."""
        if not is_full_commit_sha(revision):
            raise RepoIntakeError("repository-intake worktree revision is malformed")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "worktree",
                "add",
                "--detach",
                str(self.worktree_path),
                revision,
            ],
            cwd=self.caller_root,
        )

    def _remove_worktree(self) -> None:
        """Remove only the clean, registered intake worktree."""
        self._run(["git", "worktree", "remove", str(self.worktree_path)], cwd=self.caller_root)

    def _fetch(self, default_branch: str) -> None:
        """Fetch only the validated remote default branch in controlled mode."""
        command = [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            *self._remote_config,
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "origin",
            f"refs/heads/{default_branch}:refs/remotes/origin/{default_branch}",
        ]
        self._run(command, cwd=self.worktree_path)

    def _remote_head(self, default_branch: str) -> str:
        """Read the fetched remote branch as a full commit SHA."""
        head = self._run(
            ["git", "rev-parse", "--verify", f"refs/remotes/origin/{default_branch}^{{commit}}"],
            cwd=self.worktree_path,
        ).stdout.strip()
        if not is_full_commit_sha(head):
            raise RepoIntakeError("fetched default branch has a malformed SHA")
        return head

    def _write_receipt(self, receipt: RepoIntakeReceipt) -> None:
        """Write the verified receipt atomically with owner-only permissions."""
        write_secure(
            self.receipt_path,
            json.dumps(receipt.to_dict(), sort_keys=True, indent=2) + "\n",
        )


__all__ = ["RepoIntakeError", "RepoIntakeManager", "RepoIntakeReceipt", "is_full_commit_sha"]
