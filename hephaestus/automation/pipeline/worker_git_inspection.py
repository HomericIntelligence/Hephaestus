"""Bind candidate inspection to validated, controlled Git metadata."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import hephaestus.automation.git_utils as git_utils
from hephaestus.automation.pipeline.git_jobs import (
    DIRTY_SNAPSHOT_CHANGED_FILE_MAX,
    DIRTY_SNAPSHOT_CONTENT_MAX_BYTES,
    IMPLEMENTATION_INSPECTION_DIFF_MAX_BYTES,
    IMPLEMENTATION_INSPECTION_METADATA_MAX_BYTES,
    IMPLEMENTATION_INSPECTION_STATUS_MAX_BYTES,
)
from hephaestus.git.commit_paths import CommitPaths, is_bounded_commit_paths
from hephaestus.git.config_safety import unsafe_local_git_config_key
from hephaestus.git.snapshot import (
    _BoundedGitOutput as _BoundedGitOutput,
    _controlled_git_env as _controlled_git_env,
    _dirty_worktree_snapshot_evidence as _dirty_worktree_snapshot_evidence,
    _DirtySnapshotEvidence as _DirtySnapshotEvidence,
    _GitInspectionResourceLimitError as _GitInspectionResourceLimitError,
    _isolated_checkout_git_env as _isolated_checkout_git_env,
    _path_content_identity as _path_content_identity,
    _run_bounded_git_output as _run_bounded_git_output,
    _secure_dir_fd_supported as _secure_dir_fd_supported,
)
from hephaestus.io.utils import write_secure
from hephaestus.utils.git import _is_full_commit_sha


def _bounded_candidate_commit_paths(
    worktree: Path,
    head: str,
    *,
    timeout: int,
    git_env: dict[str, str] | None = None,
) -> CommitPaths:
    """Return one bounded, filtered path manifest for candidate staging."""
    from hephaestus.git.commit_paths import (
        parse_porcelain_status,
        reject_filtered_path_shape_changes,
        select_commit_paths,
    )

    env = dict(git_env or _isolated_checkout_git_env())
    porcelain = _run_bounded_git_output(
        (
            "git",
            "-c",
            "core.fsmonitor=false",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--no-renames",
        ),
        cwd=worktree,
        timeout=timeout,
        max_bytes=IMPLEMENTATION_INSPECTION_STATUS_MAX_BYTES,
        retain_text=True,
        env=env,
    ).text
    status_entries = parse_porcelain_status(porcelain)
    if len({path for _status, path in status_entries}) > DIRTY_SNAPSHOT_CHANGED_FILE_MAX:
        raise _GitInspectionResourceLimitError("dirty snapshot file limit exceeded")
    selected = select_commit_paths(status_entries, None)
    if not selected.add_paths and not selected.update_paths:
        raise RuntimeError("dirty writer has no publishable non-secret paths")
    reject_filtered_path_shape_changes(status_entries, selected)
    if not is_bounded_commit_paths(
        selected,
        max_paths=DIRTY_SNAPSHOT_CHANGED_FILE_MAX,
        max_bytes=IMPLEMENTATION_INSPECTION_STATUS_MAX_BYTES,
    ):
        raise _GitInspectionResourceLimitError("dirty snapshot path manifest limit exceeded")
    return selected


def _candidate_commit_tree_evidence(
    worktree: Path,
    head: str,
    *,
    timeout: int,
    selected: CommitPaths | None = None,
    git_env: dict[str, str] | None = None,
    snapshot_root: Path | None = None,
) -> tuple[str, _BoundedGitOutput]:
    """Build and diff the non-secret tree in a disposable Git object store."""
    selected = selected or _bounded_candidate_commit_paths(
        worktree,
        head,
        timeout=timeout,
        git_env=git_env,
    )
    with tempfile.TemporaryDirectory(prefix="hephaestus-candidate-index-") as temporary:
        temporary_root = Path(temporary)
        index = temporary_root / "index"
        objects = temporary_root / "objects"
        objects.mkdir()
        if snapshot_root is None:
            snapshot_root = temporary_root / "worktree"
        snapshot_root.mkdir(mode=0o700)
        env = dict(git_env or _isolated_checkout_git_env())
        shared_objects = git_utils.run(
            ["git", "rev-parse", "--git-path", "objects"],
            cwd=worktree,
            timeout=timeout,
            env=env,
        ).stdout.strip()
        shared_object_path = Path(shared_objects)
        if not shared_object_path.is_absolute():
            shared_object_path = worktree / shared_object_path
        shared_object_path = shared_object_path.resolve(strict=True)
        if not shared_object_path.is_dir():
            raise RuntimeError("shared Git object store is unavailable")
        env["GIT_INDEX_FILE"] = str(index)
        env["GIT_OBJECT_DIRECTORY"] = str(objects)
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(shared_object_path)
        selected_paths = (*selected.add_paths, *selected.update_paths)
        _path_content_identity(
            worktree,
            "\0".join(selected_paths) + "\0",
            remaining_content_bytes=[DIRTY_SNAPSHOT_CONTENT_MAX_BYTES],
            timeout=timeout,
            copy_root=snapshot_root,
        )
        env["GIT_WORK_TREE"] = str(snapshot_root)
        git_utils.run(
            ["git", "read-tree", head],
            cwd=worktree,
            timeout=timeout,
            env=env,
        )
        if selected.update_paths:
            update_pathspec = temporary_root / "update-paths"
            update_pathspec.write_bytes(
                b"\0".join(os.fsencode(path) for path in selected.update_paths) + b"\0"
            )
            git_utils.run(
                [
                    "git",
                    "--literal-pathspecs",
                    "rm",
                    "-r",
                    "-f",
                    "--cached",
                    "--ignore-unmatch",
                    f"--pathspec-from-file={update_pathspec}",
                    "--pathspec-file-nul",
                ],
                cwd=worktree,
                timeout=timeout,
                env=env,
            )
        if selected.add_paths:
            add_pathspec = temporary_root / "add-paths"
            add_pathspec.write_bytes(
                b"\0".join(os.fsencode(path) for path in selected.add_paths) + b"\0"
            )
            git_utils.run(
                [
                    "git",
                    "--literal-pathspecs",
                    "add",
                    "-A",
                    f"--pathspec-from-file={add_pathspec}",
                    "--pathspec-file-nul",
                ],
                cwd=worktree,
                timeout=timeout,
                env=env,
            )
        if not selected.update_paths and not selected.add_paths:
            raise RuntimeError("dirty writer has no publishable non-secret tree change")
        tree = git_utils.run(
            ["git", "write-tree"],
            cwd=worktree,
            timeout=timeout,
            env=env,
        ).stdout.strip()
        if not _is_full_commit_sha(tree):
            raise RuntimeError("candidate commit tree is unavailable")
        diff = _run_bounded_git_output(
            (
                "git",
                "-c",
                "core.fsmonitor=false",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--binary",
                "--full-index",
                head,
                tree,
            ),
            cwd=worktree,
            timeout=timeout,
            max_bytes=IMPLEMENTATION_INSPECTION_DIFF_MAX_BYTES,
            retain_text=True,
            env=env,
        )
    return tree, diff


def _inspect_candidate_with_private_git(
    worktree: Path,
    head: str,
    *,
    timeout: int,
    linked_env: dict[str, str],
) -> tuple[_DirtySnapshotEvidence, _BoundedGitOutput, str, _BoundedGitOutput, CommitPaths | None]:
    """Inspect one candidate without use of repository-local Git configuration."""
    with _private_linked_worktree_git_env(linked_env, detached_head=head) as env:
        private_head = git_utils.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            timeout=timeout,
            env=env,
        ).stdout.strip()
        if private_head != head:
            raise RuntimeError("private Git metadata HEAD changed")
        before = _dirty_worktree_snapshot_evidence(worktree, timeout=timeout, git_env=env)
        status_result = _run_bounded_git_output(
            (
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "status.relativePaths=false",
                "status",
                "--short",
                "--untracked-files=all",
                "--no-renames",
            ),
            cwd=worktree,
            timeout=timeout,
            max_bytes=IMPLEMENTATION_INSPECTION_STATUS_MAX_BYTES,
            retain_text=True,
            env=env,
        )
        selected_paths: CommitPaths | None = None
        if status_result.text.strip():
            selected_paths = _bounded_candidate_commit_paths(
                worktree,
                head,
                timeout=timeout,
                git_env=env,
            )
            candidate_tree, diff_result = _candidate_commit_tree_evidence(
                worktree,
                head,
                timeout=timeout,
                selected=selected_paths,
                git_env=env,
            )
        else:
            candidate_tree = head
            diff_result = _BoundedGitOutput(
                text="",
                sha256=hashlib.sha256(b"").hexdigest(),
                byte_count=0,
            )
        after = _dirty_worktree_snapshot_evidence(worktree, timeout=timeout, git_env=env)
        if before != after:
            raise RuntimeError("dirty writer changed during inspection")
        return after, status_result, candidate_tree, diff_result, selected_paths


def _unsafe_local_git_config_key(config: str) -> str | None:
    """Return an unsafe repository/worktree config key, if *config* contains one."""
    return unsafe_local_git_config_key(config)


def _checkout_preflight_error(  # noqa: C901
    checkout: Path,
    timeout_s: int,
    *,
    max_config_bytes: int | None = None,
) -> str | None:
    """Return a reusable-checkout metadata safety failure before synchronization."""
    git_marker = checkout / ".git"
    if not git_marker.exists():
        return None
    if git_marker.is_symlink():
        return "checkout has unsafe Git metadata"
    config_paths: list[Path]
    if git_marker.is_dir():
        config_paths = [git_marker / "config"]
        worktree_config = git_marker / "config.worktree"
    elif git_marker.is_file():
        pointer, _identity = _portable_read_git_pointer(git_marker)
        if not pointer.startswith("gitdir: "):
            return "checkout has unsafe Git metadata"
        admin_dir = _normalized_metadata_path(checkout, pointer.removeprefix("gitdir: "))
        if admin_dir.parent.name != "worktrees":
            return "checkout has unsafe Git metadata"
        repo_git_dir = admin_dir.parent.parent
        if not repo_git_dir.is_dir():
            return "checkout has unsafe Git metadata"
        config_paths = [repo_git_dir / "config"]
        worktree_config = admin_dir / "config.worktree"
    else:
        return "checkout has unsafe Git metadata"
    if worktree_config.exists():
        config_paths.append(worktree_config)
    config_parts: list[str] = []
    if max_config_bytes is None:
        for config_path in config_paths:
            neutral_cwd = Path(config_path.anchor)
            config_parts.append(
                git_utils.run(
                    [
                        "git",
                        "config",
                        "--file",
                        str(config_path),
                        "--no-includes",
                        "--null",
                        "--list",
                    ],
                    cwd=neutral_cwd,
                    timeout=timeout_s,
                    env=_controlled_git_env(),
                ).stdout
            )
    else:
        remaining = max_config_bytes
        for config_path in config_paths:
            neutral_cwd = Path(config_path.anchor)
            output = _run_bounded_git_output(
                (
                    "git",
                    "config",
                    "--file",
                    str(config_path),
                    "--no-includes",
                    "--null",
                    "--list",
                ),
                cwd=neutral_cwd,
                timeout=timeout_s,
                max_bytes=remaining,
                retain_text=True,
            ).text
            config_parts.append(output)
            remaining -= len(output.encode("utf-8", "surrogateescape"))
    config = "\0".join(config_parts)
    unsafe_config = _unsafe_local_git_config_key(config)
    if unsafe_config is not None:
        return "checkout has unsafe local Git configuration"
    graft_value = git_utils.run(
        ["git", "rev-parse", "--git-path", "info/grafts"],
        cwd=checkout,
        timeout=timeout_s,
        env=_controlled_git_env(),
    ).stdout.strip()
    if not graft_value:
        return None
    graft_path = Path(graft_value)
    if not graft_path.is_absolute():
        graft_path = checkout / graft_path
    if graft_path.is_file():
        return "checkout has unsafe legacy Git grafts"
    return None


@dataclass(frozen=True)
class _FilesystemIdentity:
    """Identify one open file-system object."""

    device: int
    inode: int
    file_type: int


@dataclass(frozen=True)
class _LinkedWorktreeBinding:
    """Bind one linked worktree to its validated Git metadata graph."""

    repo_root: Path
    worktree: Path
    common_dir: Path
    admin_dir: Path
    index: Path
    objects: Path
    branch_ref: str
    branch_sha: str
    common_identity: _FilesystemIdentity
    index_identity: _FilesystemIdentity
    structural_identity: tuple[_FilesystemIdentity, ...]


@dataclass(frozen=True)
class _RepositoryGitRootBinding:
    """Bind a checkout root to its common Git metadata directory."""

    common_dir: Path
    common_identity: _FilesystemIdentity
    intake_admin_name: str | None
    intake_admin_identity: _FilesystemIdentity | None
    structural_identity: tuple[_FilesystemIdentity, ...]


class _LinkedWorktreeGitEnvironment(dict[str, str]):
    """Carry a Git child environment and its structural binding."""

    def __init__(self, values: dict[str, str], binding: _LinkedWorktreeBinding) -> None:
        """Initialize the environment for one validated binding."""
        super().__init__(values)
        self.binding = binding


class _PrivateLinkedWorktreeGitEnvironment(dict[str, str]):
    """Carry private Git metadata and its real linked-worktree binding."""

    def __init__(self, values: dict[str, str], linked_env: dict[str, str]) -> None:
        """Initialize one private environment from a validated real binding."""
        super().__init__(values)
        self.linked_env = linked_env
        self.binding = getattr(linked_env, "binding", None)


def _filesystem_identity(metadata: os.stat_result) -> _FilesystemIdentity:
    """Return the stable identity fields for one open object."""
    return _FilesystemIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        file_type=stat.S_IFMT(metadata.st_mode),
    )


def _open_directory_no_follow(path: Path) -> tuple[int, _FilesystemIdentity]:
    """Open one directory and reject a final-component link or replacement."""
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("linked worktree metadata directory is unsafe")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    if os.name == "posix":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ) or not stat.S_ISDIR(opened.st_mode):
            raise RuntimeError("linked worktree metadata directory changed")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, _filesystem_identity(opened)


def _open_directory_at_no_follow(
    parent_fd: int,
    name: str,
) -> tuple[int, _FilesystemIdentity]:
    """Open one child directory without following a path component link."""
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("linked worktree metadata directory is unsafe")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    if os.name == "posix":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ) or not stat.S_ISDIR(opened.st_mode):
            raise RuntimeError("linked worktree metadata directory changed")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, _filesystem_identity(opened)


def _open_absolute_directory_no_follow(
    path: Path,
    *,
    bound_path: Path | None = None,
    bound_identity: _FilesystemIdentity | None = None,
) -> tuple[int, _FilesystemIdentity, Path]:
    """Open an absolute directory through its raw path components."""
    if not path.is_absolute():
        raise RuntimeError("linked worktree metadata path is not absolute")
    current = Path(path.anchor)
    descriptor, identity = _open_directory_no_follow(current)
    descriptors = [descriptor]
    try:
        for component in path.parts[1:]:
            if component == ".":
                continue
            if component == "..":
                if len(descriptors) == 1:
                    raise RuntimeError("linked worktree metadata path escapes its anchor")
                os.close(descriptors.pop())
                current = current.parent
                descriptor = descriptors[-1]
                identity = _filesystem_identity(os.fstat(descriptor))
                continue
            descriptor, identity = _open_directory_at_no_follow(descriptors[-1], component)
            descriptors.append(descriptor)
            current /= component
            if bound_path is not None and current == bound_path and identity != bound_identity:
                raise RuntimeError("linked worktree metadata directory changed")
    except BaseException:
        for open_descriptor in reversed(descriptors):
            os.close(open_descriptor)
        raise
    for open_descriptor in descriptors[:-1]:
        os.close(open_descriptor)
    return descriptors[-1], identity, current


def _metadata_path(base: Path, value: str) -> tuple[Path, bool]:
    """Return an absolute raw metadata path and its relative-path state."""
    raw = Path(value)
    relative = not raw.is_absolute()
    return (base / raw if relative else raw), relative


def _open_root_metadata_directory_no_follow(
    base: Path,
    value: str,
    *,
    base_identity: _FilesystemIdentity,
    error: str,
) -> tuple[int, _FilesystemIdentity, Path]:
    """Open a Git metadata directory through the pointer's raw path."""
    raw, relative = _metadata_path(base, value)
    try:
        return _open_absolute_directory_no_follow(
            raw,
            bound_path=base if relative else None,
            bound_identity=base_identity if relative else None,
        )
    except RuntimeError as exc:
        if ".." in raw.parts:
            raise RuntimeError(error) from exc
        raise
    except OSError as exc:
        raise RuntimeError(error) from exc


