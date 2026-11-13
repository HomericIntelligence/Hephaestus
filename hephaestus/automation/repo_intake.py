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
import logging
import os
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Self, TypeGuard

from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.git.config_safety import unsafe_local_git_config_key
from hephaestus.git.runtime import (
    current_operation_shutdown,
    git_metadata_lock_path,
    operation_deadline,
    operation_file_lock,
    remaining_operation_timeout,
)
from hephaestus.io.utils import write_secure
from hephaestus.utils.file_lock import (
    ExclusiveLockUnavailableError,
    LockUnavailableError,
    file_lock,
)
from hephaestus.utils.worktree_identity import (
    source_worktree_name,
)


class RepoIntakeError(RuntimeError):
    """Raised when an intake checkout cannot be verified safely."""


class RepoIntakeInUseError(RepoIntakeError):
    """Raised when a different process owns the repository-intake run lease."""


def is_full_commit_sha(value: object) -> TypeGuard[str]:
    """Return whether *value* is a lower-case full SHA-1 or SHA-256."""
    return bool(
        isinstance(value, str)
        and len(value) in (40, 64)
        and all(character in "0123456789abcdef" for character in value)
    )


_LAUNCH_IDENTITY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


def is_valid_launch_identity(value: object) -> TypeGuard[str]:
    """Return whether *value* is a safe per-launch directory token."""
    return bool(isinstance(value, str) and _LAUNCH_IDENTITY_RE.fullmatch(value))


@dataclass(frozen=True, slots=True)
class RepoIntakeReceipt:
    """Durable ownership and revision proof for one intake checkout.

    A v3 receipt belongs to one coordinator launch. Its ``launch_identity``
    selects the disposable ``launches/<identity>`` directory that holds the
    worktree, the receipt, and the owner lock.
    """

    repository: str
    repository_identity: str
    ownership_key: str
    common_dir: Path
    path: Path
    state_root: Path
    default_branch: str
    revision: str
    generation: int
    launch_identity: str
    detached: bool = True
    branch: str | None = None
    schema_version: int = 3

    def to_dict(self) -> dict[str, object]:
        """Return the receipt in its closed JSON representation."""
        return {
            "schema_version": self.schema_version,
            "repository": self.repository,
            "repository_identity": self.repository_identity,
            "ownership_key": self.ownership_key,
            "common_dir": str(self.common_dir),
            "path": str(self.path),
            "state_root": str(self.state_root),
            "default_branch": self.default_branch,
            "revision": self.revision,
            "generation": self.generation,
            "launch_identity": self.launch_identity,
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
            "state_root",
            "default_branch",
            "revision",
            "generation",
            "launch_identity",
            "detached",
            "branch",
        }
        if set(payload) != fields:
            raise RepoIntakeError("repository-intake receipt schema mismatch")
        if not isinstance(payload["schema_version"], int) or isinstance(
            payload["schema_version"], bool
        ):
            raise RepoIntakeError("repository-intake receipt schema version is invalid")
        if payload["schema_version"] != 3:
            raise RepoIntakeError("repository-intake receipt schema mismatch")
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
            "state_root",
            "default_branch",
            "revision",
            "launch_identity",
        )
        if any(not isinstance(payload[name], str) or not payload[name] for name in string_fields):
            raise RepoIntakeError("repository-intake receipt contains an invalid value")
        branch = payload["branch"]
        if branch is not None and (not isinstance(branch, str) or not branch):
            raise RepoIntakeError("repository-intake receipt branch is invalid")
        if not is_valid_launch_identity(payload["launch_identity"]):
            raise RepoIntakeError("repository-intake receipt launch identity is invalid")
        try:
            receipt = cls(
                schema_version=payload["schema_version"],
                repository=payload["repository"],
                repository_identity=payload["repository_identity"],
                ownership_key=payload["ownership_key"],
                common_dir=Path(payload["common_dir"]),
                path=Path(payload["path"]),
                state_root=Path(payload["state_root"]),
                default_branch=payload["default_branch"],
                revision=payload["revision"],
                generation=payload["generation"],
                launch_identity=payload["launch_identity"],
                detached=payload["detached"],
                branch=branch,
            )
        except (TypeError, ValueError) as exc:
            raise RepoIntakeError(f"invalid repository-intake receipt: {exc}") from exc
        _validate_receipt_values(receipt)
        return receipt


_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_KNOWN_WORKTREE_LINES = ("locked", "prunable")
_DURABLE_STATE_NAMES = (".automation-state", Path(DEFAULT_STATE_DIR).name)
_GIT_METADATA_TEXT_LIMIT = 4096
_GIT_CONFIG_TEXT_LIMIT = 1024 * 1024


def _validate_receipt_values(receipt: RepoIntakeReceipt) -> None:
    """Validate receipt values and their durable-state relationship."""
    if (
        receipt.generation < 1
        or not receipt.common_dir.is_absolute()
        or not receipt.path.is_absolute()
        or not receipt.state_root.is_absolute()
        or not receipt.detached
        or receipt.branch is not None
        or not is_full_commit_sha(receipt.revision)
        or not _is_valid_branch(receipt.default_branch)
        or not is_valid_launch_identity(receipt.launch_identity)
    ):
        raise RepoIntakeError("repository-intake receipt values are unsafe")
    if receipt.state_root != receipt.path.parent:
        raise RepoIntakeError("repository-intake receipt ownership does not match")
    for candidate in (receipt.common_dir, receipt.path, receipt.state_root):
        if candidate.is_symlink():
            raise RepoIntakeError("repository-intake receipt contains a symlinked path")
    if receipt.state_root.exists() and not receipt.state_root.is_dir():
        raise RepoIntakeError("repository-intake receipt state root is unsafe")


_REGISTRATION_FIELDS = frozenset(
    {
        "schema_version",
        "repository",
        "repository_identity",
        "common_dir",
        "digest",
        "state_root",
        "default_branch",
        "active_launch_identity",
        "active_revision",
        "ownership_key",
    }
)


