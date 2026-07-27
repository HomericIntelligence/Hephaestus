"""Durable receipts for detached-review checkouts preserved after remote drift."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.io.utils import write_secure
from hephaestus.utils.file_lock import file_lock

_RECEIPT_DIR = "direct-review-recovery"
_RECEIPT_VERSION = 3
_REMOTE_CHANGED_REASON = "remote_changed"
_WORKTREE_MARKER = "hephaestus-direct-review-recovery"
_DIR_FD_SUPPORTED = os.name == "posix" and hasattr(os, "O_DIRECTORY")
_INSPECTION_ONLY_FAILURES = frozenset(
    {
        "remote_changed",
        "remote_changed_unrecorded",
        "remote_unchanged",
        "remote_unconfirmed",
        "retry_checkout_changed",
        "retry_checkout_unconfirmed",
    }
)


def _is_full_sha(value: object) -> bool:
    """Return whether *value* is a full SHA-1 or SHA-256 commit identifier."""
    return (
        isinstance(value, str)
        and len(value) in (40, 64)
        and all(char in "0123456789abcdef" for char in value)
    )


def is_inspection_only_detached_push_failure(value: object) -> bool:
    """Return whether a failed detached publication needs manual inspection."""
    return isinstance(value, str) and value in _INSPECTION_ONLY_FAILURES


def _validated_worktree_path(repo_root: Path, issue: int, path: Path) -> Path:
    """Return a normalized isolated-review path or raise for an unsafe receipt."""
    if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0:
        raise ValueError("direct review recovery issue is invalid")
    root = (repo_root / "build" / ".worktrees").resolve()
    normalized = path.resolve()
    prefix = f"review-pr-{issue}"
    suffix = normalized.name.removeprefix(prefix)
    if (
        normalized.parent != root
        or not normalized.name.startswith(prefix)
        or (suffix and (not suffix.startswith("-") or not suffix[1:].isdigit()))
    ):
        raise ValueError("direct review recovery worktree is invalid")
    return normalized


def _receipt_dir(repo_root: Path, *, create: bool = False) -> Path:
    """Return a confined direct-review receipt directory.

    Receipt metadata participates in recovery decisions, so it must not escape
    the repository when a pre-existing state-directory component is a symlink.
    The caller that writes a receipt requests directory creation explicitly;
    read-only discovery leaves an absent state directory absent.
    """
    root = repo_root.resolve()
    receipt_dir = root / DEFAULT_STATE_DIR / _RECEIPT_DIR
    resolved_receipt_dir = receipt_dir.resolve()
    if root not in resolved_receipt_dir.parents:
        raise ValueError("direct review recovery receipt directory is outside the repository")
    component = root
    for part in (*Path(DEFAULT_STATE_DIR).parts, _RECEIPT_DIR):
        component /= part
        if component.is_symlink():
            raise ValueError("direct review recovery receipt directory is symlinked")
        if component.exists() and not component.is_dir():
            raise ValueError("direct review recovery receipt directory is not a directory")
    if create:
        receipt_dir.mkdir(parents=True, exist_ok=True)
        # Re-check after mkdir: a concurrent replacement must not redirect the
        # lock or atomic receipt write outside this repository.
        resolved_receipt_dir = receipt_dir.resolve()
        if root not in resolved_receipt_dir.parents or receipt_dir.is_symlink():
            raise ValueError("direct review recovery receipt directory is unsafe")
    return receipt_dir


def _receipt_path(receipt_dir: Path, pr: int, worktree: Path) -> Path:
    """Return a stable path for one PR/worktree recovery receipt."""
    digest = hashlib.sha256(str(worktree).encode("utf-8")).hexdigest()
    return receipt_dir / f"direct-review-{pr}-{digest}.json"


def _receipt_lock_path(receipt_dir: Path, pr: int) -> Path:
    """Return the per-PR lock serializing receipt writes and reads."""
    return receipt_dir / f"direct-review-{pr}.lock"


def _directory_open_flags() -> int:
    """Return flags for a directory descriptor that refuses final symlinks."""
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _open_directory_component(parent_fd: int, name: str, *, create: bool) -> int | None:
    """Open one trusted directory component relative to ``parent_fd``."""
    flags = _directory_open_flags()
    try:
        child_fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            return None
        # Another loop process may have created it between open and mkdir.
        with suppress(FileExistsError):
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        try:
            child_fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ValueError("direct review recovery receipt directory is unsafe") from error
            raise
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError("direct review recovery receipt directory is unsafe") from error
        raise
    try:
        if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
            raise ValueError("direct review recovery receipt directory is not a directory")
    except BaseException:
        os.close(child_fd)
        raise
    return child_fd


@contextmanager
def _open_receipt_directory(
    repo_root: Path, *, create: bool = False
) -> Iterator[tuple[Path, int | None]]:
    """Yield a receipt path and a no-follow descriptor when the platform supports it.

    The descriptor-relative operations that follow cannot be redirected by a
    concurrent replacement of the automation state directory. On platforms
    without directory-descriptor support, retain the existing portable path
    validation and locking behavior.
    """
    normalized_root = Path(repo_root).resolve()
    receipt_path = normalized_root / DEFAULT_STATE_DIR / _RECEIPT_DIR
    if not _DIR_FD_SUPPORTED:
        yield _receipt_dir(normalized_root, create=create), None
        return

    root_fd = os.open(normalized_root, _directory_open_flags())
    current_fd = root_fd
    try:
        for component in (*Path(DEFAULT_STATE_DIR).parts, _RECEIPT_DIR):
            child_fd = _open_directory_component(current_fd, component, create=create)
            if child_fd is None:
                yield receipt_path, None
                return
            os.close(current_fd)
            current_fd = child_fd
        yield receipt_path, current_fd
    finally:
        os.close(current_fd)


@contextmanager
def _receipt_lock(receipt_dir: Path, receipt_fd: int | None, pr: int) -> Iterator[None]:
    """Serialize receipt access without following a replaced receipt parent."""
    if receipt_fd is None:
        with file_lock(_receipt_lock_path(receipt_dir, pr)):
            yield
        return

    name = _receipt_lock_path(receipt_dir, pr).name
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(name, flags, 0o600, dir_fd=receipt_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("direct review recovery receipt lock is not a regular file")
        os.fchmod(fd, 0o600)
        try:
            import fcntl
        except ImportError:  # pragma: no cover - guarded by _DIR_FD_SUPPORTED on CI
            yield
            return
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_receipt(receipt_dir: Path, receipt_fd: int | None, target: Path, content: str) -> None:
    """Atomically write a receipt without resolving its parent again."""
    if receipt_fd is None:
        write_secure(target, content)
        return

    target_name = target.name
    temporary_name = f".{target_name}.{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary_name, flags, 0o600, dir_fd=receipt_fd)
    try:
        os.fchmod(fd, 0o600)
        data = content.encode("utf-8")
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary_name,
            target_name,
            src_dir_fd=receipt_fd,
            dst_dir_fd=receipt_fd,
        )
        os.fsync(receipt_fd)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=receipt_fd)
        raise


def _read_receipt(receipt_dir: Path, receipt_fd: int | None, name: str) -> Any:
    """Read one regular receipt from a trusted descriptor or portable path."""
    if receipt_fd is None:
        return json.loads((receipt_dir / name).read_text(encoding="utf-8"))
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(name, flags, dir_fd=receipt_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("direct review recovery receipt is not a regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return json.load(handle)
    finally:
        if fd >= 0:
            os.close(fd)


def _read_secure_text(directory: Path, directory_fd: int | None, name: str) -> str:
    """Read a regular no-follow text file from a trusted directory."""
    if directory_fd is None:
        return (directory / name).read_text(encoding="utf-8")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("direct review recovery marker is not a regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return handle.read()
    finally:
        if fd >= 0:
            os.close(fd)


def _receipt_names(receipt_dir: Path, receipt_fd: int | None, pr: int) -> list[str]:
    """Return receipt basenames for a PR without traversing a mutable parent."""
    prefix = f"direct-review-{pr}-"
    if receipt_fd is None:
        return [path.name for path in receipt_dir.glob(f"{prefix}*.json")]
    return sorted(
        name
        for name in os.listdir(receipt_fd)
        if name.startswith(prefix) and name.endswith(".json")
    )


@contextmanager
def _open_marker_directory(repo_root: Path, marker_path: Path) -> Iterator[tuple[Path, int | None]]:
    """Yield the marker parent through a no-follow descriptor when available."""
    marker_dir = marker_path.parent
    if not _DIR_FD_SUPPORTED:
        yield marker_dir, None
        return
    root = repo_root.resolve()
    try:
        components = marker_dir.relative_to(root).parts
    except ValueError as error:
        raise ValueError("direct review recovery marker is outside the repository") from error
    root_fd = os.open(root, _directory_open_flags())
    current_fd = root_fd
    try:
        for component in components:
            child_fd = _open_directory_component(current_fd, component, create=False)
            if child_fd is None:
                raise ValueError("direct review recovery Git metadata is unavailable")
            os.close(current_fd)
            current_fd = child_fd
        yield marker_dir, current_fd
    finally:
        os.close(current_fd)


def _receipt_directory_is_current(repo_root: Path, receipt_fd: int | None) -> bool:
    """Return whether the receipt descriptor still names the live state directory."""
    if receipt_fd is None:
        return True
    try:
        with _open_receipt_directory(repo_root) as (_, current_fd):
            return current_fd is not None and os.path.samestat(
                os.fstat(receipt_fd), os.fstat(current_fd)
            )
    except (OSError, ValueError):
        return False


def _worktree_identity(worktree: Path) -> tuple[int, int]:
    """Return the filesystem identity that binds a receipt to one checkout."""
    stat = worktree.stat()
    return stat.st_dev, stat.st_ino


def _worktree_marker_path(repo_root: Path, worktree: Path) -> Path:
    """Return the private Git-metadata marker for one direct-review checkout."""
    dot_git = worktree / ".git"
    if dot_git.is_symlink():
        raise ValueError("direct review recovery Git metadata is symlinked")
    if dot_git.is_dir():
        git_dir = dot_git
    else:
        try:
            line = dot_git.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ValueError("direct review recovery Git metadata is unavailable") from error
        prefix = "gitdir: "
        if not line.startswith(prefix):
            raise ValueError("direct review recovery Git metadata is invalid")
        raw_git_dir = Path(line.removeprefix(prefix))
        git_dir = raw_git_dir if raw_git_dir.is_absolute() else (worktree / raw_git_dir)
    git_dir = git_dir.resolve()
    if not git_dir.is_dir():
        raise ValueError("direct review recovery Git metadata is unavailable")
    normalized_root = repo_root.resolve()
    try:
        git_dir.relative_to(normalized_root)
    except ValueError as error:
        raise ValueError("direct review recovery Git metadata is outside the repository") from error
    if not dot_git.is_dir():
        common_git_dir = normalized_root / ".git" / "worktrees"
        try:
            git_dir.relative_to(common_git_dir.resolve())
        except ValueError as error:
            raise ValueError(
                "direct review recovery Git metadata is not a linked worktree"
            ) from error
    return git_dir / _WORKTREE_MARKER


def _write_worktree_marker(repo_root: Path, worktree: Path) -> str:
    """Create an unguessable marker that cannot survive checkout replacement."""
    marker = secrets.token_urlsafe(32)
    marker_path = _worktree_marker_path(repo_root, worktree)
    with _open_marker_directory(repo_root, marker_path) as (marker_dir, marker_fd):
        _write_receipt(marker_dir, marker_fd, marker_path, marker + "\n")
    return marker


def _read_worktree_marker(repo_root: Path, worktree: Path) -> str:
    """Read the checkout-local marker or raise when it is absent or unsafe."""
    marker_path = _worktree_marker_path(repo_root, worktree)
    with _open_marker_directory(repo_root, marker_path) as (marker_dir, marker_fd):
        marker = _read_secure_text(marker_dir, marker_fd, marker_path.name).strip()
    if not marker:
        raise ValueError("direct review recovery worktree marker is invalid")
    return marker


def record_direct_review_recovery(
    *,
    repo_root: Path,
    issue: int,
    pr: int,
    worktree: Path,
    branch: str,
    expected_remote_sha: str,
    source_sha: str,
) -> Path:
    """Persist an immutable receipt for a verified detached-push remote drift.

    A receipt is written only after the remote has authoritatively changed, so
    later runs can distinguish an abandoned recovery checkout from a merely
    occupied (and potentially active) checkout.
    """
    if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
        raise ValueError("direct review recovery PR is invalid")
    if not isinstance(branch, str) or not branch:
        raise ValueError("direct review recovery branch is invalid")
    if not _is_full_sha(expected_remote_sha) or not _is_full_sha(source_sha):
        raise ValueError("direct review recovery commit receipt is invalid")
    normalized_root = Path(repo_root).resolve()
    normalized_worktree = _validated_worktree_path(normalized_root, issue, worktree)
    if not normalized_worktree.is_dir():
        raise ValueError("direct review recovery worktree is missing")
    worktree_device, worktree_inode = _worktree_identity(normalized_worktree)
    worktree_marker = _write_worktree_marker(normalized_root, normalized_worktree)
    receipt = {
        "branch": branch,
        "expected_remote_sha": expected_remote_sha,
        "issue": issue,
        "pr": pr,
        "reason": _REMOTE_CHANGED_REASON,
        "repo_root": str(normalized_root),
        "schema_version": _RECEIPT_VERSION,
        "source_sha": source_sha,
        "worktree": str(normalized_worktree),
        "worktree_device": worktree_device,
        "worktree_inode": worktree_inode,
        "worktree_marker": worktree_marker,
    }
    with _open_receipt_directory(normalized_root, create=True) as (receipt_dir, receipt_fd):
        if receipt_fd is None and _DIR_FD_SUPPORTED:  # pragma: no cover - create=True guarantees it
            raise ValueError("direct review recovery receipt directory is unavailable")
        target = _receipt_path(receipt_dir, pr, normalized_worktree)
        with _receipt_lock(receipt_dir, receipt_fd, pr):
            _write_receipt(
                receipt_dir,
                receipt_fd,
                target,
                json.dumps(receipt, sort_keys=True) + "\n",
            )
            if not _receipt_directory_is_current(normalized_root, receipt_fd):
                raise ValueError("direct review recovery receipt directory changed during write")
        return target


def list_direct_review_recovery_paths(*, repo_root: Path, issue: int, pr: int) -> list[Path]:
    """Return only valid, receipt-backed detached-review recovery paths.

    Invalid or tampered receipts are ignored. They are not evidence that an
    arbitrary occupied checkout is safe to surface as a recovery artifact.
    """
    if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
        return []
    normalized_root = Path(repo_root).resolve()
    try:
        receipt_directory = _open_receipt_directory(normalized_root)
    except ValueError:
        return []
    paths: list[Path] = []
    seen: set[Path] = set()
    try:
        with receipt_directory as (receipt_dir, receipt_fd):
            if receipt_fd is None and (_DIR_FD_SUPPORTED or not receipt_dir.is_dir()):
                return []
            with _receipt_lock(receipt_dir, receipt_fd, pr):
                for receipt_name in _receipt_names(receipt_dir, receipt_fd, pr):
                    try:
                        payload: Any = _read_receipt(receipt_dir, receipt_fd, receipt_name)
                        if not isinstance(payload, dict):
                            continue
                        worktree = _validated_worktree_path(
                            normalized_root, issue, Path(str(payload.get("worktree", "")))
                        )
                        worktree_device, worktree_inode = _worktree_identity(worktree)
                        worktree_marker = _read_worktree_marker(normalized_root, worktree)
                        recorded_marker = payload.get("worktree_marker")
                        if (
                            payload.get("schema_version") != _RECEIPT_VERSION
                            or payload.get("reason") != _REMOTE_CHANGED_REASON
                            or payload.get("repo_root") != str(normalized_root)
                            or payload.get("issue") != issue
                            or payload.get("pr") != pr
                            or not isinstance(payload.get("branch"), str)
                            or not _is_full_sha(payload.get("expected_remote_sha"))
                            or not _is_full_sha(payload.get("source_sha"))
                            or not worktree.is_dir()
                            or isinstance(payload.get("worktree_device"), bool)
                            or not isinstance(payload.get("worktree_device"), int)
                            or payload.get("worktree_device") != worktree_device
                            or isinstance(payload.get("worktree_inode"), bool)
                            or not isinstance(payload.get("worktree_inode"), int)
                            or payload.get("worktree_inode") != worktree_inode
                            or not isinstance(recorded_marker, str)
                            or not secrets.compare_digest(recorded_marker, worktree_marker)
                            or worktree in seen
                        ):
                            continue
                    except (OSError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                    seen.add(worktree)
                    paths.append(worktree)
    except (OSError, ValueError):
        return []
    return sorted(paths)