def _root_metadata_file_identity_no_follow(
    base: Path,
    value: str,
    *,
    base_identity: _FilesystemIdentity,
    error: str,
) -> tuple[Path, _FilesystemIdentity]:
    """Identify a Git metadata file through the pointer's raw path."""
    raw, relative = _metadata_path(base, value)
    parent_fd = -1
    try:
        parent_fd, _parent_identity, parent = _open_absolute_directory_no_follow(
            raw.parent,
            bound_path=base if relative else None,
            bound_identity=base_identity if relative else None,
        )
        return parent / raw.name, _regular_file_identity_at(parent_fd, raw.name)
    except RuntimeError as exc:
        if ".." in raw.parts:
            raise RuntimeError(error) from exc
        raise
    except OSError as exc:
        raise RuntimeError(error) from exc
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _read_bounded_regular_at(
    parent_fd: int,
    name: str,
    *,
    max_bytes: int,
) -> tuple[bytes, _FilesystemIdentity]:
    """Read one bounded regular file through a bound parent descriptor."""
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
        raise RuntimeError("linked worktree metadata file is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if os.name == "posix":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino) or not stat.S_ISREG(
            opened.st_mode
        ):
            raise RuntimeError("linked worktree metadata file changed")
        payload = os.read(descriptor, max_bytes + 1)
        if len(payload) > max_bytes or os.read(descriptor, 1):
            raise RuntimeError("linked worktree metadata file is too large")
    finally:
        os.close(descriptor)
    return payload, _filesystem_identity(opened)