def _read_worker_registration(
    path: Path, *, common_dir: Path, state_root: Path | None = None
) -> dict[str, Any]:
    """Read one intake registration with bounded no-follow file checks."""
    digest = hashlib.sha256(str(common_dir).encode()).hexdigest()[:16]
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise RepoIntakeError("repository-intake worker registration is unsafe")
            encoded = stream.read(65537)
        if len(encoded) > 65536:
            raise RepoIntakeError("repository-intake worker registration is too large")
        payload = json.loads(encoded)
    except (OSError, ValueError) as exc:
        raise RepoIntakeError(
            "repository-intake worker registration is unavailable or invalid"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != _REGISTRATION_FIELDS:
        raise RepoIntakeError("repository-intake worker registration schema mismatch")
    if payload.get("schema_version") != 3 or isinstance(payload.get("schema_version"), bool):
        raise RepoIntakeError("repository-intake worker registration schema mismatch")
    string_fields = _REGISTRATION_FIELDS - {"schema_version"}
    if any(not isinstance(payload[name], str) or not payload[name] for name in string_fields):
        raise RepoIntakeError("repository-intake worker registration contains an invalid value")
    repository = str(payload["repository"])
    if "/" not in repository:
        raise RepoIntakeError("repository-intake worker registration identity is invalid")
    identity = f"{repository}:{digest}"
    derived_state_root = Path(str(payload["state_root"]))
    if (
        payload["common_dir"] != str(common_dir)
        or payload["digest"] != digest
        or payload["repository_identity"] != identity
        or payload["ownership_key"] != f"{identity}:intake"
        or (state_root is not None and derived_state_root != state_root)
        or not is_valid_launch_identity(payload["active_launch_identity"])
        or not is_full_commit_sha(payload["active_revision"])
        or not _is_valid_branch(payload["default_branch"])
    ):
        raise RepoIntakeError("repository-intake worker registration ownership does not match")
    return payload


def intake_worker_base(
    repo_root: Path, common_dir: Path, *, repository: str | None = None
) -> Path | None:
    """Return the external worker base for a registration-bound intake state root.

    The durable ``intake-registration.json`` outlives every launch, so the
    answer survives the owning launch's cleanup. While the active launch
    receipt is still present, it must also agree with the registration and
    the requested checkout root exactly as the launch receipt did before.
    """
    digest = hashlib.sha256(str(common_dir).encode()).hexdigest()[:16]
    state_parent = common_dir.parent.parent / ".hephaestus-repo-intake"
    state_root = state_parent / digest
    # Fail quiet before any state read: only a root inside the launch state
    # is a candidate intake checkout, and the state checks stay lexical until
    # that candidate gate passes.
    if not repo_root.is_relative_to(state_root / "launches"):
        return None
    for path in (state_parent, state_root):
        if path.is_symlink() or path.resolve(strict=True) != path:
            raise RepoIntakeError("repository-intake worker state path is unsafe")
    registration = _read_worker_registration(
        state_root / "intake-registration.json",
        common_dir=common_dir,
        state_root=state_root,
    )
    if repository is not None and repository not in {
        registration["repository"],
        str(registration["repository"]).rsplit("/", 1)[-1],
    }:
        raise RepoIntakeError("repository-intake worker repository does not match")
    active = str(registration["active_launch_identity"])
    launch_root = state_root / "launches" / active
    expected_root = launch_root / "worktree"
    if repo_root != expected_root:
        return None
    launch_receipt_path = launch_root / "receipt.json"
    if launch_receipt_path.exists():
        _assert_launch_receipt_agrees_with_registration(
            launch_receipt_path,
            common_dir=common_dir,
            repository_identity=f"{registration['repository']}:{digest}",
            active_launch_identity=active,
            expected_root=expected_root,
            launch_root=launch_root,
        )
    base = state_root / "build" / ".worktrees"
    if base.resolve(strict=False) != base:
        raise RepoIntakeError("repository-intake worker base is unsafe")
    return base


def _assert_launch_receipt_agrees_with_registration(
    launch_receipt_path: Path,
    *,
    common_dir: Path,
    repository_identity: str,
    active_launch_identity: str,
    expected_root: Path,
    launch_root: Path,
) -> None:
    """Raise when a live launch receipt disagrees with the durable registration.

    A post-cleanup launch tree is absent, so this proof only runs while the
    active launch still owns its intake. The receipt must then agree with
    the registration exactly as a launch receipt did before.
    """
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(launch_receipt_path, flags), "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise RepoIntakeError("repository-intake worker receipt is unsafe")
            encoded = stream.read(65537)
        if len(encoded) > 65536:
            raise RepoIntakeError("repository-intake worker receipt is too large")
        payload = json.loads(encoded)
        if not isinstance(payload, dict):
            raise RepoIntakeError("repository-intake worker receipt must be an object")
        receipt = RepoIntakeReceipt.from_dict(payload)
    except (OSError, ValueError) as exc:
        raise RepoIntakeError("repository-intake worker receipt is unavailable or invalid") from exc
    if (
        receipt.launch_identity != active_launch_identity
        or receipt.common_dir != common_dir
        or receipt.path != expected_root
        or receipt.state_root != launch_root
        or receipt.repository_identity != repository_identity
        or receipt.ownership_key != f"{repository_identity}:intake:{active_launch_identity}"
    ):
        raise RepoIntakeError("repository-intake worker receipt ownership does not match")


def checkout_intake_worker_base(repo_root: Path, *, repository: str | None = None) -> Path | None:
    """Read intake worker authority without creating state."""
    root = repo_root.resolve(strict=True)
    if not (root / ".git").exists():
        return None
    common = git_metadata_lock_path(root).parent.resolve(strict=True)
    return intake_worker_base(root, common, repository=repository)


def repository_worker_path_is_valid(
    repo_root: Path,
    worktree: Path,
    *,
    repository: str,
    item_number: int | None = None,
    lane: str = "impl",
) -> bool:
    """Check checkout ancestry or the exact verified intake worker boundary."""
    base = (
        checkout_intake_worker_base(repo_root, repository=repository)
        if repo_root.exists()
        else None
    )
    if base is None:
        return worktree == repo_root or repo_root in worktree.parents
    if (
        repo_root.is_symlink()
        or repo_root.resolve(strict=True) != repo_root
        or worktree.parent != base
        or not isinstance(item_number, int)
        or isinstance(item_number, bool)
        or item_number <= 0
        or lane not in {"impl", "review"}
        or worktree.name != source_worktree_name(item_number, lane)
    ):
        return False
    if worktree.is_symlink() or worktree.resolve(strict=True) != worktree:
        return False
    return git_metadata_lock_path(worktree).parent.resolve(strict=True) == (
        git_metadata_lock_path(repo_root).parent.resolve(strict=True)
    )


def _copy_durable_state(source: Path, destination_root: Path) -> None:
    """Copy one durable state directory into a canonical state root.

    The source is copied, never moved. Intake preparation can still fail after
    this point, and it must not destroy caller state on the way. An entry that
    already exists in the destination wins, so the copy stays idempotent across
    runs.
    """
    if not source.is_dir() or source.is_symlink():
        return
    destination = destination_root / source.name
    destination.mkdir(parents=True, exist_ok=True)
    existing = {entry.name for entry in destination.iterdir()}
    for entry in sorted(source.iterdir()):
        if entry.name in existing:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.copytree(entry, destination / entry.name, symlinks=True)
        else:
            shutil.copy2(entry, destination / entry.name, follow_symlinks=False)


def repository_host_state_root(repo_root: Path) -> Path:
    """Select durable host storage without changing source identity.

    The loop's own intake worktree is exempt. Durable state written under its
    ``build`` directory is the loop's own output, not caller legacy state, so
    it moves to the intake state root instead of blocking the run. The earlier
    check refused on bare existence, which made the loop reject the receipts
    it had just written.
    """
    root = repo_root.resolve(strict=True)
    base = checkout_intake_worker_base(root)
    if base is None:
        return root
    state_root = base.parent.parent
    for name in _DURABLE_STATE_NAMES:
        legacy = root / "build" / name
        if legacy.is_symlink():
            raise RepoIntakeError("legacy intake state path is unsafe")
        _copy_durable_state(legacy, state_root / "build")
    return state_root


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
    """Create and remove one launch-owned ephemeral intake worktree."""

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
        launch_identity: str,
    ) -> None:
        """Initialize one launch-owned intake manager for a caller checkout."""
        if not repository or "/" not in repository:
            raise RepoIntakeError("repository identity is invalid")
        if timeout_s <= 0:
            raise RepoIntakeError("repository-intake timeout is invalid")
        if not is_valid_launch_identity(launch_identity):
            raise RepoIntakeError(
                f"repository-intake launch identity is invalid: {launch_identity!r}"
            )
        if caller_root.is_symlink():
            raise RepoIntakeError(f"caller checkout is symlinked: {caller_root}")
        try:
            self.caller_root = caller_root.resolve(strict=True)
        except (OSError, RuntimeError, UnicodeError) as exc:
            raise RepoIntakeError(f"caller checkout cannot be resolved: {caller_root}") from exc
        if not self.caller_root.is_dir():
            raise RepoIntakeError(f"caller checkout does not exist: {self.caller_root}")
        self.repository = repository
        self.gh_command = gh_command
        self.timeout_s = timeout_s
        self._run_command = git_runner
        self._git_env = dict(git_env)
        self._remote_config = tuple(remote_config)
        self.launch_identity = launch_identity
        self.common_dir = self._resolve_common_dir()
        digest = hashlib.sha256(str(self.common_dir).encode()).hexdigest()[:16]
        self.repository_identity = f"{repository}:{digest}"
        # Keep the launch tree outside the caller root.  The common directory
        # is shared by linked worktrees, so this path remains stable when the
        # caller is detached or linked elsewhere.
        state_parent = self.common_dir.parent.parent / ".hephaestus-repo-intake"
        self.state_parent = state_parent
        # Keep this path lexical.  Resolving it here would follow an attacker-
        # supplied symlink before validation and make the ownership check
        # inspect a different path than the one selected by the manager.
        self.state_dir = state_parent / digest
        self.launch_dir = self.state_dir / "launches" / launch_identity
        self.worktree_path = self.launch_dir / "worktree"
        self.receipt_path = self.launch_dir / "receipt.json"
        self.owner_lock_path = self.launch_dir / "owner.lock"
        # The durable registration outlives any single launch.  Only the v3
        # launch receipt extends the ownership key with the launch identity.
        self.registration_path = self.state_dir / "intake-registration.json"
        self.ownership_key = f"{self.repository_identity}:intake"
        self.launch_ownership_key = f"{self.ownership_key}:{launch_identity}"

    @contextmanager
    def owner(self) -> Iterator[None]:
        """Hold exclusive per-launch intake ownership for this launch identity."""
        with self._owner_file_lock():
            self._owner_lock_held = True
            try:
                yield
            finally:
                self._owner_lock_held = False

    @contextmanager
    def _owner_file_lock(self) -> Iterator[None]:
        """Hold the launch owner lock, reentrant for this manager instance."""
        if getattr(self, "_owner_lock_held", False):
            yield
            return
        try:
            with file_lock(self.owner_lock_path, blocking=False, require_exclusive=True):
                self._owner_lock_held = True
                try:
                    yield
                finally:
                    self._owner_lock_held = False
        except ExclusiveLockUnavailableError as exc:
            raise RepoIntakeError(
                "exclusive repository-intake launch locking is unavailable"
            ) from exc
        except LockUnavailableError as exc:
            raise RepoIntakeError(
                "repository_intake_launch_in_use: a duplicate launch identity owns the "
                "repository-intake owner lock; use a unique launch identity and retry"
            ) from exc
        except InterruptedError:
            raise
        except RepoIntakeError:
            raise
        except (OSError, RuntimeError) as exc:
            raise RepoIntakeError("repository-intake launch owner lock is unavailable") from exc

    def prepare(
        self,
        *,
        operational_state_paths: Collection[Path] = (),
        admitted_metadata_lock: Path | None = None,
        expected_compatibility_lock_paths: Collection[Path] | None = None,
    ) -> RepoIntakeReceipt:
        """Return a verified launch intake receipt, creating the intake once."""
        try:
            started_s = time.monotonic()
            remaining_s = remaining_operation_timeout(self.timeout_s)
            if remaining_s is None:
                raise RepoIntakeError("repository-intake timeout is unavailable")
            deadline_s = started_s + float(remaining_s)
            metadata_lock = git_metadata_lock_path(self.caller_root)
            metadata_lock = metadata_lock.parent.resolve(strict=True) / metadata_lock.name
            if admitted_metadata_lock is not None and metadata_lock != admitted_metadata_lock:
                raise RepoIntakeError("Git common directory changed before preparation")
            metadata_lock = admitted_metadata_lock or metadata_lock
        except (subprocess.TimeoutExpired, InterruptedError):
            raise
        except RepoIntakeError:
            raise
        except (OSError, RuntimeError, UnicodeError) as exc:
            raise RepoIntakeError("Git metadata lock is unavailable") from exc
        try:
            with (
                operation_deadline(deadline_s, shutdown=current_operation_shutdown()),
                self._owner_file_lock(),
                operation_file_lock(metadata_lock, require_exclusive=True),
            ):
                return self._prepare_locked(
                    operational_state_paths=operational_state_paths,
                    expected_compatibility_lock_paths=expected_compatibility_lock_paths,
                )
        except (subprocess.TimeoutExpired, InterruptedError):
            raise
        except ExclusiveLockUnavailableError as exc:
            raise RepoIntakeError("exclusive Git metadata locking is unavailable") from exc
        except RepoIntakeError:
            raise
        except (OSError, RuntimeError) as exc:
            raise RepoIntakeError("repository-intake preparation failed safely") from exc

    def validate(
        self,
        *,
        operational_state_paths: Collection[Path] = (),
    ) -> tuple[Path, ...]:
        """Validate state and return registered primary compatibility locks."""
        _records, allowed_paths = self._validated_state(
            operational_state_paths=operational_state_paths
        )
        return self._primary_compatibility_lock_paths(allowed_paths)

    def _validated_state(
        self,
        *,
        operational_state_paths: Collection[Path] = (),
    ) -> tuple[tuple[_WorktreeRecord, ...], frozenset[Path]]:
        """Return validated preparation state without a Git mutation."""
        self._require_safe_common_dir()
        self._validate_origin()
        records = self._worktree_records()
        self._validate_state_paths(records)
        allowed_paths = self._validate_state_authority(
            records,
            operational_state_paths=operational_state_paths,
        )
        return records, allowed_paths

    def _prepare_locked(
        self,
        *,
        operational_state_paths: Collection[Path] = (),
        expected_compatibility_lock_paths: Collection[Path] | None = None,
    ) -> RepoIntakeReceipt:
        """Prepare the launch intake worktree while the common metadata lock is held."""
        records, allowed_paths = self._validated_state(
            operational_state_paths=operational_state_paths
        )
        if expected_compatibility_lock_paths is not None:
            expected_paths = self._canonical_compatibility_lock_paths(
                expected_compatibility_lock_paths
            )
            actual_paths = self._primary_compatibility_lock_paths(allowed_paths)
            if actual_paths != expected_paths:
                raise RepoIntakeError(
                    "repository-intake worktree registration changed before preparation"
                )
        # Recovery first. Abandoned launches and the legacy permanent intake
        # are removed under their lock proofs before this launch creates its
        # own intake. A launch directory whose owner lock is held stays live.
        self._recover_abandoned_intakes_locked(records)
        records = self._worktree_records()
        record = self._record_for_path(records)
        if record is not None or self.worktree_path.exists():
            # Launch names are unique and the sweep just proved its entries,
            # so a registration at this launch path is foreign state.
            raise RepoIntakeError("unowned repository-intake worktree is preserved")
        default_branch = self._read_default_branch()
        self._fetch(default_branch, checkout=self.caller_root)
        target = self._remote_head(default_branch, checkout=self.caller_root)
        self._add_worktree(target)
        try:
            records = self._worktree_records()
            record = self._record_for_path(records)
            self._assert_no_registered_descendants(records)
            self._validate_intake_checkout()
            self._assert_clean_detached(record)
            final_head = self._head(self.worktree_path)
            remote_head = self._remote_head(default_branch, checkout=self.worktree_path)
            if final_head != target or final_head != remote_head:
                raise RepoIntakeError("repository-intake SHA changed during preparation")
            receipt = RepoIntakeReceipt(
                repository=self.repository,
                repository_identity=self.repository_identity,
                ownership_key=self.launch_ownership_key,
                common_dir=self.common_dir,
                path=self.worktree_path.resolve(),
                state_root=self.launch_dir.resolve(),
                default_branch=default_branch,
                revision=final_head,
                generation=1,
                launch_identity=self.launch_identity,
            )
            self._write_registration(default_branch=default_branch, revision=final_head)
            self._write_receipt(receipt)
            return receipt
        except BaseException as error:
            rollback_failure = self._rollback_launch_preparation()
            if rollback_failure is not None:
                raise RepoIntakeError(
                    "repository-intake launch preparation rollback failed; the launch "
                    f"tree is preserved at {self.launch_dir}; original error: {error}"
                ) from rollback_failure
            raise

    def _rollback_launch_preparation(self) -> BaseException | None:
        """Remove a just-added launch intake after a preparation failure.

        The metadata lock is still held. The rollback re-proves clean,
        detached, descendant-free state before the Git removal. Any rollback
        failure preserves the launch tree untouched and is returned so the
        caller can re-raise the original error with the retained path.
        """
        try:
            records = self._worktree_records()
            record = self._record_for_path(records)
            if record is not None:
                self._assert_no_registered_descendants(records)
                self._validate_intake_checkout()
                self._assert_clean_detached(record)
                self._remove_worktree()
            self._remove_launch_tree()
        except BaseException as rollback_error:
            return rollback_error
        return None

    def cleanup(self) -> None:
        """Remove this launch intake after its consumers stop.

        The cleanup proves fresh ownership facts under the shared metadata
        lock, removes the Git worktree registration and the launch tree, and
        preserves any dirty, foreign, or symlinked state as a structured
        failure that carries the retained path.
        """
        metadata_lock = self._resolve_metadata_lock()
        try:
            with (
                operation_deadline(
                    time.monotonic() + float(remaining_operation_timeout(self.timeout_s) or 0),
                    shutdown=current_operation_shutdown(),
                ),
                operation_file_lock(metadata_lock, require_exclusive=True),
            ):
                self._cleanup_locked()
        except (subprocess.TimeoutExpired, InterruptedError):
            raise
        except ExclusiveLockUnavailableError as exc:
            raise RepoIntakeError("exclusive Git metadata locking is unavailable") from exc
        except RepoIntakeError:
            raise
        except (OSError, RuntimeError) as exc:
            raise RepoIntakeError("repository-intake cleanup failed safely") from exc

    def _resolve_metadata_lock(self) -> Path:
        """Return the resolved Git metadata lock path for this caller."""
        try:
            metadata_lock = git_metadata_lock_path(self.caller_root)
            return metadata_lock.parent.resolve(strict=True) / metadata_lock.name
        except (OSError, RuntimeError, UnicodeError) as exc:
            raise RepoIntakeError("Git metadata lock is unavailable") from exc

    def _cleanup_locked(self) -> None:
        """Remove the launch worktree and tree while the metadata lock is held."""
        records = self._worktree_records()
        record = self._record_for_path(records)
        receipt = self._read_receipt()
        if record is not None:
            if receipt is None:
                raise RepoIntakeError(
                    f"repository-intake launch receipt is missing and preserved: {self.launch_dir}"
                )
            self._assert_no_registered_descendants(records)
            self._validate_intake_checkout()
            self._assert_clean_detached(record)
            if record.head != receipt.revision:
                raise RepoIntakeError("repository-intake receipt and worktree HEAD do not match")
            self._require_registration_agreement(receipt)
            self._remove_worktree()
        elif self.worktree_path.exists():
            raise RepoIntakeError(
                "repository-intake launch worktree is unregistered and preserved: "
                f"{self.worktree_path}"
            )
        self._remove_launch_tree()

    def _require_registration_agreement(self, receipt: RepoIntakeReceipt) -> None:
        """Require the durable registration to agree with this launch receipt."""
        registration = self._read_registration()
        if registration is None:
            return
        if registration["active_launch_identity"] != self.launch_identity:
            # A later launch already owns the registration's active pointer.
            return
        if registration["active_revision"] != receipt.revision:
            raise RepoIntakeError("repository-intake registration does not match the receipt")

    def _remove_launch_tree(self) -> None:
        """Remove the launch tree lexically without following symlinks."""
        try:
            self.launch_dir.lstat()
        except FileNotFoundError:
            return
        _remove_tree_lexically(self.launch_dir)

    def _recover_abandoned_intakes_locked(self, records: tuple[_WorktreeRecord, ...]) -> None:
        """Remove abandoned launch intakes while the metadata lock is held."""
        recover_abandoned_intakes(self, records)

    @staticmethod
    def _canonical_compatibility_lock_paths(
        paths: Collection[Path],
    ) -> tuple[Path, ...]:
        """Canonicalize lock parents without following final lock entries."""
        try:
            canonical = {Path(path).absolute() for path in paths}
        except (OSError, RuntimeError, UnicodeError) as exc:
            raise RepoIntakeError("repository-intake operational state paths are invalid") from exc
        return tuple(sorted(canonical, key=os.fsencode))

    @classmethod
    def _primary_compatibility_lock_paths(
        cls,
        paths: Collection[Path],
    ) -> tuple[Path, ...]:
        """Return canonical primary paths from exact compatibility triplets."""
        return cls._canonical_compatibility_lock_paths(
            tuple(
                path
                for path in paths
                if not Path(path).name.endswith((".owner.lock", ".owner.json"))
            )
        )

    def _require_safe_common_dir(self) -> None:
        """Confirm the common directory exists as a directory.

        The loop runs in a caller-owned checkout on a shared host. The host
        umask and the parent directory modes are not the loop's policy, so
        the intake check does not read the mode or the owner of the common
        directory. An operator can see the mode with ``git rev-parse``.
        """
        if not self.common_dir.is_dir():
            raise RepoIntakeError(
                f"repository-intake Git common directory is missing: {self.common_dir}"
            )

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
        intake_path = getattr(self, "worktree_path", None)
        if intake_path is not None and cwd == intake_path and intake_path.exists():
            self._validate_intake_checkout()
        try:
            return self._run_command(
                command,
                cwd=cwd,
                check=check,
                timeout=self.timeout_s,
                env=self._git_env if env is None else env,
                log_errors=False,
            )
        except (subprocess.TimeoutExpired, InterruptedError):
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            label = next(
                (
                    argument
                    for argument in command[1:]
                    if argument
                    in {"fetch", "remote", "rev-parse", "status", "symbolic-ref", "worktree"}
                ),
                command[1] if len(command) > 1 else command[0],
            )
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

    def _validate_state_paths(self, records: tuple[_WorktreeRecord, ...]) -> None:
        """Reject symlinked or non-directory intake state containers."""
        for path, label, is_expected_kind in (
            (self.state_parent, "state parent", lambda candidate: candidate.is_dir()),
            (self.state_dir, "state path", lambda candidate: candidate.is_dir()),
            (
                self.state_dir / "launches",
                "launch root",
                lambda candidate: candidate.is_dir(),
            ),
            (self.launch_dir, "launch path", lambda candidate: candidate.is_dir()),
            (self.receipt_path, "receipt path", lambda candidate: candidate.is_file()),
            (
                self.registration_path,
                "registration path",
                lambda candidate: candidate.is_file(),
            ),
            (self.owner_lock_path, "owner lock path", lambda candidate: candidate.is_file()),
        ):
            if path.is_symlink() or (path.exists() and not is_expected_kind(path)):
                raise RepoIntakeError(f"repository-intake {label} is unsafe: {path}")
        if self.worktree_path.is_symlink():
            raise RepoIntakeError(f"repository-intake worktree is symlinked: {self.worktree_path}")
        protected_roots = (self.caller_root, *(record.path for record in records))
        state_paths = (
            self.state_parent,
            self.state_dir,
            self.launch_dir,
            self.receipt_path,
            self.registration_path,
            self.owner_lock_path,
            self.worktree_path,
        )
        for state_path in state_paths:
            for protected_root in protected_roots:
                if self._same_path(protected_root, self.worktree_path):
                    # Receipt validation below decides whether this exact
                    # registration is the owned intake or foreign state.
                    continue
                if self._path_is_within(state_path, protected_root):
                    raise RepoIntakeError(
                        f"repository-intake state overlaps a registered worktree: {state_path}"
                    )
        self.state_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_dir.mkdir(mode=0o700, exist_ok=True)
        (self.state_dir / "launches").mkdir(mode=0o700, exist_ok=True)
        self.launch_dir.mkdir(mode=0o700, exist_ok=True)

    def _read_receipt(self) -> RepoIntakeReceipt | None:
        """Read the owned launch receipt, if one exists."""
        if not self.receipt_path.exists():
            return None
        try:
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
            or receipt.ownership_key != self.launch_ownership_key
            or receipt.launch_identity != self.launch_identity
            or receipt.common_dir != self.common_dir
            or receipt.path != self.worktree_path.resolve()
            or receipt.state_root != self.launch_dir.resolve()
        ):
            raise RepoIntakeError("repository-intake receipt ownership does not match")
        return receipt

    def _read_registration(self) -> dict[str, Any] | None:
        """Read the durable intake registration, if one exists."""
        if not self.registration_path.exists():
            return None
        return _read_worker_registration(
            self.registration_path,
            common_dir=self.common_dir,
            state_root=self.state_dir,
        )

    def _write_registration(self, *, default_branch: str, revision: str) -> None:
        """Write the durable cross-launch intake registration atomically."""
        registration = {
            "schema_version": 3,
            "repository": self.repository,
            "repository_identity": self.repository_identity,
            "common_dir": str(self.common_dir),
            "digest": hashlib.sha256(str(self.common_dir).encode()).hexdigest()[:16],
            "state_root": str(self.state_dir),
            "default_branch": default_branch,
            "active_launch_identity": self.launch_identity,
            "active_revision": revision,
            "ownership_key": self.ownership_key,
        }
        write_secure(
            self.registration_path,
            json.dumps(registration, sort_keys=True, indent=2) + "\n",
        )

    def _migrate_durable_state(self, source: Path, destination_root: Path) -> None:
        """Copy one durable state directory into the canonical state root."""
        _copy_durable_state(source, destination_root)

    def _validate_state_authority(
        self,
        records: tuple[_WorktreeRecord, ...],
        *,
        operational_state_paths: Collection[Path] = (),
    ) -> frozenset[Path]:
        """Migrate durable state into the canonical root instead of blocking."""
        allowed_operational_paths = self._allowed_operational_state_paths(
            records,
            operational_state_paths,
        )
        destination_root = self.state_dir / "build"
        legacy_sources: list[Path] = []
        for legacy_root in self._legacy_worktree_roots(records):
            for name in _DURABLE_STATE_NAMES:
                source = legacy_root / "build" / name
                if self._state_directory_has_entries(
                    source,
                    root=legacy_root,
                    source=source,
                    destination=destination_root,
                    label="legacy",
                    operational_state_paths=allowed_operational_paths,
                ):
                    legacy_sources.append(source)
        recovery_source = (
            legacy_sources[0]
            if legacy_sources
            else self.caller_root / "build" / _DURABLE_STATE_NAMES[0]
        )
        for name in _DURABLE_STATE_NAMES:
            destination = destination_root / name
            self._state_directory_has_entries(
                destination,
                root=self.state_dir,
                source=recovery_source,
                destination=destination_root,
                label="destination",
            )
        for source in legacy_sources:
            self._migrate_durable_state(source, destination_root)
        return allowed_operational_paths

    def _legacy_worktree_roots(
        self,
        records: tuple[_WorktreeRecord, ...],
    ) -> tuple[Path, ...]:
        """Return unique registered roots other than the intake destination."""
        roots: list[Path] = []
        for record in records:
            if self._same_path(record.path, self.worktree_path):
                continue
            if any(self._same_path(record.path, root) for root in roots):
                continue
            roots.append(record.path)
        return tuple(roots)

    def _state_directory_has_entries(
        self,
        path: Path,
        *,
        root: Path,
        source: Path,
        destination: Path,
        label: str,
        operational_state_paths: frozenset[Path] = frozenset(),
    ) -> bool:
        """Validate one state path and report whether it contains an entry."""
        self._validate_state_path_chain(
            path,
            root=root,
            source=source,
            destination=destination,
            label=label,
        )
        if not path.exists():
            return False
        if not path.is_dir():
            self._raise_unsafe_state_path(label, path, source, destination)
        try:
            entries = tuple(path.iterdir())
        except OSError as exc:
            raise RepoIntakeError(
                "repository-intake state inspection failed; "
                f"preserve source {source} and destination {destination}, "
                "then reconcile them manually"
            ) from exc
        if not operational_state_paths:
            return bool(entries)
        if not entries:
            return False
        return self._operational_state_has_entries(
            path,
            entries=entries,
            source=source,
            destination=destination,
            label=label,
            operational_state_paths=operational_state_paths,
        )

    def _operational_state_has_entries(
        self,
        path: Path,
        *,
        entries: tuple[Path, ...],
        source: Path,
        destination: Path,
        label: str,
        operational_state_paths: frozenset[Path],
    ) -> bool:
        """Report entries outside one exact safe compatibility-lock set."""
        lock_directories = {item.parent for item in operational_state_paths}
        matching_directories = tuple(
            lock_directory for lock_directory in lock_directories if lock_directory.parent == path
        )
        if len(matching_directories) != 1:
            return bool(entries)
        lock_directory = matching_directories[0]
        if any(entry != lock_directory for entry in entries):
            return True
        allowed_lock_paths = frozenset(
            item for item in operational_state_paths if item.parent == lock_directory
        )
        return self._compatibility_lock_has_entries(
            lock_directory,
            source=source,
            destination=destination,
            label=label,
            operational_state_paths=allowed_lock_paths,
        )

    def _compatibility_lock_has_entries(
        self,
        lock_directory: Path,
        *,
        source: Path,
        destination: Path,
        label: str,
        operational_state_paths: frozenset[Path],
    ) -> bool:
        """Validate the compatibility-lock directory and its exact files."""
        try:
            lock_metadata = lock_directory.lstat()
            if not stat.S_ISDIR(lock_metadata.st_mode):
                self._raise_unsafe_state_path(
                    label,
                    lock_directory,
                    source,
                    destination,
                )
            lock_entries = tuple(lock_directory.iterdir())
        except OSError as exc:
            raise RepoIntakeError(
                "repository-intake state inspection failed; "
                f"preserve source {source} and destination {destination}, "
                "then reconcile them manually"
            ) from exc
        for entry in lock_entries:
            if entry not in operational_state_paths:
                return True
            try:
                metadata = entry.lstat()
            except OSError as exc:
                raise RepoIntakeError(
                    "repository-intake state inspection failed; "
                    f"preserve source {source} and destination {destination}, "
                    "then reconcile them manually"
                ) from exc
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                self._raise_unsafe_state_path(label, entry, source, destination)
        return False

    def _allowed_operational_state_paths(
        self,
        records: tuple[_WorktreeRecord, ...],
        paths: Collection[Path],
    ) -> frozenset[Path]:
        """Return exact compatibility-lock files for registered worktrees."""
        selected = frozenset(Path(path).absolute() for path in paths)
        if not selected:
            return frozenset()
        primary_candidates = tuple(
            path for path in selected if not path.name.endswith((".owner.lock", ".owner.json"))
        )
        if len(primary_candidates) != 1:
            raise RepoIntakeError("repository-intake operational state paths are invalid")
        primary = primary_candidates[0]
        expected = frozenset(
            {
                primary,
                Path(f"{primary}.owner.lock"),
                Path(f"{primary}.owner.json"),
            }
        )
        if selected != expected or not primary.name.startswith("git-"):
            raise RepoIntakeError("repository-intake operational state paths are invalid")
        if not primary.name.endswith(".lock"):
            raise RepoIntakeError("repository-intake operational state paths are invalid")
        legacy_roots = self._legacy_worktree_roots(records)
        canonical_roots: list[Path] = []
        for root in legacy_roots:
            try:
                canonical_roots.append(root.resolve(strict=True))
            except FileNotFoundError:
                # A stale registration cannot supply a compatibility lock.
                continue
            except (OSError, RuntimeError, UnicodeError) as exc:
                raise RepoIntakeError(
                    "repository-intake worktree registration is unavailable"
                ) from exc
        for root in canonical_roots:
            if primary.parent == root / DEFAULT_STATE_DIR / "locks":
                allowed: set[Path] = set()
                for registered_root in canonical_roots:
                    registered_primary = (
                        registered_root / DEFAULT_STATE_DIR / "locks" / primary.name
                    )
                    allowed.update(
                        {
                            registered_primary,
                            Path(f"{registered_primary}.owner.lock"),
                            Path(f"{registered_primary}.owner.json"),
                        }
                    )
                return frozenset(allowed)
        return frozenset()

    def _validate_state_path_chain(
        self,
        path: Path,
        *,
        root: Path,
        source: Path,
        destination: Path,
        label: str,
    ) -> None:
        """Require a lexical, nonsymlinked state path below its owner root."""
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            self._raise_unsafe_state_path(label, root, source, destination)
        try:
            relative = path.relative_to(root)
        except ValueError:
            self._raise_unsafe_state_path(label, path, source, destination)
        current = root
        for component in relative.parts:
            current /= component
            if current.is_symlink():
                self._raise_unsafe_state_path(label, current, source, destination)
            if current.exists() and not current.is_dir():
                self._raise_unsafe_state_path(label, current, source, destination)
        if path.exists() and not path.is_dir():
            self._raise_unsafe_state_path(label, path, source, destination)

    @staticmethod
    def _raise_unsafe_state_path(
        label: str,
        path: Path,
        source: Path,
        destination: Path,
    ) -> NoReturn:
        """Raise one actionable state-path error without changing either side."""
        raise RepoIntakeError(
            f"repository-intake {label} state path is unsafe: {path}; "
            f"preserve source {source} and destination {destination}, "
            "then reconcile them manually"
        )

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
            elif (
                line == "detached"
                or line.startswith("reason ")
                or any(
                    line == attribute or line.startswith(f"{attribute} ")
                    for attribute in _KNOWN_WORKTREE_LINES
                )
            ):
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

    @staticmethod
    def _path_is_within(candidate: Path, root: Path) -> bool:
        """Return whether a state path is equal to or below a protected root."""
        try:
            return candidate.resolve(strict=False).is_relative_to(root.resolve(strict=False))
        except (OSError, RuntimeError) as exc:
            raise RepoIntakeError("repository-intake path containment is unavailable") from exc

    @staticmethod
    def _directory_open_flags() -> int:
        """Return flags that open one directory without following its final link."""
        if os.name != "posix" or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            raise RepoIntakeError(
                "repository-intake linked-worktree proof requires no-follow descriptors"
            )
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    @staticmethod
    def _read_optional_metadata_text(
        directory_fd: int,
        name: str,
        *,
        max_bytes: int = _GIT_METADATA_TEXT_LIMIT,
    ) -> str | None:
        """Read one optional bounded regular file without following a link."""
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        file_fd = -1
        try:
            try:
                file_fd = os.open(name, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise RepoIntakeError("repository-intake Git metadata is not a regular file")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(file_fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > max_bytes:
                raise RepoIntakeError("repository-intake Git metadata is too large")
            return payload.decode("utf-8")
        except RepoIntakeError:
            raise
        except (OSError, UnicodeError) as exc:
            raise RepoIntakeError("repository-intake Git metadata is unavailable") from exc
        finally:
            if file_fd >= 0:
                os.close(file_fd)

    @classmethod
    def _read_metadata_text(
        cls,
        directory_fd: int,
        name: str,
        *,
        max_bytes: int = _GIT_METADATA_TEXT_LIMIT,
    ) -> str:
        """Read one required bounded regular file without following a link."""
        payload = cls._read_optional_metadata_text(
            directory_fd,
            name,
            max_bytes=max_bytes,
        )
        if payload is None:
            raise RepoIntakeError("repository-intake Git metadata is unavailable")
        return payload

    @staticmethod
    def _normalized_metadata_path(value: str, *, relative_to: Path) -> Path:
        """Return an absolute lexical path from one Git metadata value."""
        raw = Path(value)
        candidate = raw if raw.is_absolute() else relative_to / raw
        return Path(os.path.abspath(candidate))

    @staticmethod
    def _metadata_line(payload: str) -> str:
        """Return the single metadata line, or raise an error if it is malformed."""
        lines = payload.splitlines()
        if len(lines) != 1 or not lines[0] or lines[0] != lines[0].strip():
            raise RepoIntakeError("repository-intake Git metadata is malformed")
        return lines[0]

    def _validate_local_git_config(self, payload: str) -> None:
        """Reject local Git configuration that can change trusted commands."""
        try:
            result = self._run_command(
                ["git", "config", "--file", "-", "--no-includes", "--null", "--list"],
                cwd=Path(self.common_dir.anchor),
                check=False,
                timeout=self.timeout_s,
                env=self._git_env,
                log_errors=False,
                input_text=payload,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RepoIntakeError("repository-intake Git configuration parser failed") from exc
        if result.returncode != 0 or not isinstance(result.stdout, str):
            raise RepoIntakeError("repository-intake Git configuration is malformed")
        if unsafe_local_git_config_key(result.stdout) is not None:
            raise RepoIntakeError("repository-intake Git configuration is unsafe")

    def _validate_intake_git_pointer(
        self, worktree_path: Path | None = None
    ) -> tuple[str, str | None]:
        """Verify the intake gitfile and capture its local configuration.

        The configuration is read without following symlinks.
        """
        worktree_path = worktree_path or self.worktree_path
        worktree_fd = common_fd = worktrees_fd = admin_fd = -1
        try:
            worktree_fd = os.open(worktree_path, self._directory_open_flags())
            gitfile = self._read_metadata_text(worktree_fd, ".git")
            gitfile_line = self._metadata_line(gitfile)
            if not gitfile_line.startswith("gitdir: "):
                raise RepoIntakeError("repository-intake Git pointer is malformed")
            gitdir_value = gitfile_line.removeprefix("gitdir: ")
            if not gitdir_value:
                raise RepoIntakeError("repository-intake Git pointer is malformed")
            admin_path = self._normalized_metadata_path(
                gitdir_value,
                relative_to=worktree_path,
            )
            worktrees_path = self.common_dir / "worktrees"
            if admin_path.parent != worktrees_path or admin_path.name in {"", ".", ".."}:
                raise RepoIntakeError(
                    "repository-intake Git pointer is outside the selected common directory"
                )

            common_fd = os.open(self.common_dir, self._directory_open_flags())
            worktrees_fd = os.open("worktrees", self._directory_open_flags(), dir_fd=common_fd)
            admin_fd = os.open(admin_path.name, self._directory_open_flags(), dir_fd=worktrees_fd)
            backlink = self._metadata_line(self._read_metadata_text(admin_fd, "gitdir"))
            commondir = self._metadata_line(self._read_metadata_text(admin_fd, "commondir"))
            backlink_path = self._normalized_metadata_path(backlink, relative_to=admin_path)
            if backlink_path != worktree_path / ".git":
                raise RepoIntakeError("repository-intake Git pointer is not bound to its worktree")
            common_path = self._normalized_metadata_path(commondir, relative_to=admin_path)
            try:
                resolved_common_path = common_path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise RepoIntakeError(
                    "repository-intake Git common-directory pointer is unavailable"
                ) from exc
            if resolved_common_path != self.common_dir:
                raise RepoIntakeError(
                    "repository-intake Git pointer is bound to a different common directory"
                )
            common_config = self._read_metadata_text(
                common_fd,
                "config",
                max_bytes=_GIT_CONFIG_TEXT_LIMIT,
            )
            worktree_config = self._read_optional_metadata_text(
                admin_fd,
                "config.worktree",
                max_bytes=_GIT_CONFIG_TEXT_LIMIT,
            )
            return common_config, worktree_config
        except RepoIntakeError:
            raise
        except OSError as exc:
            raise RepoIntakeError("repository-intake Git metadata is unavailable") from exc
        finally:
            for file_descriptor in (admin_fd, worktrees_fd, common_fd, worktree_fd):
                if file_descriptor >= 0:
                    os.close(file_descriptor)

    def _validate_intake_checkout(self, worktree_path: Path | None = None) -> None:
        """Verify the intake pointer and the exact captured local configs."""
        common_config, worktree_config = self._validate_intake_git_pointer(worktree_path)
        self._validate_local_git_config(common_config)
        if worktree_config is not None:
            self._validate_local_git_config(worktree_config)

    def _assert_no_registered_descendants(
        self,
        records: tuple[_WorktreeRecord, ...],
        root: Path | None = None,
    ) -> None:
        """Reject removal when a registered descendant worktree exists."""
        intake_path = Path(os.path.abspath(root or self.worktree_path))
        for record in records:
            registered_path = Path(os.path.abspath(record.path))
            if registered_path != intake_path and registered_path.is_relative_to(intake_path):
                raise RepoIntakeError(
                    "repository-intake has a registered descendant worktree; "
                    f"preserve and relocate or remove it before retry: {record.path}"
                )

    def _assert_clean_detached(
        self, record: _WorktreeRecord | None, checkout: Path | None = None
    ) -> None:
        """Require the owned intake worktree to be clean and detached."""
        checkout = checkout or self.worktree_path
        if record is None:
            raise RepoIntakeError("repository-intake worktree is not registered")
        status = self._run(
            ["git", "-c", "core.fsmonitor=false", "status", "--porcelain", "--untracked-files=all"],
            cwd=checkout,
        ).stdout.strip()
        if status:
            raise RepoIntakeError(f"repository-intake worktree is dirty and preserved: {checkout}")
        branch = self._run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=checkout,
            check=False,
        )
        if branch.returncode == 0 and branch.stdout.strip():
            raise RepoIntakeError(f"repository-intake worktree is attached: {checkout}")
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
        """Add a detached launch worktree at an exact full revision."""
        if not is_full_commit_sha(revision):
            raise RepoIntakeError("repository-intake worktree revision is malformed")
        self.launch_dir.mkdir(parents=True, exist_ok=True)
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

    def _remove_worktree(self, worktree: Path | None = None) -> None:
        """Remove only the clean, registered intake worktree."""
        target = worktree or self.worktree_path
        self._run(["git", "worktree", "remove", str(target)], cwd=self.caller_root)

    def _fetch(self, default_branch: str, *, checkout: Path) -> None:
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
        self._run(command, cwd=checkout)

    def _remote_head(self, default_branch: str, *, checkout: Path) -> str:
        """Read the fetched remote branch as a full commit SHA."""
        head = self._run(
            ["git", "rev-parse", "--verify", f"refs/remotes/origin/{default_branch}^{{commit}}"],
            cwd=checkout,
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


_LOGGER = logging.getLogger(__name__)


def _remove_tree_lexically(root: Path) -> None:
    """Remove one tree without following a symlink at any level.

    Every entry is removed by lstat-checked name, so a hostile symlink or
    rename swap cannot redirect the removal to foreign state.
    """
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        root.unlink(missing_ok=True)
        return
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        entries = list(os.scandir(current))
        child_directories: list[Path] = []
        for entry in entries:
            child = current / entry.name
            try:
                child_metadata = child.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(child_metadata.st_mode):
                child.unlink(missing_ok=True)
            elif stat.S_ISDIR(child_metadata.st_mode):
                child_directories.append(child)
            else:
                child.unlink(missing_ok=True)
        if child_directories:
            stack.append(current)
            stack.extend(child_directories)
            continue
        os.rmdir(current)


def _same_worktree_path(left: Path, right: Path) -> bool:
    """Compare two worktree paths without requiring both paths to exist."""
    try:
        return left.resolve() == right.resolve()
    except (OSError, RuntimeError):
        return left.absolute() == right.absolute()


def _records_for_path(
    records: tuple[_WorktreeRecord, ...], worktree: Path
) -> tuple[_WorktreeRecord, ...]:
    """Return every registration that names one worktree path."""
    return tuple(record for record in records if _same_worktree_path(record.path, worktree))


def _warn_preserved(reason: str, path: Path) -> None:
    """Report one preserved intake path with its recovery proof failure."""
    _LOGGER.warning(
        "repository-intake recovery preserved %s: %s; see "
        "docs/runbooks/corrupted-worktree.md for retained-path recovery",
        path,
        reason,
    )


def recover_abandoned_intakes(
    manager: RepoIntakeManager,
    records: tuple[_WorktreeRecord, ...],
) -> None:
    """Remove abandoned launch intakes that no live launch owns.

    This is the explicit SIGKILL and host-failure recovery path. Immediate
    cleanup is impossible after an abrupt stop, so a later launch proves for
    each candidate that no live launch owns it before removal. A held
    nonblocking owner lock is the liveness proof; a live launch and any
    dirty, damaged, or foreign state are always preserved with a warning.
    """
    launches_root = manager.state_dir / "launches"
    try:
        candidates = tuple(launches_root.iterdir())
    except FileNotFoundError:
        candidates = ()
    except OSError as exc:
        _warn_preserved(f"launch directory scan failed: {exc}", launches_root)
        return
    for candidate in candidates:
        if candidate.is_symlink() or not candidate.is_dir():
            _warn_preserved("not an ordinary launch directory", candidate)
            continue
        if not is_valid_launch_identity(candidate.name):
            _warn_preserved("launch identity is invalid", candidate)
            continue
        if candidate == manager.launch_dir:
            continue
        owner_lock_path = candidate / "owner.lock"
        try:
            proof = file_lock(owner_lock_path, blocking=False, require_exclusive=True)
            proof.__enter__()
        except (LockUnavailableError, OSError, RuntimeError):
            # A live launch owns this directory. Never remove it.
            continue
        try:
            _recover_one_abandoned_intake(manager, records, candidate)
        finally:
            proof.__exit__(None, None, None)
    _recover_legacy_intake(manager, records)


def _recover_one_abandoned_intake(
    manager: RepoIntakeManager,
    records: tuple[_WorktreeRecord, ...],
    candidate: Path,
) -> None:
    """Remove one lock-proved abandoned launch intake, or preserve it."""
    receipt_path = candidate / "receipt.json"
    try:
        receipt = _read_launch_receipt(receipt_path)
    except RepoIntakeError as exc:
        _warn_preserved(f"launch receipt is unreadable: {exc}", candidate)
        return
    worktree = candidate / "worktree"
    if receipt is None:
        if worktree.exists():
            _warn_preserved("abandoned launch has no receipt", candidate)
            return
        # An empty launch directory holds no work and no registration.
        _remove_tree_lexically(candidate)
        return
    if (
        receipt.repository != manager.repository
        or receipt.repository_identity != manager.repository_identity
        or receipt.common_dir != manager.common_dir
        or receipt.ownership_key != f"{manager.ownership_key}:{candidate.name}"
        or not _same_worktree_path(receipt.path, worktree)
        or receipt.state_root != candidate.resolve()
    ):
        _warn_preserved("abandoned launch receipt ownership does not match", candidate)
        return
    matches = _records_for_path(records, worktree)
    if len(matches) > 1:
        _warn_preserved("worktree registration is ambiguous", candidate)
        return
    try:
        if matches:
            record = matches[0]
            if record.branch is not None:
                _warn_preserved("abandoned intake worktree is branch-backed", candidate)
                return
            # The descriptor-based pointer and config proofs run first so no
            # Git command touches the damaged checkout before it is proven.
            manager._validate_intake_checkout(worktree)
            manager._assert_no_registered_descendants(records, root=worktree)
            manager._assert_clean_detached(record, checkout=worktree)
            if record.head != receipt.revision:
                _warn_preserved(
                    "abandoned intake worktree HEAD does not match its receipt", candidate
                )
                return
            manager._remove_worktree(worktree)
        elif worktree.exists():
            _warn_preserved("abandoned intake worktree is unregistered", candidate)
            return
        _remove_tree_lexically(candidate)
    except RepoIntakeError as exc:
        _warn_preserved(f"abandoned intake proof failed: {exc}", candidate)


def _read_launch_receipt(path: Path) -> RepoIntakeReceipt | None:
    """Read one launch receipt without ownership assumptions."""
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RepoIntakeError(f"cannot read repository-intake receipt: {path}") from exc
    if not isinstance(payload, dict):
        raise RepoIntakeError("repository-intake receipt must be an object")
    return RepoIntakeReceipt.from_dict(payload)


def _recover_legacy_intake(
    manager: RepoIntakeManager,
    records: tuple[_WorktreeRecord, ...],
) -> None:
    """Remove the legacy permanent intake once safe removal is established.

    The legacy intake at ``state_dir/worktree`` has no launch directory. Its
    run lock proves that no legacy run holds it, and the clean, detached,
    descendant-free proof proves that no unfinished work remains. Durable
    state below its ``build`` directory was already migrated by the state
    authority step before this sweep runs.
    """
    legacy_worktree = manager.state_dir / "worktree"
    if not legacy_worktree.exists() or legacy_worktree.is_symlink():
        return
    legacy_lock_path = manager.common_dir / "hephaestus-repository-intake.run.lock"
    try:
        proof = file_lock(legacy_lock_path, blocking=False, require_exclusive=True)
        proof.__enter__()
    except (LockUnavailableError, OSError, RuntimeError):
        return
    try:
        matches = _records_for_path(records, legacy_worktree)
        if len(matches) > 1:
            _warn_preserved("legacy intake registration is ambiguous", legacy_worktree)
            return
        if not matches:
            _warn_preserved("legacy intake worktree is unregistered", legacy_worktree)
            return
        record = matches[0]
        if record.branch is not None:
            _warn_preserved("legacy intake worktree is branch-backed", legacy_worktree)
            return
        # The descriptor-based pointer and config proofs run first so no Git
        # command touches the legacy checkout before it is proven.
        manager._validate_intake_checkout(legacy_worktree)
        manager._assert_no_registered_descendants(records, root=legacy_worktree)
        manager._assert_clean_detached(record, checkout=legacy_worktree)
        legacy_receipt_path = manager.state_dir / "receipt.json"
        if legacy_receipt_path.exists():
            payload = json.loads(legacy_receipt_path.read_text(encoding="utf-8"))
            if (
                is_full_commit_sha(payload.get("revision"))
                and payload.get("revision") != record.head
            ):
                _warn_preserved(
                    "legacy intake receipt does not match its worktree HEAD",
                    legacy_worktree,
                )
                return
        manager._remove_worktree(legacy_worktree)
        _LOGGER.warning("removed abandoned legacy repository intake %s", legacy_worktree)
    except (RepoIntakeError, OSError, ValueError) as exc:
        _warn_preserved(f"legacy intake proof failed: {exc}", legacy_worktree)
    finally:
        proof.__exit__(None, None, None)


__all__ = [
    "RepoIntakeError",
    "RepoIntakeInUseError",
    "RepoIntakeManager",
    "RepoIntakeReceipt",
    "is_full_commit_sha",
    "is_valid_launch_identity",
    "recover_abandoned_intakes",
]
