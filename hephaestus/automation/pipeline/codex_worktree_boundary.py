"""Hold the Git control boundary for one Codex implementation job."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from hephaestus.agents.codex_isolation import CodexGitReceiptV1


class CodexWorktreeBoundaryError(RuntimeError):
    """Report a fail-closed Git control boundary error."""


@dataclass(frozen=True, slots=True)
class GitControlIdentity:
    """Record the identity and optional content digest of one Git object."""

    path: str
    device: int
    inode: int
    mode: int
    owner: int
    size: int
    modified_ns: int
    changed_ns: int
    sha256: str | None


def _sha256_from_fd(descriptor: int) -> str:
    """Hash one regular file through its held descriptor."""
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _read_from_fd(descriptor: int, *, maximum: int = 1024 * 1024) -> bytes:
    """Read one bounded regular file through its held descriptor."""
    chunks: list[bytes] = []
    offset = 0
    while offset <= maximum:
        chunk = os.pread(descriptor, min(65536, maximum + 1 - offset), offset)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        offset += len(chunk)
    raise CodexWorktreeBoundaryError("Git control file is too large")


def _absolute_control_path(path: Path) -> Path:
    """Return one normalized absolute control path without resolving links."""
    return Path(os.path.abspath(path))


def _open_no_follow_path(path: Path, *, directory: bool) -> int:
    """Open one control path through no-follow directory descriptors."""
    if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
        raise OSError("Git control path is not normalized")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path.anchor, directory_flags)
    try:
        for offset, part in enumerate(path.parts[1:], start=1):
            final = offset == len(path.parts) - 1
            flags = (
                directory_flags
                if not final or directory
                else os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            opened = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = opened
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_control(path: Path, *, directory: bool) -> tuple[int, GitControlIdentity]:
    """Open and identify one no-follow Git control path."""
    try:
        descriptor = _open_no_follow_path(path, directory=directory)
    except OSError as exc:
        raise CodexWorktreeBoundaryError("Git control path is unavailable") from exc
    try:
        value = os.fstat(descriptor)
        expected = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
        if not expected:
            raise CodexWorktreeBoundaryError("Git control path has an invalid type")
        digest = None if directory else _sha256_from_fd(descriptor)
        identity = GitControlIdentity(
            path=str(path),
            device=value.st_dev,
            inode=value.st_ino,
            mode=value.st_mode,
            owner=value.st_uid,
            size=value.st_size,
            modified_ns=value.st_mtime_ns,
            changed_ns=value.st_ctime_ns,
            sha256=digest,
        )
        return descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def _decode_control(descriptor: int, name: str) -> str:
    """Decode one bounded Git control file."""
    try:
        return _read_from_fd(descriptor).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CodexWorktreeBoundaryError(f"{name} must be UTF-8") from exc


def _parse_git_pointer(text: str, worktree: Path) -> Path:
    """Resolve one exact linked-worktree Git pointer."""
    lines = text.splitlines()
    if len(lines) != 1 or not lines[0].startswith("gitdir: "):
        raise CodexWorktreeBoundaryError("Git pointer is invalid")
    value = lines[0].removeprefix("gitdir: ")
    if not value or "\x00" in value:
        raise CodexWorktreeBoundaryError("Git pointer is invalid")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = worktree / candidate
    return _absolute_control_path(candidate)


def _parse_common_dir(text: str, git_dir: Path) -> Path:
    """Resolve the common Git directory from its exact control file."""
    value = text.strip()
    if not value or "\x00" in value or len(text.splitlines()) != 1:
        raise CodexWorktreeBoundaryError("Git common-directory record is invalid")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = git_dir / candidate
    return _absolute_control_path(candidate)


def _reject_unsafe_config(text: str) -> None:
    """Reject includes, hooks, and file-system monitors in a bound snapshot."""
    section = ""
    for original in text.splitlines():
        line = original.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            if not line.endswith("]"):
                raise CodexWorktreeBoundaryError("Git configuration is invalid")
            section = line[1:-1].strip().split(maxsplit=1)[0].casefold()
            if section in {"include", "includeif"}:
                raise CodexWorktreeBoundaryError("unsafe Git configuration")
            continue
        key = line.split("=", 1)[0].strip().casefold()
        if not section or not key:
            raise CodexWorktreeBoundaryError("Git configuration is invalid")
        if section == "core" and key in {"hookspath", "fsmonitor"}:
            raise CodexWorktreeBoundaryError("unsafe Git configuration")


class CodexWorktreeBoundary:
    """Hold descriptors and validate Git path and content identity."""

    def __init__(
        self,
        *,
        receipt: CodexGitReceiptV1,
        descriptors: tuple[int, ...],
        held_identities: tuple[GitControlIdentity, ...],
    ) -> None:
        """Initialize one open boundary from held control descriptors."""
        self.receipt = receipt
        self._descriptors = descriptors
        self._held_identities = held_identities
        self._closed = False

    def __enter__(self) -> CodexWorktreeBoundary:
        """Return this held boundary."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close all held descriptors."""
        self.close()

    def close(self) -> None:
        """Close all held descriptors once."""
        if self._closed:
            return
        self._closed = True
        for descriptor in reversed(self._descriptors):
            os.close(descriptor)

    def _verify_identities(self, *, content: bool) -> None:
        if self._closed:
            raise CodexWorktreeBoundaryError("Git control boundary is closed")
        for descriptor, expected in zip(
            self._descriptors,
            self._held_identities,
            strict=True,
        ):
            try:
                held = os.fstat(descriptor)
                current_descriptor = _open_no_follow_path(
                    Path(expected.path),
                    directory=stat.S_ISDIR(expected.mode),
                )
            except OSError as exc:
                raise CodexWorktreeBoundaryError("Git control identity changed") from exc
            try:
                current = os.fstat(current_descriptor)
            finally:
                os.close(current_descriptor)
            identity_fields = (
                held.st_dev,
                held.st_ino,
                held.st_mode,
                held.st_uid,
                held.st_size,
                held.st_mtime_ns,
                held.st_ctime_ns,
            )
            expected_fields = (
                expected.device,
                expected.inode,
                expected.mode,
                expected.owner,
                expected.size,
                expected.modified_ns,
                expected.changed_ns,
            )
            path_fields = (
                current.st_dev,
                current.st_ino,
                current.st_mode,
                current.st_uid,
                current.st_size,
                current.st_mtime_ns,
                current.st_ctime_ns,
            )
            if (
                content
                and expected.sha256 is not None
                and _sha256_from_fd(descriptor) != expected.sha256
            ):
                raise CodexWorktreeBoundaryError("Git control content changed")
            if identity_fields != expected_fields or path_fields != expected_fields:
                raise CodexWorktreeBoundaryError("Git control identity changed")

    def verify_before_launch(self) -> None:
        """Verify held descriptor and path identity before adapter launch."""
        self._verify_identities(content=False)

    def verify_after_return(self) -> None:
        """Verify held identities and file content after adapter return."""
        self._verify_identities(content=True)