def _read_bounded_git_pointer_at(parent_fd: int, name: str) -> tuple[str, _FilesystemIdentity]:
    """Read one small regular Git metadata file without following a link."""
    payload, identity = _read_bounded_regular_at(parent_fd, name, max_bytes=4096)
    try:
        return payload.decode("utf-8").strip(), identity
    except UnicodeDecodeError as exc:
        raise RuntimeError("linked worktree metadata pointer is invalid") from exc


def _reject_object_alternates_at(objects_fd: int) -> None:
    """Reject a repository object store that can redirect object reads."""
    try:
        info_fd, _identity = _open_directory_at_no_follow(objects_fd, "info")
    except FileNotFoundError:
        return
    try:
        try:
            os.stat("alternates", dir_fd=info_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise RuntimeError("linked worktree object store has unsafe alternates")
    finally:
        os.close(info_fd)


def _reject_portable_object_alternates(objects: Path) -> None:
    """Reject an alternate-object entry without following a portable path."""
    info = objects / "info"
    try:
        _portable_path_identity(info, directory=True)
    except FileNotFoundError:
        return
    try:
        (info / "alternates").lstat()
    except FileNotFoundError:
        return
    raise RuntimeError("linked worktree object store has unsafe alternates")


def _regular_file_identity_at(parent_fd: int, name: str) -> _FilesystemIdentity:
    """Open one regular metadata file and return its no-follow identity."""
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("linked worktree metadata file is unsafe")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if os.name == "posix":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino) or not stat.S_ISREG(
            opened.st_mode
        ):
            raise RuntimeError("linked worktree metadata file changed")
        return _filesystem_identity(opened)
    finally:
        os.close(descriptor)


