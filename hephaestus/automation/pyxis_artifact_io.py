"""Closed host I/O helpers for Linux Pyxis artifact verification."""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_AUTHORITY_MAX_BYTES = 64 * 1024


class PyxisArtifactIOError(OSError):
    """Raised when a Pyxis artifact does not satisfy the host I/O contract."""


@dataclass
class _BoundPathEntry:
    """Descriptor-bound identity for one direct child of a shared root."""

    name: str
    descriptor: int
    kind: Literal["directory", "regular"]
    identity: tuple[int, ...]
    expected_sha256: str | None = None
    require_read_only: bool = False


@dataclass
class CrossNodePathBinding:
    """Hold and revalidate path identities used by a cross-node launch."""

    path: Path
    _root_descriptor: int
    _root_identity: tuple[int, ...]
    _filesystem_identity: tuple[int, ...]
    _original_mode: int
    _entries: list[_BoundPathEntry] = field(default_factory=list)
    _closed: bool = False

    def __enter__(self) -> CrossNodePathBinding:
        """Return this live binding."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Close all held descriptors."""
        self.close()

    @property
    def capacity_bytes(self) -> int:
        """Return total bytes from the held filesystem descriptor."""
        metadata = os.fstatvfs(self._root_descriptor)
        return metadata.f_frsize * metadata.f_blocks

    def bind_path(
        self,
        path: Path,
        *,
        kind: Literal["directory", "regular"],
        expected_sha256: str | None = None,
        require_read_only: bool = False,
    ) -> None:
        """Bind one direct child path to its current descriptor identity."""
        self.revalidate_root()
        candidate = path.expanduser().absolute()
        if candidate.parent != self.path or candidate.name in {"", ".", ".."}:
            raise PyxisArtifactIOError("cross-node path is outside its bound root")
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if kind == "directory":
            flags |= os.O_DIRECTORY
        descriptor = -1
        try:
            descriptor = os.open(candidate.name, flags, dir_fd=self._root_descriptor)
            metadata = os.fstat(descriptor)
            _require_bound_kind(metadata, kind, require_read_only=require_read_only)
            if expected_sha256 is not None:
                _require_descriptor_digest(descriptor, expected_sha256)
            self._entries.append(
                _BoundPathEntry(
                    name=candidate.name,
                    descriptor=descriptor,
                    kind=kind,
                    identity=_path_identity(metadata, kind),
                    expected_sha256=expected_sha256,
                    require_read_only=require_read_only,
                )
            )
            descriptor = -1
            self._root_identity = _path_identity(os.fstat(self._root_descriptor), "directory")
        except OSError as exc:
            if isinstance(exc, PyxisArtifactIOError):
                raise
            raise PyxisArtifactIOError("cross-node path binding failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def seal_root(self) -> None:
        """Remove write bits from the bound shared root until close."""
        try:
            current = os.fstat(self._root_descriptor)
            os.fchmod(self._root_descriptor, stat.S_IMODE(current.st_mode) & ~0o222)
            self._root_identity = _path_identity(os.fstat(self._root_descriptor), "directory")
        except OSError as exc:
            raise PyxisArtifactIOError("cross-node root could not be sealed") from exc

    def revalidate_root(self) -> None:
        """Require the held root and its visible name to keep one identity."""
        if self._closed:
            raise PyxisArtifactIOError("cross-node path binding is closed")
        try:
            _require_trusted_ancestry(self.path)
            held = os.fstat(self._root_descriptor)
            if _path_identity(held, "directory") != self._root_identity:
                raise PyxisArtifactIOError("cross-node root identity changed")
            if (
                _filesystem_identity(os.fstatvfs(self._root_descriptor))
                != self._filesystem_identity
            ):
                raise PyxisArtifactIOError("cross-node root filesystem changed")
            visible_descriptor = os.open(
                self.path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                if _path_identity(os.fstat(visible_descriptor), "directory") != self._root_identity:
                    raise PyxisArtifactIOError("cross-node root path changed")
                if (
                    _filesystem_identity(os.fstatvfs(visible_descriptor))
                    != self._filesystem_identity
                ):
                    raise PyxisArtifactIOError("cross-node root filesystem changed")
            finally:
                os.close(visible_descriptor)
        except OSError as exc:
            if isinstance(exc, PyxisArtifactIOError):
                raise
            raise PyxisArtifactIOError("cross-node root revalidation failed") from exc

    def revalidate(self) -> None:
        """Require all bound names to match their held descriptors."""
        self.revalidate_root()
        for entry in self._entries:
            descriptor = -1
            try:
                held = os.fstat(entry.descriptor)
                if _path_identity(held, entry.kind) != entry.identity:
                    raise PyxisArtifactIOError("cross-node artifact identity changed")
                flags = os.O_RDONLY | os.O_NOFOLLOW
                if entry.kind == "directory":
                    flags |= os.O_DIRECTORY
                descriptor = os.open(
                    entry.name,
                    flags,
                    dir_fd=self._root_descriptor,
                )
                current = os.fstat(descriptor)
                _require_bound_kind(
                    current,
                    entry.kind,
                    require_read_only=entry.require_read_only,
                )
                if _path_identity(current, entry.kind) != entry.identity:
                    raise PyxisArtifactIOError("cross-node artifact path changed")
                if entry.expected_sha256 is not None:
                    _require_descriptor_digest(descriptor, entry.expected_sha256)
            except OSError as exc:
                if isinstance(exc, PyxisArtifactIOError):
                    raise
                raise PyxisArtifactIOError("cross-node artifact revalidation failed") from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

    def close(self) -> None:
        """Restore the root mode and close each descriptor once."""
        if self._closed:
            return
        self._closed = True
        for entry in self._entries:
            with suppress(OSError):
                os.close(entry.descriptor)
        with suppress(OSError):
            os.fchmod(self._root_descriptor, self._original_mode)
        with suppress(OSError):
            os.close(self._root_descriptor)


def effective_user_id() -> int:
    """Return the effective host user ID."""
    return os.geteuid()


def read_private_regular_file(path: Path) -> bytes:
    """Read a bounded owner-only regular file without following a symlink."""
    descriptor = -1
    try:
        descriptor = os.open(path.expanduser().absolute(), os.O_RDONLY | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        _require_private_regular(before)
        chunks: list[bytes] = []
        remaining = _AUTHORITY_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after) or len(value) > _AUTHORITY_MAX_BYTES:
            raise PyxisArtifactIOError("private artifact changed or exceeded its bound")
        return value
    except OSError as exc:
        if isinstance(exc, PyxisArtifactIOError):
            raise
        raise PyxisArtifactIOError("private artifact read failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def validate_private_squashfs_file(path: Path) -> tuple[Path, str]:
    """Return one private squashfs path and its descriptor-bound digest."""
    candidate = path.expanduser().absolute()
    descriptor = -1
    try:
        descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        _require_private_regular(before)
        digest = hashlib.sha256()
        magic = os.read(descriptor, 4)
        digest.update(magic)
        if magic != b"hsqs":
            raise PyxisArtifactIOError("artifact is not a squashfs file")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        resolved = candidate.resolve(strict=True)
        resolved_metadata = resolved.stat()
        if _identity(before) != _identity(after) or (
            resolved_metadata.st_dev,
            resolved_metadata.st_ino,
        ) != (before.st_dev, before.st_ino):
            raise PyxisArtifactIOError("artifact changed during validation")
        return resolved, digest.hexdigest()
    except OSError as exc:
        if isinstance(exc, PyxisArtifactIOError):
            raise
        raise PyxisArtifactIOError("private squashfs validation failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def stage_private_content_addressed_file(
    source: Path,
    destination_root: Path,
    expected_sha256: str,
    *,
    retain_binding: bool = False,
) -> Path | CrossNodePathBinding:
    """Copy exact owner-read-only bytes to a private digest-named path."""
    root = destination_root.expanduser().absolute()
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise PyxisArtifactIOError("content digest is invalid")
    target_name = f"sha256-{expected_sha256}.sqsh"
    target = root / target_name
    root_descriptor = -1
    source_descriptor = -1
    target_descriptor = -1
    created_target = False
    try:
        root_descriptor = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        root_metadata = os.fstat(root_descriptor)
        _require_private_directory(root_metadata)
        source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        before = os.fstat(source_descriptor)
        _require_private_regular(before)
        target_descriptor = _open_new_digest_target(
            root_descriptor,
            target_name,
            expected_sha256,
        )
        if target_descriptor >= 0:
            created_target = True
            _copy_exact_content(
                source_descriptor,
                target_descriptor,
                before,
                expected_sha256,
            )
            _require_private_regular(os.fstat(target_descriptor))
        _require_same_directory_path(root, root_metadata)
        if retain_binding:
            retained_descriptor = os.dup(root_descriptor)
            try:
                binding = _binding_from_descriptor(root, retained_descriptor)
                retained_descriptor = -1
                binding.bind_path(
                    target,
                    kind="regular",
                    expected_sha256=expected_sha256,
                    require_read_only=True,
                )
            except BaseException:
                if retained_descriptor >= 0:
                    os.close(retained_descriptor)
                else:
                    binding.close()
                raise
            return binding
        return target
    except OSError as exc:
        if created_target and root_descriptor >= 0:
            with suppress(OSError):
                os.unlink(target_name, dir_fd=root_descriptor)
        if isinstance(exc, PyxisArtifactIOError):
            raise
        raise PyxisArtifactIOError("content-addressed staging failed") from exc
    finally:
        if target_descriptor >= 0:
            os.close(target_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if root_descriptor >= 0:
            os.close(root_descriptor)


def validate_private_capacity_root(
    root: Path, *, retain_binding: bool = False
) -> tuple[Path | CrossNodePathBinding, int]:
    """Return one private directory and its filesystem capacity."""
    binding: CrossNodePathBinding | None = None
    try:
        binding = bind_cross_node_root(root)
        total_bytes = binding.capacity_bytes
    except OSError as exc:
        if binding is not None:
            binding.close()
        raise PyxisArtifactIOError("capacity root is unavailable") from exc
    if retain_binding:
        return binding, total_bytes
    path = binding.path
    binding.close()
    return path, total_bytes


def _copy_exact_content(
    source_descriptor: int,
    target_descriptor: int,
    before: os.stat_result,
    expected_sha256: str,
) -> None:
    """Copy one descriptor and require stable source bytes."""
    digest = hashlib.sha256()
    while chunk := os.read(source_descriptor, 1024 * 1024):
        digest.update(chunk)
        view = memoryview(chunk)
        while view:
            view = view[os.write(target_descriptor, view) :]
    os.fsync(target_descriptor)
    after = os.fstat(source_descriptor)
    if _identity(before) != _identity(after) or digest.hexdigest() != expected_sha256:
        raise PyxisArtifactIOError("source changed during staging")


def bind_cross_node_root(root: Path) -> CrossNodePathBinding:
    """Open one trusted shared root and retain its identity."""
    candidate = root.expanduser().absolute()
    descriptor = -1
    try:
        _require_trusted_ancestry(candidate)
        descriptor = os.open(candidate, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        binding = _binding_from_descriptor(candidate, descriptor)
        descriptor = -1
        return binding
    except OSError as exc:
        if isinstance(exc, PyxisArtifactIOError):
            raise
        raise PyxisArtifactIOError("cross-node root binding failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _require_private_regular(metadata: os.stat_result) -> None:
    """Require owner-only, read-only regular-file metadata."""
    if not stat.S_ISREG(metadata.st_mode):
        raise PyxisArtifactIOError("artifact is not a regular file")
    if metadata.st_uid != os.geteuid():
        raise PyxisArtifactIOError("artifact has the wrong owner")
    if stat.S_IMODE(metadata.st_mode) & 0o277:
        raise PyxisArtifactIOError("artifact has unsafe permissions")


def _require_private_directory(metadata: os.stat_result) -> None:
    """Require an owner-only directory held by a descriptor."""
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise PyxisArtifactIOError("staging root is not private")


def _require_same_directory_path(root: Path, expected: os.stat_result) -> None:
    """Require the staging path to name the held directory."""
    try:
        current = root.lstat()
    except OSError as exc:
        raise PyxisArtifactIOError("staging root changed during copy") from exc
    if _inode_identity(current) != _inode_identity(expected):
        raise PyxisArtifactIOError("staging root changed during copy")


def _verify_existing_digest_target(
    root_descriptor: int,
    target_name: str,
    expected_sha256: str,
) -> None:
    """Require an existing target to contain the exact private bytes."""
    descriptor = -1
    try:
        descriptor = os.open(
            target_name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=root_descriptor,
        )
        before = os.fstat(descriptor)
        _require_private_regular(before)
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after) or digest.hexdigest() != expected_sha256:
            raise PyxisArtifactIOError("existing staged artifact is invalid")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_new_digest_target(
    root_descriptor: int,
    target_name: str,
    expected_sha256: str,
) -> int:
    """Create a target, or verify an existing target and return a sentinel."""
    try:
        return os.open(
            target_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o400,
            dir_fd=root_descriptor,
        )
    except FileExistsError:
        _verify_existing_digest_target(
            root_descriptor,
            target_name,
            expected_sha256,
        )
        return -1


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    """Return the identity fields that must stay stable during one read."""
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


def _path_identity(
    metadata: os.stat_result, kind: Literal["directory", "regular"]
) -> tuple[int, ...]:
    """Return stable identity and policy fields for a bound path."""
    base = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        stat.S_IMODE(metadata.st_mode),
    )
    if kind == "directory":
        return base
    return (*base, metadata.st_size, metadata.st_mtime_ns)


def _filesystem_identity(metadata: os.statvfs_result) -> tuple[int, ...]:
    """Return stable fields for one opened filesystem view."""
    return (
        metadata.f_bsize,
        metadata.f_frsize,
        metadata.f_blocks,
        metadata.f_fsid,
        metadata.f_flag,
        metadata.f_namemax,
    )


def _binding_from_descriptor(root: Path, descriptor: int) -> CrossNodePathBinding:
    """Create one owned binding from an open directory descriptor."""
    metadata = os.fstat(descriptor)
    _require_private_directory(metadata)
    return CrossNodePathBinding(
        path=root,
        _root_descriptor=descriptor,
        _root_identity=_path_identity(metadata, "directory"),
        _filesystem_identity=_filesystem_identity(os.fstatvfs(descriptor)),
        _original_mode=stat.S_IMODE(metadata.st_mode),
    )


def _require_bound_kind(
    metadata: os.stat_result,
    kind: Literal["directory", "regular"],
    *,
    require_read_only: bool,
) -> None:
    """Require the expected type and a trusted owner-only mode."""
    if kind == "directory":
        if not stat.S_ISDIR(metadata.st_mode):
            raise PyxisArtifactIOError("cross-node artifact is not a directory")
    elif not stat.S_ISREG(metadata.st_mode):
        raise PyxisArtifactIOError("cross-node artifact is not a regular file")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise PyxisArtifactIOError("cross-node artifact permissions are unsafe")
    if require_read_only and stat.S_IMODE(metadata.st_mode) & 0o222:
        raise PyxisArtifactIOError("cross-node artifact is writable")


def _require_descriptor_digest(descriptor: int, expected_sha256: str) -> None:
    """Require exact bytes from an already-open regular file."""
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    if digest.hexdigest() != expected_sha256:
        raise PyxisArtifactIOError("cross-node artifact digest changed")


def _require_trusted_ancestry(path: Path) -> None:
    """Require root-owned or private ancestry without untrusted writers."""
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PyxisArtifactIOError("cross-node path ancestry is unavailable") from exc
    if resolved != path:
        raise PyxisArtifactIOError("cross-node path ancestry contains a symlink")
    for component in reversed((resolved, *resolved.parents)):
        metadata = component.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        root_owned_sticky = metadata.st_uid == 0 and bool(mode & stat.S_ISVTX)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {
            0,
            os.geteuid(),
        }:
            raise PyxisArtifactIOError("cross-node path ancestry is not trusted")
        if mode & 0o022 and not root_owned_sticky:
            raise PyxisArtifactIOError("cross-node path ancestry is not trusted")


def _inode_identity(metadata: os.stat_result) -> tuple[int, int]:
    """Return stable identity fields for one filesystem object."""
    return metadata.st_dev, metadata.st_ino


__all__ = [
    "CrossNodePathBinding",
    "PyxisArtifactIOError",
    "bind_cross_node_root",
    "effective_user_id",
    "read_private_regular_file",
    "stage_private_content_addressed_file",
    "validate_private_capacity_root",
    "validate_private_squashfs_file",
]