def capture_codex_worktree_boundary(worktree: Path) -> CodexWorktreeBoundary:
    """Capture one linked worktree and retain all Git control descriptors."""
    canonical_worktree = _absolute_control_path(worktree)
    try:
        worktree_descriptor = _open_no_follow_path(canonical_worktree, directory=True)
    except OSError as exc:
        raise CodexWorktreeBoundaryError("Codex worktree is unavailable") from exc
    os.close(worktree_descriptor)

    descriptors: list[int] = []
    identities: list[GitControlIdentity] = []

    def hold(path: Path, *, directory: bool = False) -> int:
        descriptor, identity = _open_control(path, directory=directory)
        descriptors.append(descriptor)
        identities.append(identity)
        return descriptor

    try:
        git_pointer = canonical_worktree / ".git"
        pointer_fd = hold(git_pointer)
        git_dir = _parse_git_pointer(_decode_control(pointer_fd, "Git pointer"), canonical_worktree)
        hold(git_dir, directory=True)

        common_record = git_dir / "commondir"
        common_fd = hold(common_record)
        common_dir = _parse_common_dir(
            _decode_control(common_fd, "Git common-directory record"),
            git_dir,
        )
        if git_dir.parent.name != "worktrees" or git_dir.parent.parent != common_dir:
            raise CodexWorktreeBoundaryError("Git control path escapes linked-worktree metadata")
        hold(common_dir, directory=True)

        index = git_dir / "index"
        hold(index)
        repository_config = common_dir / "config"
        repository_config_fd = hold(repository_config)
        worktree_config = git_dir / "config.worktree"
        worktree_config_fd = hold(worktree_config)
        _reject_unsafe_config(_decode_control(repository_config_fd, "repository config"))
        _reject_unsafe_config(_decode_control(worktree_config_fd, "worktree config"))

        fixed_environment = tuple(
            sorted(
                {
                    "GIT_ATTR_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_DIR": str(git_dir),
                    "GIT_INDEX_FILE": str(index),
                    "GIT_NO_REPLACE_OBJECTS": "1",
                    "GIT_OPTIONAL_LOCKS": "0",
                    "GIT_WORK_TREE": str(canonical_worktree),
                }.items()
            )
        )
        control_names = {
            str(git_pointer): "git_pointer",
            str(git_dir): "git_dir",
            str(common_record): "common_record",
            str(common_dir): "common_dir",
            str(index): "index",
            str(repository_config): "repository_config",
            str(worktree_config): "worktree_config",
        }
        named_identities = tuple(
            sorted(
                (
                    (
                        control_names[identity.path],
                        (
                            identity.device,
                            identity.inode,
                            identity.mode,
                            identity.owner,
                            identity.size,
                            identity.modified_ns,
                        ),
                    )
                    for identity in identities
                ),
                key=lambda item: item[0],
            )
        )
        named_digests = tuple(
            sorted(
                (
                    (control_names[identity.path], identity.sha256)
                    for identity in identities
                    if identity.sha256 is not None
                ),
                key=lambda item: item[0],
            )
        )
        receipt = CodexGitReceiptV1(
            schema_version=1,
            canonical_worktree=str(canonical_worktree),
            git_dir=str(git_dir),
            common_dir=str(common_dir),
            index=str(index),
            repository_config=str(repository_config),
            worktree_config=str(worktree_config),
            fixed_environment=fixed_environment,
            protected_paths=(str(git_pointer),),
            read_only_paths=tuple(
                dict.fromkeys(
                    (
                        str(git_dir),
                        str(common_dir),
                        str(index),
                        str(repository_config),
                        str(worktree_config),
                    )
                )
            ),
            read_write_paths=(str(canonical_worktree),),
            identities=named_identities,
            digests=named_digests,
        )
        return CodexWorktreeBoundary(
            receipt=receipt,
            descriptors=tuple(descriptors),
            held_identities=tuple(identities),
        )
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


__all__ = [
    "CodexWorktreeBoundary",
    "CodexWorktreeBoundaryError",
    "GitControlIdentity",
    "capture_codex_worktree_boundary",
]