def _read_branch_ref(
    common_fd: int,
    common_dir: Path,
    branch_ref: str,
) -> str:
    """Read one direct loose or packed branch reference from bound metadata."""
    components = tuple(branch_ref.split("/"))
    if (
        len(components) < 3
        or components[:2] != ("refs", "heads")
        or any(component in {"", ".", ".."} for component in components)
    ):
        raise RuntimeError("linked worktree branch reference is invalid")
    descriptors: list[int] = []
    parent_fd = common_fd
    try:
        try:
            for component in components[:-1]:
                parent_fd, _identity = _open_directory_at_no_follow(parent_fd, component)
                descriptors.append(parent_fd)
            value, _identity = _read_bounded_git_pointer_at(parent_fd, components[-1])
        except FileNotFoundError:
            value = ""
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    if value:
        if not _is_full_commit_sha(value):
            raise RuntimeError("linked worktree branch reference is invalid")
        return value

    try:
        packed_bytes, _packed_identity = _read_bounded_regular_at(
            common_fd,
            "packed-refs",
            max_bytes=IMPLEMENTATION_INSPECTION_METADATA_MAX_BYTES,
        )
    except FileNotFoundError:
        packed_bytes = b""
    try:
        packed = packed_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("linked worktree packed references are invalid") from exc
    matches = [
        line.partition(" ")[0]
        for line in packed.splitlines()
        if not line.startswith(("#", "^")) and line.partition(" ")[2] == branch_ref
    ]
    if len(matches) != 1 or not _is_full_commit_sha(matches[0]):
        raise RuntimeError("linked worktree branch reference is unavailable")
    return matches[0]


