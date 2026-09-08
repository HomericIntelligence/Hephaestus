"""Bounded content identities for owned implementation worktrees."""

from __future__ import annotations

import ctypes
import hashlib
import io
import os
import queue as queue_mod
import re
import selectors
import shutil
import signal
import stat
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard, cast

import hephaestus.automation.git_utils as git_utils
from hephaestus.config.child_environments import build_git_child_env

_TAIL = 4000
DIRTY_SNAPSHOT_GIT_MAX_BYTES = 4 * 1024 * 1024
DIRTY_SNAPSHOT_CONTENT_MAX_BYTES = 8 * 1024 * 1024
DIRTY_SNAPSHOT_CHANGED_FILE_MAX = 512

_DIRTY_CONTENT_SNAPSHOT_KEYS = frozenset({"index_sha256", "worktree_sha256", "untracked_sha256"})


class _GitInspectionResourceLimitError(RuntimeError):
    """Raised when untrusted writer data exceeds an inspection bound."""


@dataclass(frozen=True)
class _BoundedGitOutput:
    """One bounded Git output and its exact full digest."""

    text: str
    sha256: str
    byte_count: int


@dataclass(frozen=True)
class _DirtySnapshotEvidence:
    """Bounded dirty-content identity and changed-file count."""

    snapshot: dict[str, str]
    changed_file_count: int


def _subprocess_pipe_selector_supported() -> bool:
    """Return whether the platform selector supports subprocess pipes."""
    return os.name != "nt"


def _trusted_windows_taskkill() -> str:
    """Return the absolute Windows system ``taskkill`` executable."""
    system_directory = ctypes.create_unicode_buffer(32_768)
    ctypes_any = cast(Any, ctypes)
    kernel32 = ctypes_any.WinDLL("kernel32", use_last_error=True)
    length = kernel32.GetSystemDirectoryW(system_directory, len(system_directory))
    if length <= 0 or length >= len(system_directory):
        raise RuntimeError("Windows system directory is unavailable")
    taskkill = (Path(system_directory.value) / "taskkill.exe").resolve(strict=True)
    if not taskkill.is_absolute():  # pragma: no cover - resolve guarantees this
        raise RuntimeError("Windows task termination capability is unavailable")
    return str(taskkill)