def _portable_path_identity(
    path: Path,
    *,
    directory: bool,
    bound_path: Path | None = None,
    bound_identity: _FilesystemIdentity | None = None,
) -> _FilesystemIdentity:
    """Validate the raw components of one absolute portable path."""
    if not path.is_absolute():
        raise RuntimeError("linked worktree metadata path is not absolute")
    current = Path(path.anchor)
    final = current.lstat()
    metadata_stack = [final]
    components = path.parts[1:]
    for offset, component in enumerate(components):
        if component == ".":
            continue
        if component == "..":
            if len(metadata_stack) == 1:
                raise RuntimeError("linked worktree metadata path escapes its anchor")
            metadata_stack.pop()
            current = current.parent
            final = metadata_stack[-1]
            continue
        current /= component
        metadata = current.lstat()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0) & reparse_flag
        ):
            raise RuntimeError("linked worktree metadata path has a reparse point")
        if offset != len(components) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("linked worktree metadata path prefix is unsafe")
        final = metadata
        metadata_stack.append(metadata)
        identity = _filesystem_identity(metadata)
        if bound_path is not None and current == bound_path and identity != bound_identity:
            raise RuntimeError("linked worktree metadata directory changed")
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(final.st_mode):
        raise RuntimeError("linked worktree metadata path has an unsafe type")
    return _filesystem_identity(final)


def _portable_read_bounded_regular(
    path: Path,
    *,
    max_bytes: int,
) -> tuple[bytes, _FilesystemIdentity]:
    """Read one bounded regular file after portable reparse-point validation."""
    identity = _portable_path_identity(path, directory=False)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if _filesystem_identity(opened) != identity or opened.st_size > max_bytes:
            raise RuntimeError("linked worktree metadata file changed")
        payload = os.read(descriptor, max_bytes + 1)
        if len(payload) > max_bytes or os.read(descriptor, 1):
            raise RuntimeError("linked worktree metadata file is too large")
    finally:
        os.close(descriptor)
    return payload, identity


def _portable_read_git_pointer(path: Path) -> tuple[str, _FilesystemIdentity]:
    """Read one portable bounded Git pointer."""
    payload, identity = _portable_read_bounded_regular(path, max_bytes=4096)
    try:
        return payload.decode("utf-8").strip(), identity
    except UnicodeDecodeError as exc:
        raise RuntimeError("linked worktree metadata pointer is invalid") from exc


def _normalized_metadata_path(base: Path, value: str) -> Path:
    """Return one absolute lexical metadata path without resolving links."""
    raw = Path(value)
    candidate = raw if raw.is_absolute() else base / raw
    return Path(os.path.abspath(candidate))


def _portable_root_metadata_identity(
    base: Path,
    value: str,
    *,
    directory: bool,
    base_identity: _FilesystemIdentity,
    error: str,
) -> tuple[Path, _FilesystemIdentity]:
    """Validate and identify one portable Git metadata pointer."""
    raw, relative = _metadata_path(base, value)
    try:
        identity = _portable_path_identity(
            raw,
            directory=directory,
            bound_path=base if relative else None,
            bound_identity=base_identity if relative else None,
        )
    except RuntimeError as exc:
        if ".." in raw.parts:
            raise RuntimeError(error) from exc
        raise
    except OSError as exc:
        raise RuntimeError(error) from exc
    return _normalized_metadata_path(base, value), identity


def _portable_repository_git_root(repo_root: Path) -> _RepositoryGitRootBinding:
    """Bind a primary or linked checkout to portable common Git metadata."""
    repo_identity = _portable_path_identity(repo_root, directory=True)
    marker = repo_root / ".git"
    marker_metadata = marker.lstat()
    if stat.S_ISDIR(marker_metadata.st_mode):
        common_identity = _portable_path_identity(marker, directory=True)
        return _RepositoryGitRootBinding(
            common_dir=marker,
            common_identity=common_identity,
            intake_admin_name=None,
            intake_admin_identity=None,
            structural_identity=(repo_identity, common_identity),
        )
    if not stat.S_ISREG(marker_metadata.st_mode):
        raise RuntimeError("repository root Git metadata is unsafe")
    pointer, marker_identity = _portable_read_git_pointer(marker)
    if not pointer.startswith("gitdir: "):
        raise RuntimeError("repository root gitfile is invalid")
    admin_dir, admin_identity = _portable_root_metadata_identity(
        repo_root,
        pointer.removeprefix("gitdir: "),
        directory=True,
        base_identity=repo_identity,
        error="repository root gitfile is invalid",
    )
    back_pointer, back_pointer_identity = _portable_read_git_pointer(admin_dir / "gitdir")
    back_path, back_identity = _portable_root_metadata_identity(
        admin_dir,
        back_pointer,
        directory=False,
        base_identity=admin_identity,
        error="repository root admin back-pointer changed",
    )
    if back_path != marker or back_identity != marker_identity:
        raise RuntimeError("repository root admin back-pointer changed")
    common_pointer, common_pointer_identity = _portable_read_git_pointer(admin_dir / "commondir")
    common_dir, common_identity = _portable_root_metadata_identity(
        admin_dir,
        common_pointer,
        directory=True,
        base_identity=admin_identity,
        error="repository root common directory changed",
    )
    if admin_dir.parent != common_dir / "worktrees":
        raise RuntimeError("repository root admin directory is unregistered")
    return _RepositoryGitRootBinding(
        common_dir=common_dir,
        common_identity=common_identity,
        intake_admin_name=admin_dir.name,
        intake_admin_identity=admin_identity,
        structural_identity=(
            repo_identity,
            marker_identity,
            admin_identity,
            back_pointer_identity,
            common_pointer_identity,
            common_identity,
        ),
    )


def _repository_git_root(
    repo_fd: int,
    repo_root: Path,
    repo_identity: _FilesystemIdentity,
) -> tuple[int, _RepositoryGitRootBinding]:
    """Bind a primary or linked checkout to common Git metadata."""
    marker_metadata = os.stat(".git", dir_fd=repo_fd, follow_symlinks=False)
    if stat.S_ISDIR(marker_metadata.st_mode):
        common_fd, common_identity = _open_directory_at_no_follow(repo_fd, ".git")
        return common_fd, _RepositoryGitRootBinding(
            common_dir=repo_root / ".git",
            common_identity=common_identity,
            intake_admin_name=None,
            intake_admin_identity=None,
            structural_identity=(repo_identity, common_identity),
        )
    if not stat.S_ISREG(marker_metadata.st_mode):
        raise RuntimeError("repository root Git metadata is unsafe")
    pointer, marker_identity = _read_bounded_git_pointer_at(repo_fd, ".git")
    if not pointer.startswith("gitdir: "):
        raise RuntimeError("repository root gitfile is invalid")
    admin_fd, admin_identity, admin_dir = _open_root_metadata_directory_no_follow(
        repo_root,
        pointer.removeprefix("gitdir: "),
        base_identity=repo_identity,
        error="repository root gitfile is invalid",
    )
    try:
        back_pointer, back_pointer_identity = _read_bounded_git_pointer_at(admin_fd, "gitdir")
        back_path, back_identity = _root_metadata_file_identity_no_follow(
            admin_dir,
            back_pointer,
            base_identity=admin_identity,
            error="repository root admin back-pointer changed",
        )
        if back_path != repo_root / ".git" or back_identity != marker_identity:
            raise RuntimeError("repository root admin back-pointer changed")
        common_pointer, common_pointer_identity = _read_bounded_git_pointer_at(
            admin_fd, "commondir"
        )
        common_fd, common_identity, common_dir = _open_root_metadata_directory_no_follow(
            admin_dir,
            common_pointer,
            base_identity=admin_identity,
            error="repository root common directory changed",
        )
    finally:
        os.close(admin_fd)
    if admin_dir.parent != common_dir / "worktrees":
        os.close(common_fd)
        raise RuntimeError("repository root admin directory is unregistered")
    return common_fd, _RepositoryGitRootBinding(
        common_dir=common_dir,
        common_identity=common_identity,
        intake_admin_name=admin_dir.name,
        intake_admin_identity=admin_identity,
        structural_identity=(
            repo_identity,
            marker_identity,
            admin_identity,
            back_pointer_identity,
            common_pointer_identity,
            common_identity,
        ),
    )


def _validate_registered_repository_admin(
    registry_fd: int,
    root: _RepositoryGitRootBinding,
) -> None:
    """Verify the linked repository root administration directory."""
    if root.intake_admin_name is None:
        return
    descriptor, identity = _open_directory_at_no_follow(
        registry_fd,
        root.intake_admin_name,
    )
    try:
        if identity != root.intake_admin_identity:
            raise RuntimeError("repository root admin directory changed")
    finally:
        os.close(descriptor)


def _portable_read_branch_ref(common_dir: Path, branch_ref: str) -> str:
    """Read one loose or packed branch through portable validated paths."""
    components = tuple(branch_ref.split("/"))
    windows_components = tuple(PureWindowsPath(component) for component in components)
    if (
        len(components) < 3
        or components[:2] != ("refs", "heads")
        or any(component in {"", ".", ".."} for component in components)
        or any(
            component.drive or component.root or component.parts != (value,)
            for value, component in zip(components, windows_components, strict=True)
        )
    ):
        raise RuntimeError("linked worktree branch reference is invalid")
    loose = common_dir.joinpath(*components)
    if not loose.is_relative_to(common_dir):
        raise RuntimeError("linked worktree branch reference is invalid")
    try:
        payload, _identity = _portable_read_bounded_regular(loose, max_bytes=4096)
    except FileNotFoundError:
        payload = b""
    if payload:
        try:
            value = payload.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeError("linked worktree branch reference is invalid") from exc
        if not _is_full_commit_sha(value):
            raise RuntimeError("linked worktree branch reference is invalid")
        return value
    try:
        packed, _identity = _portable_read_bounded_regular(
            common_dir / "packed-refs",
            max_bytes=IMPLEMENTATION_INSPECTION_METADATA_MAX_BYTES,
        )
    except FileNotFoundError:
        packed = b""
    try:
        packed_text = packed.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("linked worktree packed references are invalid") from exc
    matches = [
        line.partition(" ")[0]
        for line in packed_text.splitlines()
        if not line.startswith(("#", "^")) and line.partition(" ")[2] == branch_ref
    ]
    if len(matches) != 1 or not _is_full_commit_sha(matches[0]):
        raise RuntimeError("linked worktree branch reference is unavailable")
    return matches[0]