def _terminate_bounded_process_tree(
    process: subprocess.Popen[bytes],
    *,
    process_group: bool,
) -> None:
    """Stop a bounded-output child and descendants that hold its pipes."""
    if process_group and os.name == "posix":
        with suppress(PermissionError, ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    elif process_group and os.name == "nt":  # pragma: no cover - Windows only
        with suppress(OSError, RuntimeError, subprocess.TimeoutExpired):
            subprocess.run(
                [_trusted_windows_taskkill(), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=build_git_child_env(),
                timeout=5,
                check=False,
            )
    with suppress(OSError):
        process.kill()
    with suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=5)


def _read_bounded_git_output_with_threads(  # noqa: C901
    process: subprocess.Popen[bytes],
    argv: tuple[str, ...],
    *,
    timeout: int | float,
    max_bytes: int,
    retain_text: bool,
    process_group: bool = False,
) -> _BoundedGitOutput:
    """Read both child pipes with bounded reader threads."""
    if process.stdout is None or process.stderr is None:  # pragma: no cover
        raise RuntimeError("Git output pipes are unavailable")
    streams = (process.stdout, process.stderr)
    events: queue_mod.Queue[tuple[str, bytes | BaseException | None]] = queue_mod.Queue(maxsize=16)
    stop = threading.Event()

    def put_event(name: str, value: bytes | BaseException | None) -> None:
        while not stop.is_set():
            try:
                events.put((name, value), timeout=0.05)
                return
            except queue_mod.Full:
                continue

    def read_pipe(name: str, stream: io.BufferedReader) -> None:
        try:
            while not stop.is_set():
                try:
                    chunk = os.read(stream.fileno(), 64 * 1024)
                except BlockingIOError:
                    stop.wait(0.01)
                    continue
                if not chunk:
                    break
                put_event(name, chunk)
        except BaseException as exc:
            put_event(name, exc)
        finally:
            put_event(name, None)

    readers: tuple[threading.Thread, ...] = ()
    started_readers: list[threading.Thread] = []
    digest = hashlib.sha256()
    output = bytearray()
    stderr_tail = bytearray()
    byte_count = 0
    ended: set[str] = set()
    deadline = time.monotonic() + timeout
    process_completed = False
    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
        readers = (
            threading.Thread(
                target=read_pipe,
                args=("stdout", process.stdout),
                name=f"hephaestus-git-pipe-{process.pid}-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=read_pipe,
                args=("stderr", process.stderr),
                name=f"hephaestus-git-pipe-{process.pid}-stderr",
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()
            started_readers.append(reader)
        while len(ended) != len(readers):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                name, chunk = events.get(timeout=min(remaining, 0.1))
            except queue_mod.Empty:
                continue
            if chunk is None:
                ended.add(name)
                continue
            if isinstance(chunk, BaseException):
                raise RuntimeError(f"Git {name} pipe read failed") from chunk
            if name == "stderr":
                stderr_tail.extend(chunk)
                if len(stderr_tail) > _TAIL:
                    del stderr_tail[:-_TAIL]
                continue
            byte_count += len(chunk)
            if byte_count > max_bytes:
                raise _GitInspectionResourceLimitError("Git output limit exceeded")
            digest.update(chunk)
            if retain_text:
                output.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        returncode = process.wait(timeout=remaining)
        process_completed = True
    finally:
        stop.set()
        if not process_completed:
            _terminate_bounded_process_tree(process, process_group=process_group)
        for reader in started_readers:
            reader.join(timeout=1.0)
        for reader, stream in zip(readers, (process.stdout, process.stderr), strict=True):
            if reader not in started_readers or not reader.is_alive():
                with suppress(OSError):
                    stream.close()
        if not readers:
            for stream in streams:
                with suppress(OSError):
                    stream.close()
    text = output.decode("utf-8", errors="surrogateescape") if retain_text else ""
    if returncode != 0:
        raise subprocess.CalledProcessError(
            returncode,
            argv,
            output=text,
            stderr=stderr_tail.decode("utf-8", errors="replace"),
        )
    return _BoundedGitOutput(text=text, sha256=digest.hexdigest(), byte_count=byte_count)


def _run_bounded_git_output(  # noqa: C901
    argv: tuple[str, ...],
    *,
    cwd: Path,
    timeout: int | float,
    max_bytes: int,
    retain_text: bool,
    env: dict[str, str] | None = None,
) -> _BoundedGitOutput:
    """Run Git with bounded memory and return an exact output digest."""
    timeout = cast(float, git_utils.remaining_operation_timeout(timeout))
    thread_backend = not _subprocess_pipe_selector_supported()
    process_options: dict[str, object] = {}
    if os.name == "posix":
        process_options["start_new_session"] = True
    elif os.name == "nt":  # pragma: no cover - Windows only
        process_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env or _controlled_git_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **cast(Any, process_options),
    )
    if process.stdout is None or process.stderr is None:  # pragma: no cover
        _terminate_bounded_process_tree(process, process_group=True)
        raise RuntimeError("Git output pipes are unavailable")
    if thread_backend:
        return _read_bounded_git_output_with_threads(
            process,
            argv,
            timeout=timeout,
            max_bytes=max_bytes,
            retain_text=retain_text,
            process_group=True,
        )
    selector: selectors.BaseSelector | None = None
    digest = hashlib.sha256()
    output = bytearray()
    stderr_tail = bytearray()
    byte_count = 0
    deadline = time.monotonic() + timeout
    process_completed = False
    try:
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stderr.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _events in selector.select(timeout=min(remaining, 0.1)):
                try:
                    chunk = os.read(key.fd, 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    stderr_tail.extend(chunk)
                    if len(stderr_tail) > _TAIL:
                        del stderr_tail[:-_TAIL]
                    continue
                byte_count += len(chunk)
                if byte_count > max_bytes:
                    raise _GitInspectionResourceLimitError("Git output limit exceeded")
                digest.update(chunk)
                if retain_text:
                    output.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        returncode = process.wait(timeout=remaining)
        process_completed = True
    finally:
        if not process_completed:
            _terminate_bounded_process_tree(process, process_group=True)
        if selector is not None:
            selector.close()
        with suppress(OSError):
            process.stdout.close()
        with suppress(OSError):
            process.stderr.close()
    text = output.decode("utf-8", errors="surrogateescape") if retain_text else ""
    if returncode != 0:
        raise subprocess.CalledProcessError(
            returncode,
            argv,
            output=text,
            stderr=stderr_tail.decode("utf-8", errors="replace"),
        )
    return _BoundedGitOutput(text=text, sha256=digest.hexdigest(), byte_count=byte_count)


def _path_content_identity(  # noqa: C901
    root: Path,
    paths_output: str,
    *,
    seed_digest: str = "",
    remaining_content_bytes: list[int] | None = None,
    timeout: int | None = None,
    copy_root: Path | None = None,
) -> str:
    """Hash NUL-delimited paths and their current file-system content."""
    if paths_output and (not paths_output.endswith("\0") or "\0\0" in paths_output):
        raise RuntimeError("dirty snapshot contains an unsafe path")
    relative_values = tuple(paths_output[:-1].split("\0")) if paths_output else ()
    if copy_root is not None:
        relative_values = tuple(
            sorted(
                set(relative_values),
                key=lambda value: (-len(Path(value).parts), os.fsencode(value)),
            )
        )
    digest = hashlib.sha256()
    digest.update(b"D")
    digest.update(seed_digest.encode("ascii"))
    if not relative_values:
        return digest.hexdigest()
    if not _secure_dir_fd_supported():
        raise RuntimeError("secure dirty snapshot path inspection is unavailable")

    def identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            stat.S_IMODE(metadata.st_mode),
        )

    deadline = time.monotonic() + timeout if timeout is not None else None

    def check_deadline() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired("dirty snapshot content", cast(int, timeout))

    def destination_path(parts: tuple[str, ...]) -> Path:
        """Return one trusted snapshot path without following copied links."""
        if copy_root is None:  # pragma: no cover - guarded by callers
            raise RuntimeError("candidate snapshot root is unavailable")
        parent = copy_root
        for component in parts[:-1]:
            candidate = parent / component
            try:
                candidate.mkdir()
            except FileExistsError:
                if candidate.is_symlink() or not candidate.is_dir():
                    raise RuntimeError("candidate snapshot has an unsafe path prefix") from None
            parent = candidate
        return parent / parts[-1]

    open_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    root_fd = os.open(root, open_flags | os.O_DIRECTORY)
    try:
        for relative in relative_values:
            check_deadline()
            relative_path = Path(relative)
            parts = relative_path.parts
            if (
                relative != relative_path.as_posix()
                or relative_path.is_absolute()
                or not parts
                or any(part in {"", ".", ".."} for part in parts)
            ):
                raise RuntimeError("dirty snapshot contains an unsafe path")
            encoded_path = os.fsencode(relative)
            digest.update(len(encoded_path).to_bytes(8, "big"))
            digest.update(encoded_path)
            descriptors: list[int] = []
            parent_fd = root_fd
            try:
                try:
                    missing_ancestor = False
                    for component in parts[:-1]:
                        check_deadline()
                        component_metadata = os.stat(
                            component,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                        if stat.S_ISLNK(component_metadata.st_mode):
                            missing_ancestor = True
                            break
                        if not stat.S_ISDIR(component_metadata.st_mode):
                            missing_ancestor = True
                            break
                        parent_fd = os.open(
                            component,
                            open_flags | os.O_DIRECTORY,
                            dir_fd=parent_fd,
                        )
                        descriptors.append(parent_fd)
                    if missing_ancestor:
                        digest.update(b"M")
                        continue
                    metadata = os.stat(
                        parts[-1],
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    digest.update(b"M")
                    continue
                except NotADirectoryError as exc:
                    raise RuntimeError("dirty snapshot contains an unsafe path") from exc
                digest.update(f"{stat.S_IFMT(metadata.st_mode):o}\0".encode())
                if stat.S_ISLNK(metadata.st_mode):
                    digest.update(b"L")
                    target = os.fsencode(os.readlink(parts[-1], dir_fd=parent_fd))
                    after = os.stat(
                        parts[-1],
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if identity(metadata) != identity(after):
                        raise RuntimeError("dirty snapshot content changed during inspection")
                    if remaining_content_bytes is not None:
                        remaining_content_bytes[0] -= len(target)
                        if remaining_content_bytes[0] < 0:
                            raise _GitInspectionResourceLimitError(
                                "dirty snapshot content limit exceeded"
                            )
                    digest.update(len(target).to_bytes(8, "big"))
                    digest.update(target)
                    if copy_root is not None:
                        os.symlink(os.fsdecode(target), destination_path(parts))
                elif stat.S_ISREG(metadata.st_mode):
                    file_fd = os.open(
                        parts[-1],
                        open_flags | os.O_NONBLOCK,
                        dir_fd=parent_fd,
                    )
                    descriptors.append(file_fd)
                    before = os.fstat(file_fd)
                    if not stat.S_ISREG(before.st_mode) or (
                        metadata.st_dev,
                        metadata.st_ino,
                    ) != (before.st_dev, before.st_ino):
                        raise RuntimeError("dirty snapshot content changed during inspection")
                    if (
                        remaining_content_bytes is not None
                        and before.st_size > remaining_content_bytes[0]
                    ):
                        raise _GitInspectionResourceLimitError(
                            "dirty snapshot content limit exceeded"
                        )
                    digest.update(b"F")
                    digest.update(b"X" if before.st_mode & 0o111 else b"N")
                    digest.update(before.st_size.to_bytes(8, "big"))
                    os.set_blocking(file_fd, True)
                    captured = bytearray()
                    while True:
                        check_deadline()
                        read_limit = 1024 * 1024
                        if remaining_content_bytes is not None:
                            read_limit = min(read_limit, remaining_content_bytes[0] + 1)
                        block = os.read(file_fd, max(1, read_limit))
                        if not block:
                            break
                        if remaining_content_bytes is not None:
                            remaining_content_bytes[0] -= len(block)
                            if remaining_content_bytes[0] < 0:
                                raise _GitInspectionResourceLimitError(
                                    "dirty snapshot content limit exceeded"
                                )
                        digest.update(block)
                        if copy_root is not None:
                            captured.extend(block)
                    if identity(before) != identity(os.fstat(file_fd)):
                        raise RuntimeError("dirty snapshot content changed during inspection")
                    if copy_root is not None:
                        copy_path = destination_path(parts)
                        copy_path.write_bytes(captured)
                        copy_path.chmod(stat.S_IMODE(before.st_mode))
                else:
                    if not stat.S_ISDIR(metadata.st_mode):
                        raise RuntimeError("dirty snapshot contains an unsupported path type")
                    after = os.stat(
                        parts[-1],
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if identity(metadata) != identity(after):
                        raise RuntimeError("dirty snapshot content changed during inspection")
                    digest.update(b"O")
                    digest.update(f"{metadata.st_size}:{metadata.st_rdev}".encode())
                    if copy_root is not None:
                        destination_path(parts).mkdir(exist_ok=True)
            finally:
                for descriptor in reversed(descriptors):
                    os.close(descriptor)
    finally:
        os.close(root_fd)
    return digest.hexdigest()


def _dirty_worktree_snapshot_evidence(
    worktree: Path,
    *,
    timeout: int,
    git_env: dict[str, str] | None = None,
) -> _DirtySnapshotEvidence:
    """Return bounded identities for index, tracked, and untracked content."""
    command_prefix = ("git", "-c", "core.fsmonitor=false")
    env = dict(git_env or _isolated_checkout_git_env())
    index = _run_bounded_git_output(
        (*command_prefix, "ls-files", "--stage", "-z"),
        cwd=worktree,
        timeout=timeout,
        max_bytes=DIRTY_SNAPSHOT_GIT_MAX_BYTES,
        retain_text=False,
        env=env,
    )
    tracked_paths = _run_bounded_git_output(
        (
            *command_prefix,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            "HEAD",
        ),
        cwd=worktree,
        timeout=timeout,
        max_bytes=DIRTY_SNAPSHOT_GIT_MAX_BYTES,
        retain_text=True,
        env=env,
    )
    tracked_diff = _run_bounded_git_output(
        (
            *command_prefix,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--binary",
            "--full-index",
            "HEAD",
        ),
        cwd=worktree,
        timeout=timeout,
        max_bytes=DIRTY_SNAPSHOT_GIT_MAX_BYTES,
        retain_text=False,
        env=env,
    )
    untracked_paths = _run_bounded_git_output(
        (*command_prefix, "ls-files", "--others", "--exclude-standard", "-z"),
        cwd=worktree,
        timeout=timeout,
        max_bytes=DIRTY_SNAPSHOT_GIT_MAX_BYTES,
        retain_text=True,
        env=env,
    )
    tracked = tuple(path for path in tracked_paths.text.split("\0") if path)
    untracked = tuple(path for path in untracked_paths.text.split("\0") if path)
    changed_file_count = len(set(tracked).union(untracked))
    if changed_file_count > DIRTY_SNAPSHOT_CHANGED_FILE_MAX:
        raise _GitInspectionResourceLimitError("dirty snapshot file limit exceeded")
    remaining_content_bytes = [DIRTY_SNAPSHOT_CONTENT_MAX_BYTES]
    snapshot = {
        "index_sha256": index.sha256,
        "worktree_sha256": _path_content_identity(
            worktree,
            tracked_paths.text,
            seed_digest=tracked_diff.sha256,
            remaining_content_bytes=remaining_content_bytes,
            timeout=timeout,
        ),
        "untracked_sha256": _path_content_identity(
            worktree,
            untracked_paths.text,
            remaining_content_bytes=remaining_content_bytes,
            timeout=timeout,
        ),
    }
    return _DirtySnapshotEvidence(snapshot=snapshot, changed_file_count=changed_file_count)


def _dirty_worktree_content_snapshot(
    worktree: Path,
    *,
    timeout: int,
    git_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return the bounded content identity for one dirty worktree."""
    return _dirty_worktree_snapshot_evidence(
        worktree,
        timeout=timeout,
        git_env=git_env,
    ).snapshot


def _valid_dirty_content_snapshot(value: object) -> TypeGuard[dict[str, str]]:
    """Return whether a dirty snapshot has the closed digest schema."""
    return (
        isinstance(value, dict)
        and set(value) == _DIRTY_CONTENT_SNAPSHOT_KEYS
        and all(
            isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
            for digest in value.values()
        )
    )


_TRUSTED_GIT_CANDIDATES = (
    Path("/opt/homebrew/bin/git"),
    Path("/usr/local/bin/git"),
    Path("/usr/bin/git"),
)

_TRUSTED_GIT_ROOTS = (Path("/opt/homebrew"), Path("/usr/local"), Path("/usr"))

_TRUSTED_GIT_DISCOVERY_ROOTS = (
    Path("/opt/homebrew/Cellar/git"),
    Path("/usr/local/Cellar/git"),
    Path("/usr/bin"),
)


def _controlled_git_env() -> dict[str, str]:
    """Return an environment that cannot redirect or extend Git execution."""
    env = build_git_child_env()
    trusted_git = _trusted_git_executable()
    path_entries = os.defpath.split(os.pathsep)
    if trusted_git is not None:
        trusted_parent = str(Path(trusted_git).parent)
        path_entries = [
            trusted_parent,
            *(entry for entry in path_entries if entry != trusted_parent),
        ]
    env["PATH"] = os.pathsep.join(path_entries)
    return env


def _isolated_checkout_git_env() -> dict[str, str]:
    """Return a legacy Git environment with host configuration disabled.

    ``GIT_CONFIG`` controls ``git config``. A private ``GIT_DIR`` is also
    necessary when another Git command must not load repository configuration.
    """
    env = _controlled_git_env()
    env["GIT_CONFIG"] = os.devnull
    return env


def _trusted_git_executable() -> str | None:
    """Return an allowlisted, non-writable ``git`` binary for host checks."""
    discovered = shutil.which("git")
    candidates: tuple[tuple[Path, bool], ...] = tuple(
        (candidate, False) for candidate in _TRUSTED_GIT_CANDIDATES
    )
    if discovered is not None:
        candidates = ((Path(discovered), True), *candidates)
    for candidate, is_discovered in candidates:
        try:
            if is_discovered:
                if not candidate.is_absolute() or candidate.is_symlink():
                    continue
                resolved = candidate
                trusted_roots = _TRUSTED_GIT_DISCOVERY_ROOTS
            else:
                resolved = candidate.resolve(strict=True)
                trusted_roots = _TRUSTED_GIT_ROOTS
            mode = resolved.stat().st_mode
        except OSError:
            continue
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            continue
        if mode & 0o022:
            continue
        if not any(resolved.is_relative_to(root) for root in trusted_roots):
            continue
        return str(resolved)
    return None


def _secure_dir_fd_supported() -> bool:
    """Return whether secure descriptor-relative path traversal is available."""
    required = (os.open, os.stat, os.readlink, os.mkdir)
    return bool(
        os.name == "posix"
        and getattr(os, "O_DIRECTORY", 0)
        and getattr(os, "O_NOFOLLOW", 0)
        and all(function in os.supports_dir_fd for function in required)
        and os.stat in os.supports_follow_symlinks
    )