def _git_environment_for_binding(
    binding: _LinkedWorktreeBinding,
) -> _LinkedWorktreeGitEnvironment:
    """Return one controlled Git environment for a validated binding."""
    env = _isolated_checkout_git_env()
    env.update(
        {
            "GIT_DIR": str(binding.admin_dir),
            "GIT_COMMON_DIR": str(binding.common_dir),
            "GIT_INDEX_FILE": str(binding.index),
            "GIT_WORK_TREE": str(binding.worktree),
            "GIT_OBJECT_DIRECTORY": str(binding.objects),
        }
    )
    return _LinkedWorktreeGitEnvironment(env, binding)


def _linked_worktree_git_env_portable(
    repo_root: Path,
    worktree: Path,
) -> _LinkedWorktreeGitEnvironment:
    """Bind a linked worktree on a host without descriptor-relative traversal."""
    root = _portable_repository_git_root(repo_root)
    worktree_identity = _portable_path_identity(worktree, directory=True)
    common_dir = root.common_dir
    common_identity = root.common_identity
    registered_root = common_dir / "worktrees"
    registry_identity = _portable_path_identity(registered_root, directory=True)
    if root.intake_admin_name is not None:
        intake_admin_identity = _portable_path_identity(
            registered_root / root.intake_admin_name,
            directory=True,
        )
        if intake_admin_identity != root.intake_admin_identity:
            raise RuntimeError("repository root admin directory changed")
    pointer, marker_identity = _portable_read_git_pointer(worktree / ".git")
    if not pointer.startswith("gitdir: "):
        raise RuntimeError("linked worktree gitfile is invalid")
    admin_dir = _normalized_metadata_path(worktree, pointer.removeprefix("gitdir: "))
    if admin_dir.parent != registered_root:
        raise RuntimeError("linked worktree admin directory is unregistered")
    admin_identity = _portable_path_identity(admin_dir, directory=True)
    back_pointer, back_pointer_identity = _portable_read_git_pointer(admin_dir / "gitdir")
    if _normalized_metadata_path(admin_dir, back_pointer) != worktree / ".git":
        raise RuntimeError("linked worktree admin back-pointer changed")
    common_pointer, common_pointer_identity = _portable_read_git_pointer(admin_dir / "commondir")
    if _normalized_metadata_path(admin_dir, common_pointer) != common_dir:
        raise RuntimeError("linked worktree common directory changed")
    head_pointer, head_identity = _portable_read_git_pointer(admin_dir / "HEAD")
    if not head_pointer.startswith("ref: "):
        raise RuntimeError("linked worktree HEAD is detached")
    branch_ref = head_pointer.removeprefix("ref: ")
    branch_sha = _portable_read_branch_ref(common_dir, branch_ref)
    index = admin_dir / "index"
    index_identity = _portable_path_identity(index, directory=False)
    objects = common_dir / "objects"
    objects_identity = _portable_path_identity(objects, directory=True)
    _reject_portable_object_alternates(objects)
    binding = _LinkedWorktreeBinding(
        repo_root=repo_root,
        worktree=worktree,
        common_dir=common_dir,
        admin_dir=admin_dir,
        index=index,
        objects=objects,
        branch_ref=branch_ref,
        branch_sha=branch_sha,
        common_identity=common_identity,
        index_identity=index_identity,
        structural_identity=(
            *root.structural_identity,
            worktree_identity,
            registry_identity,
            admin_identity,
            marker_identity,
            back_pointer_identity,
            common_pointer_identity,
            head_identity,
            objects_identity,
        ),
    )
    return _git_environment_for_binding(binding)


def _linked_worktree_git_env(
    repo_root: Path,
    worktree: Path,
) -> _LinkedWorktreeGitEnvironment:
    """Bind Git commands to one registered linked-worktree metadata graph."""
    if not _secure_dir_fd_supported():
        return _linked_worktree_git_env_portable(repo_root, worktree)
    repo_fd, repo_identity = _open_directory_no_follow(repo_root)
    descriptors = [repo_fd]
    try:
        worktree_fd, worktree_identity = _open_directory_no_follow(worktree)
        descriptors.append(worktree_fd)
        common_fd, root = _repository_git_root(repo_fd, repo_root, repo_identity)
        descriptors.append(common_fd)
        common_dir = root.common_dir
        common_identity = root.common_identity
        registry_fd, registry_identity = _open_directory_at_no_follow(common_fd, "worktrees")
        descriptors.append(registry_fd)
        _validate_registered_repository_admin(registry_fd, root)
        pointer, marker_identity = _read_bounded_git_pointer_at(worktree_fd, ".git")
        prefix = "gitdir: "
        if not pointer.startswith(prefix):
            raise RuntimeError("linked worktree gitfile is invalid")
        registered_root = common_dir / "worktrees"
        raw_admin = Path(pointer.removeprefix(prefix))
        if not raw_admin.is_absolute():
            raw_admin = worktree / raw_admin
        admin_dir = raw_admin.resolve(strict=True)
        if admin_dir.parent != registered_root or raw_admin.is_symlink():
            raise RuntimeError("linked worktree admin directory is unregistered")
        admin_fd, admin_identity = _open_directory_at_no_follow(registry_fd, admin_dir.name)
        descriptors.append(admin_fd)
        back_pointer, back_pointer_identity = _read_bounded_git_pointer_at(admin_fd, "gitdir")
        raw_back_pointer = Path(back_pointer)
        if not raw_back_pointer.is_absolute():
            raw_back_pointer = admin_dir / raw_back_pointer
        if raw_back_pointer.resolve(strict=True) != worktree / ".git":
            raise RuntimeError("linked worktree admin back-pointer changed")
        common_pointer, common_pointer_identity = _read_bounded_git_pointer_at(
            admin_fd, "commondir"
        )
        raw_common = Path(common_pointer)
        if not raw_common.is_absolute():
            raw_common = admin_dir / raw_common
        if raw_common.resolve(strict=True) != common_dir:
            raise RuntimeError("linked worktree common directory changed")
        head_pointer, head_identity = _read_bounded_git_pointer_at(admin_fd, "HEAD")
        head_prefix = "ref: "
        if not head_pointer.startswith(head_prefix):
            raise RuntimeError("linked worktree HEAD is detached")
        branch_ref = head_pointer.removeprefix(head_prefix)
        branch_sha = _read_branch_ref(common_fd, common_dir, branch_ref)
        index_identity = _regular_file_identity_at(admin_fd, "index")
        objects_fd, objects_identity = _open_directory_at_no_follow(common_fd, "objects")
        descriptors.append(objects_fd)
        _reject_object_alternates_at(objects_fd)
        index = admin_dir / "index"
        objects = common_dir / "objects"
        binding = _LinkedWorktreeBinding(
            repo_root=repo_root,
            worktree=worktree,
            common_dir=common_dir,
            admin_dir=admin_dir,
            index=index,
            objects=objects,
            branch_ref=branch_ref,
            branch_sha=branch_sha,
            common_identity=common_identity,
            index_identity=index_identity,
            structural_identity=(
                *root.structural_identity,
                worktree_identity,
                registry_identity,
                admin_identity,
                marker_identity,
                back_pointer_identity,
                common_pointer_identity,
                head_identity,
                objects_identity,
            ),
        )
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return _git_environment_for_binding(binding)


@contextmanager
def _private_linked_worktree_git_env(
    linked_env: dict[str, str],
    *,
    detached_head: str,
    durable_git_dir: Path | None = None,
) -> Iterator[dict[str, str]]:
    """Use private Git metadata with the validated real index and object store."""
    if not _is_full_commit_sha(detached_head):
        raise RuntimeError("private Git metadata HEAD is invalid")
    with ExitStack() as stack:
        if durable_git_dir is None:
            temporary = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="hephaestus-recovery-git-")
            )
            git_dir = Path(temporary) / "git"
            git_dir.mkdir(mode=0o700)
        else:
            git_dir = durable_git_dir.resolve(strict=True)
            if durable_git_dir.is_symlink() or not git_dir.is_dir():
                raise RuntimeError("durable private Git metadata is invalid")
        (git_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        (git_dir / "refs" / "tags").mkdir(parents=True, exist_ok=True)
        head_path = git_dir / "HEAD"
        if head_path.exists():
            if (
                head_path.is_symlink()
                or head_path.read_text(encoding="ascii").strip() != detached_head
            ):
                raise RuntimeError("durable private Git HEAD changed")
        else:
            write_secure(head_path, f"{detached_head}\n", permissions=0o600)
        if len(detached_head) == 64:
            write_secure(
                git_dir / "config",
                "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = sha256\n",
                permissions=0o600,
            )
        binding = getattr(linked_env, "binding", None)
        if isinstance(binding, _LinkedWorktreeBinding):
            rebound = _linked_worktree_git_env(binding.repo_root, binding.worktree)
            if not _linked_binding_matches(linked_env, rebound, include_index=True):
                kind = "durable" if durable_git_dir is not None else "temporary"
                raise RuntimeError(
                    f"linked worktree metadata changed before {kind} private Git use "
                    f"({binding.index_identity!r} != {rebound.binding.index_identity!r})"
                )
            linked_env = rebound
            binding = rebound.binding
        objects = (
            binding.objects
            if isinstance(binding, _LinkedWorktreeBinding)
            else Path(linked_env["GIT_COMMON_DIR"]) / "objects"
        )
        env = dict(linked_env)
        env.update(
            {
                "GIT_DIR": str(git_dir),
                "GIT_COMMON_DIR": str(git_dir),
                "GIT_OBJECT_DIRECTORY": str(objects),
                "GIT_OPTIONAL_LOCKS": "0",
            }
        )
        env.pop("GIT_CONFIG", None)
        yield _PrivateLinkedWorktreeGitEnvironment(env, linked_env)


def _linked_binding_matches(
    expected_env: dict[str, str],
    current_env: dict[str, str],
    *,
    include_index: bool,
) -> bool:
    """Return whether two environments bind to the same metadata objects."""
    expected = getattr(expected_env, "binding", None)
    current = getattr(current_env, "binding", None)
    if isinstance(expected, _LinkedWorktreeBinding) and isinstance(current, _LinkedWorktreeBinding):
        return (
            expected.repo_root == current.repo_root
            and expected.worktree == current.worktree
            and expected.common_dir == current.common_dir
            and expected.admin_dir == current.admin_dir
            and expected.index == current.index
            and expected.objects == current.objects
            and expected.branch_ref == current.branch_ref
            and expected.structural_identity == current.structural_identity
            and (not include_index or expected.index_identity == current.index_identity)
        )
    keys = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")
    return all(expected_env.get(key) == current_env.get(key) for key in keys)
