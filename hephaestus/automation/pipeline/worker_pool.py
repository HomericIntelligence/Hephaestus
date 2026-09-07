"""Worker pool: the only place agent, build/test, git, GitHub, and session work runs.

The coordinator submits frozen jobs and drains ``(handle, result)`` tuples from
the completion queue. Workers never touch WorkItems or stage queues and never
touch coordinator state. Closed GitHub jobs use a separately injected runner.
"""

from __future__ import annotations

import csv
import ctypes
import hashlib
import io
import json
import logging
import os
import queue as queue_mod
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Collection, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, suppress
from contextvars import copy_context
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Any, TypeGuard, cast

import hephaestus.agents.runtime as agent_runtime
import hephaestus.automation.claude_invoke as claude_invoke
import hephaestus.automation.codex_adapter_admission as codex_adapter_admission
import hephaestus.automation.git_utils as git_utils
import hephaestus.automation.pipeline.codex_worktree_boundary as codex_worktree_boundary
import hephaestus.automation.subprocess_registry as subprocess_registry
from hephaestus.agents.codex_isolation import (
    CodexExecutionPolicyV1,
    CodexGitReceiptV1,
    CodexIsolationAdapterV1,
    CodexIsolationError,
    CodexIsolationRequestV1,
    StagedLinuxExecutable,
    canonical_sha256,
    close_staged_linux_executable,
    new_run_nonce,
    stage_linux_executable,
)
from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionPolicyError,
    ExecutionRequest,
    FilesystemMode,
    SessionLifecycle,
    resolve_policy,
)
from hephaestus.agents.model_selection import resolve_codex_model_selection
from hephaestus.agents.pi_session import AgentSessionBinding, PiSessionBindingError
from hephaestus.agents.runtime import (
    AgentExecutionError,
    resolve_agent,
    resume_agent_session,
    run_agent_session,
    run_agent_text,
    uses_direct_agent_runner,
    validate_agent_execution_support,
)
from hephaestus.agents.session_errors import AgentSessionLostError
from hephaestus.agents.workspace import WorkspaceKind, validate_workspace_binding
from hephaestus.automation.commit_paths import CommitPaths, is_bounded_commit_paths
from hephaestus.automation.implementation_writer import ImplementationWriterHandoff
from hephaestus.automation.learn import compact_agent_session
from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.automation.pipeline.athena_skill_jobs import (
    AthenaSkillExecutor,
    AthenaSkillJob,
    AthenaSkillResult,
)
from hephaestus.automation.pipeline.git_jobs import (
    DIRTY_SNAPSHOT_CHANGED_FILE_MAX,
    DIRTY_SNAPSHOT_CONTENT_MAX_BYTES,
    DIRTY_SNAPSHOT_GIT_MAX_BYTES,
    IMPLEMENTATION_INSPECTION_DIFF_MAX_BYTES,
    IMPLEMENTATION_INSPECTION_METADATA_MAX_BYTES,
    IMPLEMENTATION_INSPECTION_STATUS_MAX_BYTES,
)
from hephaestus.automation.pipeline.github_jobs import (
    GitHubJob,
    GitHubJobRunner,
)
from hephaestus.automation.pipeline.jobs import (
    WORKTREE_MATERIALIZED_KEY,
    AgentJob,
    BuildTestJob,
    CompactJob,
    GitJob,
    JobHandle,
    JobResult,
    validate_job_workspace,
)
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.rebase_policy import (
    RebasePolicySelector,
    RebaseValidationPolicy,
)
from hephaestus.automation.pipeline.reply_handoff import (
    implementation_remediation_reply_handoff,
    implementation_remediation_reply_handoff_journal_entry,
)
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.scope_retraction import is_safe_scope_retraction_path
from hephaestus.automation.pipeline.tool_scopes import (
    DEFAULT_TOOL_SCOPE,
    ToolScope,
    tool_scope_for,
)
from hephaestus.automation.prompts._review_rubric import plugin_skills_context
from hephaestus.automation.remediation_prepublication import (
    load_prepublication_intent,
    load_prepublication_receipt,
    prepublication_private_git_dir,
    read_prepublication_private_head,
    save_prepublication_intent,
    save_prepublication_receipt,
)
from hephaestus.automation.remediation_recovery import (
    RemediationRecoveryReceipt,
    RemediationReplyResult,
    RemediationReviewInput,
    encode_remediation_review_input,
)
from hephaestus.automation.remote_git import (
    trusted_gh_executable as _shared_trusted_gh_executable,
    trusted_remote_git_config as _shared_trusted_remote_git_config,
)
from hephaestus.automation.review_journal import CommentJournalReadError
from hephaestus.automation.session_naming import AGENT_COMMIT_MESSAGE
from hephaestus.automation.source_worktree import (
    SourceWorkspaceError,
    SourceWorkspaceManager,
    SourceWorkspaceRecovery,
)
from hephaestus.automation.verified_runner import build_verified_runner_argv
from hephaestus.automation.worktree_manager import (
    BRANCH_WORKTREE_OWNED,
    BranchWorktreeOwnedError,
    ImplementationWriterAuthority,
    RemoteGitRefreshError,
    WorktreeCreationReceiptError,
    WorktreeManager,
)
from hephaestus.config.child_environments import (
    build_codex_implementation_child_env,
    build_git_child_env,
    build_git_signing_env,
    build_host_verification_env,
    build_python_phase_env,
    read_approved_parent_env,
)
from hephaestus.diagnostics import bounded_git_diagnostic
from hephaestus.github.client import GitHubRateLimitError, GitHubUnavailableError
from hephaestus.io.utils import write_secure
from hephaestus.resilience import (
    CircuitBreakerOpenError,
    resilient_call,
)
from hephaestus.utils.file_lock import LockUnavailableError, file_lock
from hephaestus.utils.git import _is_full_commit_sha
from hephaestus.utils.helpers import get_repo_root
from hephaestus.utils.worktree_identity import source_worktree_name

logger = logging.getLogger(__name__)

_TAIL = 4000  # chars of stdout/stderr retained in a JobResult
_ERR_MAX = 500  # chars of error detail retained in a JobResult
_GIT_LOCK_WAIT_POLL_S = 0.1
_DIRTY_CONTENT_SNAPSHOT_KEYS = frozenset({"index_sha256", "worktree_sha256", "untracked_sha256"})
_CODEX_IMPLEMENTATION_OUTPUT_MAX_BYTES = 1024 * 1024
_CODEX_IMPLEMENTATION_GRACE_SECONDS = 5.0
_CODEX_IMPLEMENTATION_INVENTORY_QUIESCENCE_SECONDS = 1.0
_CODEX_IMPLEMENTATION_PROVIDER_RELAY = "vsock://2:443"


def _remediation_review_input(
    job: GitJob,
    *,
    repo_root: Path,
    worktree: Path,
    branch: str,
    parent_sha: str,
    candidate_tree_sha: str,
    recovery_commit_sha: str,
    paths: CommitPaths,
    committed_diff: str,
    committed_diff_sha256: str,
) -> RemediationReviewInput:
    """Build the exact immutable input for one prepared recovery commit."""
    repository = job.kwargs.get("remediation_repository")
    issue_number = job.kwargs.get("issue_number")
    pr_number = job.kwargs.get("remediation_pr_number")
    diagnostic = job.kwargs.get("remediation_failure_diagnostic")
    threads = job.kwargs.get("remediation_thread_snapshots")
    if (
        not isinstance(repository, str)
        or isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or isinstance(pr_number, bool)
        or not isinstance(pr_number, int)
        or not isinstance(diagnostic, str)
        or not isinstance(threads, list)
    ):
        raise ValueError("remediation recovery input is incomplete")
    return RemediationReviewInput(
        format_version=3,
        repository=repository,
        issue_number=issue_number,
        pr_number=pr_number,
        repo_root=str(repo_root),
        worktree_path=str(worktree),
        branch=branch,
        reviewed_parent_sha=parent_sha,
        candidate_tree_sha=candidate_tree_sha,
        recovery_commit_sha=recovery_commit_sha,
        changed_paths=(*paths.add_paths, *paths.update_paths),
        committed_diff_sha256=committed_diff_sha256,
        committed_diff=committed_diff,
        failure_diagnostic=diagnostic,
        thread_snapshot_sha256=RemediationReviewInput.thread_snapshot_digest(threads),
        thread_snapshot_json=RemediationReviewInput.canonical_thread_snapshot(threads),
    )


def _remediation_recovery_artifacts(
    job: GitJob,
    *,
    repo_root: Path,
    worktree: Path,
    branch: str,
    parent_sha: str,
    candidate_tree_sha: str,
    recovery_commit_sha: str,
    paths: CommitPaths,
    committed_diff: str,
    committed_diff_sha256: str,
) -> tuple[dict[str, Any], tuple[str, str]]:
    """Build and verify the complete durable record before publication."""
    review_input = _remediation_review_input(
        job,
        repo_root=repo_root,
        worktree=worktree,
        branch=branch,
        parent_sha=parent_sha,
        candidate_tree_sha=candidate_tree_sha,
        recovery_commit_sha=recovery_commit_sha,
        paths=paths,
        committed_diff=committed_diff,
        committed_diff_sha256=committed_diff_sha256,
    )
    replies = job.kwargs.get("remediation_replies")
    batch_nonce = job.kwargs.get("remediation_batch_nonce")
    if (
        not isinstance(replies, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in replies.items()
        )
        or not isinstance(batch_nonce, str)
    ):
        raise ValueError("remediation recovery reply input is incomplete")
    reply_result = RemediationReplyResult.create(
        review_input_sha256=review_input.review_input_sha256,
        replies=cast(dict[str, str], replies),
        thread_snapshot_json=review_input.thread_snapshot_json,
    )
    handoff = implementation_remediation_reply_handoff(
        review_input,
        reply_result,
        batch_nonce,
        journal_input_encoding=job.kwargs.get("remediation_journal_input_encoding"),
        journal_input_data=job.kwargs.get("remediation_journal_input_data"),
    )
    journal = implementation_remediation_reply_handoff_journal_entry(
        review_input.pr_number,
        handoff,
    )
    if handoff is None or journal is None:
        raise ValueError("remediation recovery journal is not encodable")
    return handoff, journal


def _invoke_claude_commit_message(
    issue_number: int,
    prompt: str,
    worktree_path: Path,
    agent: str,
    timeout: int,
    model: str,
    pi_dir: Path | None,
) -> str:
    """Run a commit-message request from the approved worker adapter."""
    remaining_s = cast(float, git_utils.remaining_operation_timeout(timeout))
    if remaining_s < 1:
        raise subprocess.TimeoutExpired("commit-message operation deadline", 0)
    timeout = min(timeout, int(remaining_s))
    if uses_direct_agent_runner(agent):
        result = run_agent_text(
            agent,
            prompt,
            cwd=worktree_path,
            timeout=timeout,
            model=model,
            sandbox="read-only",
            approval="never",
            execution_request=ExecutionRequest(
                AgentRole.IMPLEMENTER,
                AgentOperation.GIT_MESSAGE,
                SessionLifecycle.ONE_SHOT,
            ),
            pi_dir=pi_dir,
        )
        return (result.stdout or "").strip()
    stdout, _ = claude_invoke.invoke_claude_with_session(
        repo=git_utils.get_repo_slug(worktree_path),
        issue=issue_number,
        agent=AGENT_COMMIT_MESSAGE,
        prompt=prompt,
        model=model,
        cwd=worktree_path,
        timeout=timeout,
        output_format="text",
        allowed_tools="Read,Glob,Grep",
    )
    return (stdout or "").strip()


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
    relative_values = tuple(value for value in paths_output.split("\0") if value)
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
                relative_path.is_absolute()
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
                    if copy_root is not None and not stat.S_ISDIR(metadata.st_mode):
                        raise RuntimeError("candidate tree contains an unsupported path type")
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


def _bounded_candidate_commit_paths(
    worktree: Path,
    head: str,
    *,
    timeout: int,
    git_env: dict[str, str] | None = None,
) -> CommitPaths:
    """Return one bounded, filtered path manifest for candidate staging."""
    from hephaestus.automation.commit_paths import (
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


def _classify_github_failure(error: BaseException, *, now_epoch: float) -> tuple[str, float] | None:
    """Return a safe GitHub failure class and its first retry delay."""
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and all(current is not seen for seen in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    rate_error = next((item for item in chain if isinstance(item, GitHubRateLimitError)), None)
    breaker_error = next(
        (item for item in chain if isinstance(item, CircuitBreakerOpenError)), None
    )
    if isinstance(rate_error, GitHubRateLimitError):
        delay = (
            max(0.0, float(rate_error.reset_epoch) - now_epoch)
            if rate_error.reset_epoch > 0
            else 60.0
        )
        return "github_rate_limit", delay
    if any(isinstance(item, GitHubUnavailableError) for item in chain) or isinstance(
        breaker_error, CircuitBreakerOpenError
    ):
        delay = (
            max(0.0, breaker_error.time_until_recovery)
            if isinstance(breaker_error, CircuitBreakerOpenError)
            else 60.0
        )
        return "github_unavailable", delay
    if any(isinstance(item, subprocess.CalledProcessError) for item in chain):
        return "github_cli_error", 1.0
    if any(isinstance(item, (subprocess.TimeoutExpired, _GitLockTimeoutError)) for item in chain):
        return "github_timeout", 1.0
    if any(isinstance(item, CommentJournalReadError) for item in chain):
        return "comment_journal_read_error", 1.0
    return None


@contextmanager
def _agent_workspace_lease(job: AgentJob) -> Iterator[Path]:
    """Validate a job and hold its source-lane lock for the provider call."""
    cwd = validate_job_workspace(job)
    binding = job.workspace
    if binding is None or binding.kind is not WorkspaceKind.SOURCE:
        yield cwd
        return
    if binding.reusable_root is None or binding.repository is None:
        raise RuntimeError("source workspace binding is incomplete")
    manager = SourceWorkspaceManager(
        binding.reusable_root,
        repository=binding.repository,
        base_dir=binding.cwd.parent,
    )
    with manager.acquire(binding, allowed_tools=job.allowed_tools or "") as leased:
        yield leased


@contextmanager
def _athena_workspace_lease(job: AthenaSkillJob) -> Iterator[Path]:
    """Validate and lease a host-owned skill request workspace."""
    request = job.request
    binding = request.workspace
    if binding is None:
        canonical = request.cwd.resolve() if request.cwd.exists() else request.cwd.absolute()
        if (canonical / ".git").is_dir():
            raise RuntimeError(
                "source-reading Athena skill cannot use the reusable repository root"
            )
        yield request.cwd
        return
    cwd = validate_workspace_binding(binding, allowed_tools="Read,Glob,Grep")
    if cwd != request.cwd.resolve(strict=True):
        raise RuntimeError("Athena request cwd does not match its workspace binding")
    if binding.kind is not WorkspaceKind.SOURCE:
        yield cwd
        return
    if binding.reusable_root is None or binding.repository is None:
        raise RuntimeError("source workspace binding is incomplete")
    manager = SourceWorkspaceManager(
        binding.reusable_root,
        repository=binding.repository,
        base_dir=binding.cwd.parent,
    )
    with manager.acquire(binding, allowed_tools="Read,Glob,Grep") as leased:
        yield leased


_FETCH_ENV_BLOCKLIST = frozenset(
    {
        "GIT_ASKPASS",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_EXEC_PATH",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSL_CAINFO",
        "GIT_SSL_CAPATH",
        "GIT_SSL_NO_VERIFY",
        "GIT_WORK_TREE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "SSH_ASKPASS",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    }
)

# ``gh`` must not be discovered through a caller-controlled ``PATH``: the
# checkout synchronizer executes it as the GitHub credential helper.  These
# are the system and package-manager locations we support for the automation
# host.  Resolving candidates also rejects a symlink that escapes its trusted
# installation root.
_TRUSTED_GH_CANDIDATES = (
    Path("/opt/homebrew/bin/gh"),
    Path("/usr/local/bin/gh"),
    Path("/usr/bin/gh"),
)
_TRUSTED_GH_ROOTS = (Path("/opt/homebrew"), Path("/usr/local"), Path("/usr"))
_TRUSTED_UV_CANDIDATES = (
    Path("/opt/homebrew/bin/uv"),
    Path("/usr/local/bin/uv"),
    Path.home() / ".local/bin/uv",
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
_HOST_RUNTIME_CACHE_DIRNAME = "hephaestus-host-validation-runtime"
_HOST_RUNTIME_CACHE_FORMAT = b"sealed-runtime-v6-shell-launchers"
_HOST_RUNTIME_MANIFEST_HEADER = "sealed-runtime-file-manifest-v1"


# The host verification handles code from an untrusted pull request.  Bound
# the Git archive before extraction and bound every child output/write path.
_HOST_VERIFICATION_ARCHIVE_MAX_BYTES = 64 * 1024 * 1024
_HOST_VERIFICATION_ARCHIVE_MAX_MEMBERS = 20_000
# Full unit coverage includes package-lifecycle tests that build wheels and
# sdists alongside coverage data. Keep that bounded workload below a fixed
# 512 MiB quota; the measured nested-runtime fixture peaks near 234 MiB, so
# smaller HFS+ images leave insufficient filesystem and SQLite headroom.
_HOST_VERIFICATION_SCRATCH_MAX_BYTES = 512 * 1024 * 1024
# macOS ``ulimit -f`` uses 512-byte blocks. Coverage's SQLite data file exceeds
# 1 MiB for the full unit suite, so retain a per-file ceiling with enough room
# for that verifier-owned artifact. The separately mounted 512 MiB volume is
# still the non-bypassable aggregate quota for every PR-visible write.
_HOST_VERIFICATION_OUTPUT_FILE_MAX_BLOCKS = 131_072
_HOST_VERIFICATION_CPU_MAX_S = 240
_HOST_VERIFICATION_PROCESS_HEADROOM = 64
_HOST_VERIFICATION_POLL_S = 0.05
_HOST_VERIFICATION_SETUP_TIMEOUT_S = 30


def _agent_exception_result(exc: Exception) -> JobResult:
    """Map provider-declared and unexpected agent exceptions to job results."""
    if isinstance(exc, AgentExecutionError):
        return JobResult(
            ok=False,
            error=f"agent_error: {exc!s}"[:_ERR_MAX],
        )
    logger.exception("Agent job raised, returning error result")
    return JobResult(
        ok=False,
        error=f"{type(exc).__name__}: {exc!s}"[:_ERR_MAX],
    )


class _HostVerificationBoundaryError(RuntimeError):
    """Raised when a host verification cannot keep PR code contained."""


class _RebaseSigningEnvironmentError(RuntimeError):
    """Raised when a policy rebase cannot obtain the validated signing bridge."""


class _RemoteGitAuthenticationError(RuntimeError):
    """Raised when a remote Git operation cannot use trusted authentication."""


def _sandbox_string(path: Path) -> str:
    """Canonicalize and quote a filesystem path for a sandbox profile literal."""
    # macOS presents /var as a symlink to /private/var, but sandbox rules match
    # the physical path. A lexical temporary-directory path would otherwise
    # deny the declared snapshot's current working directory.
    return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')


def _host_verification_env(
    scratch: Path,
    executable: str,
    runtime_environment: Path,
    git_executable: str | None = None,
) -> dict[str, str]:
    """Build the minimal disposable environment for host verification.

    Deliberately do not inherit the automation process environment: a PR test
    must not receive GitHub, package-index, or cloud credentials by accident.
    The executable is resolved before this point, so ``PATH`` only needs its
    containing directory and the platform defaults.
    """
    # Keep environment paths consistent with the physical paths granted to
    # sandbox-exec; on macOS, /var is an alias for /private/var.
    scratch = scratch.resolve()
    runtime_environment = runtime_environment.resolve()
    home = scratch / "home"
    temporary = scratch / "tmp"
    cache = scratch / "cache"
    for directory in (home, temporary, cache):
        directory.mkdir(parents=True, exist_ok=True)

    return build_host_verification_env(
        home=home,
        temporary=temporary,
        cache=cache,
        runtime_environment=runtime_environment,
        executable=Path(executable),
        git_executable=Path(git_executable) if git_executable else None,
    )


def _host_runtime_fingerprint(runtime: Path) -> str:
    """Return a stable cache key for a host process's installed runtime."""
    hasher = hashlib.sha256()
    hasher.update(_HOST_RUNTIME_CACHE_FORMAT)
    hasher.update(sys.version.encode())
    try:
        hasher.update((runtime / "pyvenv.cfg").read_bytes())
    except OSError:
        hasher.update(str(runtime).encode())
    manifests = sorted(
        (
            *runtime.rglob("*.dist-info/RECORD"),
            *runtime.rglob("*.egg-info/PKG-INFO"),
        ),
        key=lambda path: path.as_posix(),
    )
    for manifest in manifests:
        hasher.update(manifest.relative_to(runtime).as_posix().encode())
        hasher.update(b"\0")
        hasher.update(manifest.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def _seal_host_runtime(runtime: Path) -> None:
    """Remove write bits without following runtime symlinks."""
    for path in (runtime, *runtime.rglob("*")):
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        path.chmod(mode & ~0o222)


def _rewrite_runtime_launchers(runtime: Path, source_runtime: Path) -> None:
    """Point copied console-script shebangs at the sealed runtime.

    A uv-managed environment records its original ``.venv`` path in console
    scripts such as ``bin/mypy``. The verifier copies that environment outside
    the mutable checkout, so those launchers must name the copied interpreter
    before the runtime is sealed.
    """
    source_path = str(source_runtime.resolve()).encode()
    target_path = str(runtime.resolve()).encode()
    source_prefix = b"#!" + source_path
    target_prefix = b"#!" + target_path
    shell_source_prefix = b"'''exec' '" + source_path + b"/"
    shell_target_prefix = b"'''exec' '" + target_path + b"/"
    for launcher in (runtime / "bin").iterdir():
        if launcher.is_symlink() or not launcher.is_file():
            continue
        try:
            content = launcher.read_bytes()
        except OSError:
            continue
        first_line, separator, remainder = content.partition(b"\n")
        if first_line.startswith(source_prefix):
            launcher.write_bytes(
                target_prefix + first_line[len(source_prefix) :] + separator + remainder
            )
            continue
        if first_line != b"#!/bin/sh":
            continue
        trampoline, trampoline_separator, script = remainder.partition(b"\n")
        if not (
            trampoline.startswith(shell_source_prefix) and trampoline.endswith(b'\' "$0" "$@"')
        ):
            continue
        launcher.write_bytes(
            first_line
            + separator
            + shell_target_prefix
            + trampoline[len(shell_source_prefix) :]
            + trampoline_separator
            + script
        )


def _sealed_runtime_marker(target: Path) -> Path:
    """Return the immutable completion marker for a cached runtime."""
    return target.with_name(f"{target.name}.sealed")


def _sealed_runtime_manifest(target: Path) -> Path:
    """Return the verifier-owned file manifest for a cached runtime."""
    return target.with_name(f"{target.name}.manifest")


def _sealed_runtime_marker_matches(target: Path) -> bool:
    """Return whether *target* has this verifier's structurally valid marker."""
    marker = _sealed_runtime_marker(target)
    try:
        return (
            target.is_dir()
            and not target.is_symlink()
            and marker.is_file()
            and not marker.is_symlink()
            and marker.read_text(encoding="utf-8") == f"{target.name}\n"
            and not (marker.stat().st_mode & 0o222)
        )
    except OSError:
        return False


def _runtime_manifest_entries(runtime: Path) -> tuple[str, ...]:
    """Return verifier-owned relative paths that must exist in a runtime cache."""
    root = runtime.resolve()
    entries: set[str] = set()
    for required in (runtime / "pyvenv.cfg", runtime / "bin" / "python"):
        candidate = required.resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise FileNotFoundError(required)
        entries.add(candidate.relative_to(root).as_posix())
    for path in runtime.rglob("*"):
        if not path.is_file():
            continue
        candidate = path.resolve()
        if not candidate.is_relative_to(root):
            raise FileNotFoundError(path)
        entries.add(candidate.relative_to(root).as_posix())
    return tuple(sorted(entries))


def _write_sealed_runtime_manifest(target: Path) -> None:
    """Persist the expected runtime-cache files outside the sealed directory."""
    content = io.StringIO()
    writer = csv.writer(content, lineterminator="\n")
    writer.writerow([_HOST_RUNTIME_MANIFEST_HEADER, target.name])
    for entry in _runtime_manifest_entries(target):
        writer.writerow([entry])
    write_secure(
        _sealed_runtime_manifest(target),
        content.getvalue(),
        permissions=0o400,
    )


def _sealed_runtime_manifest_matches(target: Path) -> bool:
    """Return whether the verifier-owned manifest matches files in *target*."""
    manifest_path = _sealed_runtime_manifest(target)
    try:
        root = target.resolve()
        if (
            not manifest_path.is_file()
            or manifest_path.is_symlink()
            or manifest_path.stat().st_mode & 0o222
        ):
            return False
        with manifest_path.open(encoding="utf-8", newline="") as manifest:
            reader = csv.reader(manifest)
            if next(reader, None) != [_HOST_RUNTIME_MANIFEST_HEADER, target.name]:
                return False
            for row in reader:
                if len(row) != 1 or not row[0]:
                    return False
                candidate = (root / row[0]).resolve()
                if not candidate.is_relative_to(root) or not candidate.is_file():
                    return False
    except (OSError, csv.Error):
        return False
    return True


def _is_sealed_runtime_cache(target: Path) -> bool:
    """Return whether *target* is marked, sealed, and manifest-complete."""
    return _sealed_runtime_marker_matches(target) and _sealed_runtime_manifest_matches(target)


def _remove_corrupted_sealed_runtime(target: Path) -> None:
    """Remove a marked cache after making its sealed directories removable."""
    for path in (target, *target.rglob("*")):
        if path.is_symlink():
            continue
        path.chmod(path.stat().st_mode | (0o700 if path.is_dir() else 0o600))
    shutil.rmtree(target)
    _sealed_runtime_marker(target).unlink()
    _sealed_runtime_manifest(target).unlink(missing_ok=True)


def _verifier_owned_runtime_environment(checkout: Path) -> Path:
    """Return a read-only runtime outside the mutable review checkout.

    The worker environment can be inside or outside the checkout.  In either
    case, snapshot it once into the user-private temp area, seal it, and use
    only that external copy for immutable host validation.  This prevents the
    sandboxed command from resolving a live worker ``.venv`` path and ensures
    the verifier has one consistent read-only runtime contract.
    """
    runtime = Path(sys.prefix).resolve()
    try:
        checkout.resolve()
    except OSError as exc:
        raise _HostVerificationBoundaryError("host_verification_runtime_unavailable") from exc

    cache_root = Path(tempfile.gettempdir()) / _HOST_RUNTIME_CACHE_DIRNAME
    if cache_root.is_symlink():
        raise _HostVerificationBoundaryError("host_verification_runtime_cache_unsafe")
    cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache_root.chmod(0o700)
    target = cache_root / _host_runtime_fingerprint(runtime)
    if _is_sealed_runtime_cache(target):
        return target
    lock_path = cache_root / f"{target.name}.lock"
    try:
        with file_lock(lock_path, require_exclusive=True):
            if _is_sealed_runtime_cache(target):
                return target
            if target.exists() or target.is_symlink():
                if _sealed_runtime_marker_matches(target):
                    _remove_corrupted_sealed_runtime(target)
                else:
                    raise _HostVerificationBoundaryError("host_verification_runtime_cache_unsafe")
            staging = Path(tempfile.mkdtemp(prefix="runtime-", dir=cache_root))
            copied = staging / "environment"
            try:
                # A uv environment's Python launcher is commonly an absolute
                # symlink. Preserve the environment boundary by dereferencing
                # it into the cache; otherwise UV resolves it back to the
                # mutable host interpreter rather than this sealed snapshot.
                shutil.copytree(runtime, copied, symlinks=False)
                copied.replace(target)
                try:
                    _rewrite_runtime_launchers(target, runtime)
                    _write_sealed_runtime_manifest(target)
                    _seal_host_runtime(target)
                    write_secure(
                        _sealed_runtime_marker(target),
                        f"{target.name}\n",
                        permissions=0o400,
                    )
                    if not _is_sealed_runtime_cache(target):
                        raise OSError("sealed runtime cache failed validation")
                except OSError:
                    _sealed_runtime_manifest(target).unlink(missing_ok=True)
                    _sealed_runtime_marker(target).unlink(missing_ok=True)
                    shutil.rmtree(target, ignore_errors=True)
                    raise
            finally:
                shutil.rmtree(staging, ignore_errors=True)
    except _HostVerificationBoundaryError:
        raise
    except (OSError, RuntimeError, LockUnavailableError) as exc:
        raise _HostVerificationBoundaryError("host_verification_runtime_prepare_failed") from exc
    return target


def _host_verification_profile(
    *,
    source: Path,
    scratch: Path,
    runtime_environment: Path,
    git_metadata: Path,
    pi_smoke_logs: Path,
    executable: Path,
) -> str:
    """Build the macOS profile; only the declared scratch tree is writable."""
    allowed_roots = (
        Path("/bin"),
        Path("/sbin"),
        Path("/usr"),
        Path("/System"),
        Path("/opt/homebrew"),
        Path("/usr/local"),
    )
    canonical_tmp = Path(os.path.sep) / "tmp"
    return "\n".join(
        (
            "(version 1)",
            "(deny default)",
            # ``system.sb`` supplies the macOS runtime IPC, loader, and
            # device-read allowances needed even by /usr/bin/true. It does
            # not grant user-workspace writes; this profile still grants
            # writes only to the disposable scratch directory below.
            '(import "system.sb")',
            "(allow process*)",
            # Every process in this one-off sandbox instance belongs to the
            # verifier command. Permit process-group cleanup across descendants
            # without granting signals to unrelated host processes.
            "(allow signal (target same-sandbox))",
            # Python multiprocessing names its spawned semaphores ``/mp-``.
            # Limit cross-process synchronization to that private namespace.
            '(allow ipc-posix-sem (ipc-posix-name-prefix "/mp-"))',
            "(allow file-read*",
            f'  (subpath "{_sandbox_string(source)}")',
            f'  (subpath "{_sandbox_string(scratch)}")',
            f'  (subpath "{_sandbox_string(runtime_environment)}")',
            f'  (subpath "{_sandbox_string(git_metadata)}")',
            f'  (subpath "{_sandbox_string(pi_smoke_logs)}")',
            f'  (literal "{_sandbox_string(executable)}")',
            *(f'  (subpath "{_sandbox_string(root)}")' for root in allowed_roots),
            ")",
            # ``getcwd`` and dynamic-loader path checks need metadata on the
            # ancestors of the explicitly allowed paths, not read access to
            # their contents. Without these, macOS reports a nonexistent CWD.
            *(
                f'(allow file-read-metadata (path-ancestors "{_sandbox_string(path)}"))'
                for path in (
                    source,
                    scratch,
                    runtime_environment,
                    git_metadata,
                    pi_smoke_logs,
                    executable,
                )
            ),
            # Tests and validation helpers commonly use the stable ``/tmp``
            # spelling for inert fixture paths.  macOS resolves that symlink
            # through ``/private/tmp`` before a mocked boundary can observe
            # it, so permit metadata for the directory itself without
            # granting reads of its contents.
            f'(allow file-read-metadata (literal "{_sandbox_string(canonical_tmp)}"))',
            f'(allow file-write* (subpath "{_sandbox_string(scratch)}"))',
            f'(allow file-write* (subpath "{_sandbox_string(pi_smoke_logs)}"))',
            "(deny network*)",
        )
    )


def _host_verification_command(
    *,
    argv: tuple[str, ...],
    source: Path,
    scratch: Path,
    runtime_environment: Path,
    git_metadata: Path,
    pi_smoke_logs: Path,
) -> tuple[str, ...]:
    """Return a command that denies network and host writes to PR code.

    A disposable Git archive protects the reviewer checkout, but it is not a
    complete trust boundary by itself: test code could still access the host.
    On supported macOS hosts, ``sandbox-exec`` supplies the remaining boundary
    (no network, read-only source, write access only to ``scratch``).  We fail
    closed when that primitive is unavailable rather than quietly widening a
    reviewer-stage capability.
    """
    if sys.platform != "darwin":
        raise _HostVerificationBoundaryError("unsupported_host_verification_boundary")

    sandbox_exec = Path("/usr/bin/sandbox-exec")
    if not sandbox_exec.is_file() or not os.access(sandbox_exec, os.X_OK):
        raise _HostVerificationBoundaryError("host_verification_boundary_unavailable")

    executable = Path(argv[0])
    profile = scratch / "host-verification.sb"
    write_secure(
        profile,
        _host_verification_profile(
            source=source,
            scratch=scratch,
            runtime_environment=runtime_environment,
            git_metadata=git_metadata,
            pi_smoke_logs=pi_smoke_logs,
            executable=executable,
        ),
    )
    # Start through a constant trusted shell so resource limits are inherited
    # by ``sandbox-exec`` and every process launched by UV/pytest. Some macOS
    # launch contexts reject unprivileged hard-limit changes, so this local
    # boundary deliberately lowers the macOS-supported soft CPU and
    # output-file limits. The process cap is the host's live baseline plus a
    # fixed small headroom, so the verifier can spawn tools without removing
    # a per-PR bound. The separately mounted scratch volume remains the
    # non-bypassable disk quota. No PR text enters the shell program; the
    # fixed argv follows the ``--`` sentinel.
    limits = (
        "set -e; "
        'limit() { hard=$(ulimit -H "$1"); target=$2; '
        'if [ "$hard" != unlimited ] && [ "$hard" -lt "$target" ]; then target=$hard; fi; '
        'ulimit -S "$1" "$target"; }; '
        f"limit -t {_HOST_VERIFICATION_CPU_MAX_S}; "
        f"limit -f {_HOST_VERIFICATION_OUTPUT_FILE_MAX_BLOCKS}; "
        'active=$(/bin/ps -u "$(/usr/bin/id -u)" -o pid= | /usr/bin/wc -l | /usr/bin/tr -d " "); '
        f'limit -u "$((active + {_HOST_VERIFICATION_PROCESS_HEADROOM}))"; '
        'exec "$@"'
    )
    return (
        "/bin/sh",
        "-c",
        limits,
        "host-verification-limits",
        str(sandbox_exec),
        "-f",
        str(profile),
        *argv,
    )


def _hdiutil_create_argv(image: Path) -> tuple[str, ...]:
    """Return the valid blank HFS+ image creation argv for quota scratch."""
    return (
        "/usr/bin/hdiutil",
        "create",
        "-size",
        f"{_HOST_VERIFICATION_SCRATCH_MAX_BYTES // (1024 * 1024)}m",
        "-fs",
        "HFS+",
        "-quiet",
        str(image),
    )


@contextmanager
def _quota_backed_volume(root: Path, image_name: str, mountpoint: Path) -> Iterator[Path]:
    """Mount a fixed-size disposable volume at an already-created mountpoint."""
    if sys.platform != "darwin":
        raise _HostVerificationBoundaryError("unsupported_host_verification_boundary")
    hdiutil = Path("/usr/bin/hdiutil")
    if not hdiutil.is_file() or not os.access(hdiutil, os.X_OK):
        raise _HostVerificationBoundaryError("host_verification_quota_unavailable")
    image = root / image_name
    create = subprocess.run(
        _hdiutil_create_argv(image),
        capture_output=True,
        timeout=_HOST_VERIFICATION_SETUP_TIMEOUT_S,
        check=False,
        env=read_approved_parent_env(),
    )
    if create.returncode != 0:
        raise _HostVerificationBoundaryError("host_verification_quota_unavailable")
    attached = False
    try:
        attach = subprocess.run(
            (str(hdiutil), "attach", "-nobrowse", "-mountpoint", str(mountpoint), str(image)),
            capture_output=True,
            timeout=_HOST_VERIFICATION_SETUP_TIMEOUT_S,
            check=False,
            env=read_approved_parent_env(),
        )
        if attach.returncode != 0:
            raise _HostVerificationBoundaryError("host_verification_quota_unavailable")
        attached = True
        yield mountpoint
    finally:
        if attached:
            # This mount is a fresh per-command scratch image.  A completed
            # child can leave a brief busy reference, so retry one bounded
            # forced detach after a timeout, OS error, or nonzero result.
            # Retrying here avoids accumulating mounted images in a
            # long-running validation loop while still failing closed when
            # cleanup cannot be confirmed.
            for _attempt in range(2):
                try:
                    detach = subprocess.run(
                        (str(hdiutil), "detach", "-force", str(mountpoint)),
                        capture_output=True,
                        timeout=_HOST_VERIFICATION_SETUP_TIMEOUT_S,
                        check=False,
                        env=read_approved_parent_env(),
                    )
                except (OSError, subprocess.TimeoutExpired):
                    continue
                if detach.returncode == 0:
                    break
            else:
                raise _HostVerificationBoundaryError("host_verification_quota_cleanup_failed")


@contextmanager
def _quota_backed_scratch(root: Path) -> Iterator[Path]:
    """Mount the general fixed-size scratch volume before PR code runs."""
    scratch = root / "scratch"
    scratch.mkdir()
    with _quota_backed_volume(root, "scratch.dmg", scratch) as mounted:
        yield mounted


@contextmanager
def _quota_backed_pi_smoke_logs(root: Path, source: Path) -> Iterator[Path]:
    """Mount Pi smoke logs directly at their validated non-symlink path."""
    logs = source / "pi-smoke-logs"
    logs.mkdir()
    with _quota_backed_volume(root, "pi-smoke-logs.dmg", logs) as mounted:
        yield mounted


def _checkout_matches_immutable_head(checkout: Path, expected_head_sha: str) -> str | None:
    """Return an error when *checkout* no longer names the expected clean commit."""
    env = _controlled_git_env()
    try:
        head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=str(checkout),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if head.returncode != 0 or head.stdout.strip() != expected_head_sha:
            return "review_checkout_head_changed"
        for argv in (
            ("git", "diff", "--quiet", expected_head_sha, "--"),
            ("git", "diff", "--cached", "--quiet", expected_head_sha, "--"),
        ):
            clean = subprocess.run(
                argv,
                cwd=str(checkout),
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if clean.returncode != 0:
                return "review_checkout_not_clean"
    except (OSError, subprocess.TimeoutExpired):
        return "review_checkout_verification_failed"
    return None


def _extract_immutable_archive(archive: bytes, destination: Path) -> None:
    """Extract a Git archive while rejecting links and path traversal."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        members = tar.getmembers()
        if len(members) > _HOST_VERIFICATION_ARCHIVE_MAX_MEMBERS:
            raise _HostVerificationBoundaryError("git_archive_member_limit_exceeded")
        declared_size = 0
        for member in members:
            member_path = Path(member.name)
            if (
                member_path.is_absolute()
                or ".." in member_path.parts
                or member.issym()
                or member.islnk()
                or member.isdev()
            ):
                raise _HostVerificationBoundaryError("unsafe_git_archive_member")
            declared_size += member.size
            if declared_size > _HOST_VERIFICATION_ARCHIVE_MAX_BYTES:
                raise _HostVerificationBoundaryError("git_archive_size_limit_exceeded")
        # Materialize only regular files and directories ourselves.  This
        # avoids tarfile's version-dependent extraction filters and keeps the
        # already-validated destination as the sole write root.
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(member.mode & 0o777)
                continue
            if not member.isfile():
                raise _HostVerificationBoundaryError("unsupported_git_archive_member")
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            if extracted is None:
                raise _HostVerificationBoundaryError("unreadable_git_archive_member")
            with extracted, target.open("wb") as output:
                shutil.copyfileobj(extracted, output)
            target.chmod(member.mode & 0o777)


def _bounded_git_archive(
    checkout: Path, expected_head_sha: str, timeout_s: int
) -> tuple[bytes, str]:
    """Export one immutable commit without unbounded archive buffering."""
    with tempfile.TemporaryFile(mode="w+b") as stderr:
        process = subprocess.Popen(
            ("git", "archive", "--format=tar", expected_head_sha),
            cwd=str(checkout),
            env=_controlled_git_env(),
            stdout=subprocess.PIPE,
            stderr=stderr,
        )
        if process.stdout is None:  # pragma: no cover - guaranteed by PIPE
            raise _HostVerificationBoundaryError("immutable_source_snapshot_failed")
        archive = bytearray()
        deadline = time.monotonic() + timeout_s
        while chunk := process.stdout.read(64 * 1024):
            if len(archive) + len(chunk) > _HOST_VERIFICATION_ARCHIVE_MAX_BYTES:
                process.kill()
                process.wait()
                raise _HostVerificationBoundaryError("git_archive_size_limit_exceeded")
            archive.extend(chunk)
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(process.args, timeout_s)
        remaining = max(deadline - time.monotonic(), 0.01)
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        stderr.seek(0, os.SEEK_END)
        stderr.seek(max(stderr.tell() - _TAIL, 0))
        stderr_tail = stderr.read().decode(errors="replace")
        if returncode != 0:
            raise _HostVerificationBoundaryError(
                f"immutable_source_snapshot_failed:{stderr_tail[-_ERR_MAX:]}"
            )
    return bytes(archive), stderr_tail


def _prepare_immutable_git_metadata(
    checkout: Path, expected_head_sha: str, source: Path, root: Path, git_executable: str
) -> Path:
    """Attach a sealed Git snapshot so repository-aware tests remain valid.

    ``git archive`` deliberately omits ``.git``. Several unit tests inspect
    only Git's tracked inventory or commit graph, so prepare a separate local
    bare clone at the already-proven head and point the archive's ``.git``
    control file to it. Both source and metadata are read-only to PR code once
    the macOS sandbox starts.
    """
    metadata = root / "metadata.git"
    env = _controlled_git_env()
    try:
        clone = subprocess.run(
            (git_executable, "clone", "--bare", "--no-local", str(checkout), str(metadata)),
            env=env,
            capture_output=True,
            text=True,
            timeout=_HOST_VERIFICATION_SETUP_TIMEOUT_S,
            check=False,
        )
        if clone.returncode != 0:
            raise _HostVerificationBoundaryError("immutable_git_metadata_snapshot_failed")
        head = subprocess.run(
            (git_executable, f"--git-dir={metadata}", "rev-parse", "HEAD"),
            env=env,
            capture_output=True,
            text=True,
            timeout=_HOST_VERIFICATION_SETUP_TIMEOUT_S,
            check=False,
        )
        if head.returncode != 0 or head.stdout.strip() != expected_head_sha:
            raise _HostVerificationBoundaryError("immutable_git_metadata_head_changed")
        for key, value in (("core.bare", "false"), ("core.worktree", str(source.resolve()))):
            configured = subprocess.run(
                (git_executable, f"--git-dir={metadata}", "config", key, value),
                env=env,
                capture_output=True,
                text=True,
                timeout=_HOST_VERIFICATION_SETUP_TIMEOUT_S,
                check=False,
            )
            if configured.returncode != 0:
                raise _HostVerificationBoundaryError("immutable_git_metadata_setup_failed")
        origin = subprocess.run(
            (git_executable, "-C", str(checkout), "remote", "get-url", "origin"),
            env=env,
            capture_output=True,
            text=True,
            timeout=_HOST_VERIFICATION_SETUP_TIMEOUT_S,
            check=False,
        )
        if origin.returncode == 0 and (origin_url := origin.stdout.strip()):
            configured_origin = subprocess.run(
                (
                    git_executable,
                    f"--git-dir={metadata}",
                    "remote",
                    "set-url",
                    "origin",
                    origin_url,
                ),
                env=env,
                capture_output=True,
                text=True,
                timeout=_HOST_VERIFICATION_SETUP_TIMEOUT_S,
                check=False,
            )
            if configured_origin.returncode != 0:
                raise _HostVerificationBoundaryError("immutable_git_metadata_setup_failed")
        write_secure(source / ".git", f"gitdir: {metadata.resolve()}\n", permissions=0o400)
        # A bare clone has no index. Populate it while metadata is still
        # host-owned and writable so ``git ls-files`` remains a read-only
        # operation for repository-aware tests.
        indexed = subprocess.run(
            (git_executable, f"--git-dir={metadata}", "read-tree", expected_head_sha),
            env=env,
            capture_output=True,
            text=True,
            timeout=_HOST_VERIFICATION_SETUP_TIMEOUT_S,
            check=False,
        )
        if indexed.returncode != 0:
            raise _HostVerificationBoundaryError("immutable_git_metadata_setup_failed")
        _seal_host_runtime(metadata)
    except _HostVerificationBoundaryError:
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _HostVerificationBoundaryError("immutable_git_metadata_snapshot_failed") from exc
    return metadata


def _prepare_host_output_aliases(source: Path, scratch: Path) -> None:
    """Route the generic ignored build output into bounded scratch.

    Pi smoke tests deliberately reject symlinked artifact roots.  Their
    ``pi-smoke-logs`` directory is instead a second quota-backed volume mounted
    directly at that source path by :func:`_quota_backed_pi_smoke_logs`.
    """
    alias = source / "build"
    if alias.exists() or alias.is_symlink():
        raise _HostVerificationBoundaryError("host_verification_output_alias_conflict")
    coverage_alias = source / "coverage.xml"
    if coverage_alias.exists() or coverage_alias.is_symlink():
        raise _HostVerificationBoundaryError("host_verification_output_alias_conflict")
    try:
        target = scratch / "build"
        target.mkdir()
        alias.symlink_to(target, target_is_directory=True)
        coverage_target = scratch / "coverage.xml"
        # Keep the source-tree alias non-dangling while pytest is still
        # running. Repository inventory tests may encounter it before the
        # coverage plugin writes its final report.
        coverage_target.touch(mode=0o600)
        coverage_alias.symlink_to(coverage_target)
    except OSError as exc:
        raise _HostVerificationBoundaryError("host_verification_output_alias_failed") from exc


def _scratch_usage_exceeds_limit(scratch: Path) -> bool:
    """Return whether the PR-visible writable tree crossed its fixed quota."""
    total = 0
    for root, directories, filenames in os.walk(scratch, followlinks=False):
        for name in (*directories, *filenames):
            try:
                stat_result = (Path(root) / name).lstat()
            except OSError:
                continue
            total += stat_result.st_size
            if total > _HOST_VERIFICATION_SCRATCH_MAX_BYTES:
                return True
    return False


def _tail_file(path: Path) -> str:
    """Read a bounded diagnostic tail from a resource-limited child log."""
    try:
        with path.open("rb") as output:
            output.seek(0, os.SEEK_END)
            output.seek(max(output.tell() - _TAIL, 0))
            return output.read().decode(errors="replace")
    except OSError:
        return ""


def _confirmed_pytest_failure(returncode: int, stdout: str, stderr: str) -> bool:
    """Return whether the fixed pytest command, not its runner, failed.

    ``sandbox-exec``/UV/bootstrap errors also surface as nonzero exits.  Only
    pytest's normal test-failure exit code plus either supported terminal
    summary format is safe to send to the implementation agent as a
    code-remediation task.
    """
    transcript = f"{stdout}\n{stderr}"
    return returncode == 1 and bool(
        re.search(
            r"(?m)^(?:=+ .*?\b[1-9]\d* failed\b.*?\bin [0-9.]+s =+|"
            r"[1-9]\d* failed(?:, [^\n]*)? in [0-9.]+s"
            r"(?: \(\d+:\d{2}(?::\d{2})?\))?)$",
            transcript,
        )
    )


def _host_validation_failure_kind(
    argv: tuple[str, ...], returncode: int, stdout: str, stderr: str
) -> str:
    """Classify fixed-tool failures without mistaking bootstrap faults for code work."""
    transcript = f"{stdout}\n{stderr}"
    if _confirmed_pytest_failure(returncode, stdout, stderr):
        return "validation"
    if len(argv) >= 3 and argv[:2] == ("uv", "run"):
        tool = argv[2]
        if (
            tool == "ruff"
            and returncode == 1
            and re.search(
                r"(?m)^(?:Found |Would reformat |unformatted: |"
                r"[1-9]\d* files? would be reformatted$)",
                transcript,
            )
        ):
            return "validation"
        if (
            tool == "mypy"
            and returncode == 1
            and re.search(r"(?m)^Found [1-9]\d* errors? in \d+ files?", transcript)
        ):
            return "validation"
    return "runner"


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate a host-verification process tree after a hard boundary breach."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        process.kill()


def _run_bounded_host_command(
    command: tuple[str, ...],
    *,
    validation_argv: tuple[str, ...],
    source: Path,
    scratch: Path,
    environment: dict[str, str],
    timeout_s: int,
    shutdown: threading.Event,
) -> JobResult:
    """Run the sandboxed child with bounded files, time, and scratch usage."""
    output = scratch / "outputs"
    output.mkdir()
    stdout_path = output / "stdout.log"
    stderr_path = output / "stderr.log"
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                command,
                cwd=str(source),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            deadline = time.monotonic() + timeout_s
            resource_breach = False
            with subprocess_registry.track_process_group(process.pid):
                while process.poll() is None:
                    if shutdown.is_set():
                        _terminate_process_group(process)
                        process.wait()
                        return JobResult(
                            ok=False,
                            error="interrupted",
                            value={"failure_kind": "runner"},
                            interrupted=True,
                        )
                    if _scratch_usage_exceeds_limit(scratch):
                        resource_breach = True
                        _terminate_process_group(process)
                        break
                    if time.monotonic() >= deadline:
                        _terminate_process_group(process)
                        process.wait()
                        return JobResult(
                            ok=False,
                            error="timeout",
                            value={"failure_kind": "validation"},
                        )
                    time.sleep(_HOST_VERIFICATION_POLL_S)
                process.wait()
        stdout_tail = _tail_file(stdout_path)
        stderr_tail = _tail_file(stderr_path)
        if resource_breach:
            return JobResult(
                ok=False,
                error="host_verification_resource_limit_exceeded",
                value={"failure_kind": "runner"},
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
            )
        failure_kind = (
            "none"
            if process.returncode == 0
            else _host_validation_failure_kind(
                validation_argv, process.returncode, stdout_tail, stderr_tail
            )
        )
        return JobResult(
            ok=process.returncode == 0,
            value={"failure_kind": failure_kind},
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            error=None if process.returncode == 0 else f"rc={process.returncode}",
        )
    except OSError as exc:
        return JobResult(
            ok=False,
            error=f"host_verification_failed: {exc!s}"[:_ERR_MAX],
            value={"failure_kind": "runner"},
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


_HOST_SIGNING_CONFIG_KEYS = (
    "user.name",
    "user.email",
    "gpg.format",
    "user.signingkey",
)


def _parse_host_git_signing_config(raw: str) -> dict[str, str] | None:
    """Parse the exact allowlisted signing keys from Git's NUL output."""
    parsed: dict[str, str] = {}
    for entry in raw.split("\0"):
        if not entry:
            continue
        key, separator, value = entry.partition("\n")
        if not separator or key not in _HOST_SIGNING_CONFIG_KEYS or key in parsed:
            return None
        parsed[key] = value
    return parsed if set(parsed) == set(_HOST_SIGNING_CONFIG_KEYS) else None


def _validated_signing_key(value: str) -> Path | None:
    """Resolve an absolute, private, regular SSH signing key path."""
    try:
        signing_key = Path(value).expanduser()
        resolved_key = signing_key.resolve(strict=True)
        mode = resolved_key.stat().st_mode
    except (OSError, RuntimeError, ValueError):
        return None
    if (
        not signing_key.is_absolute()
        or signing_key.is_symlink()
        or not resolved_key.is_file()
        or mode & 0o022
    ):
        return None
    return resolved_key


def _read_host_git_signing_config(cwd: Path, *, timeout: int) -> dict[str, str] | None:
    """Read and validate the minimum host identity needed for policy signing."""
    trusted_git = _trusted_git_executable()
    if trusted_git is None:
        return None
    env = build_git_signing_env()
    env.pop("GIT_CONFIG_GLOBAL", None)
    expression = "^(user\\.name|user\\.email|gpg\\.format|user\\.signingkey)$"
    try:
        result = subprocess.run(
            [trusted_git, "config", "--global", "--null", "--get-regexp", expression],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    parsed = _parse_host_git_signing_config(result.stdout)
    if parsed is None:
        return None
    if parsed["gpg.format"] != "ssh":
        return None
    if any(
        not value or len(value) > 4096 or any(ord(character) < 32 for character in value)
        for value in parsed.values()
    ):
        return None
    resolved_key = _validated_signing_key(parsed["user.signingkey"])
    if resolved_key is None:
        return None
    parsed["user.signingkey"] = str(resolved_key)
    return parsed


def _controlled_git_signing_env(
    cwd: Path,
    *,
    timeout: int,
    private_metadata: bool = False,
) -> dict[str, str] | JobResult:
    """Return the controlled Git environment with an allowlisted signing identity."""
    signing = _read_host_git_signing_config(cwd, timeout=timeout)
    if signing is None:
        return JobResult(
            ok=False,
            value={"failure_kind": "signing_configuration"},
            error="host signing configuration unavailable",
        )
    signing_program = _trusted_executable("ssh-keygen", path=os.defpath)
    if signing_program is None:
        return JobResult(
            ok=False,
            value={"failure_kind": "signing_configuration"},
            error="trusted SSH signing executable unavailable",
        )
    env = _isolated_checkout_git_env()
    if private_metadata:
        env.pop("GIT_CONFIG", None)
    injected = {
        **signing,
        "commit.gpgsign": "true",
        "gpg.ssh.program": signing_program,
    }
    env["GIT_CONFIG_COUNT"] = str(len(injected))
    for index, (key, value) in enumerate(injected.items()):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def _required_git_signing_env(cwd: Path, *, timeout: int) -> dict[str, str]:
    """Return the validated signing environment or surface a typed Git-job error."""
    env = _controlled_git_signing_env(cwd, timeout=timeout)
    if isinstance(env, JobResult):
        raise _RebaseSigningEnvironmentError(env.error or "host signing configuration unavailable")
    return env


def _trusted_executable(name: str, *, path: str | None = None) -> str | None:
    """Resolve a command to an absolute path before entering a controlled env."""
    executable = shutil.which(name, path=path)
    return str(Path(executable).resolve()) if executable is not None else None


def _trusted_remote_git_config(gh_command: str) -> tuple[str, ...] | None:
    """Return isolated GitHub HTTPS and SSH transport configuration."""
    return _shared_trusted_remote_git_config(gh_command)


def _trusted_uv_executable() -> str | None:
    """Return an allowlisted, non-writable ``uv`` binary for host checks."""
    for candidate in _TRUSTED_UV_CANDIDATES:
        try:
            resolved = candidate.resolve(strict=True)
            mode = resolved.stat().st_mode
        except OSError:
            continue
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            continue
        if mode & 0o022:
            continue
        return str(resolved)
    return None


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


def _trusted_gh_executable(extra_path_root: Path | None = None) -> str | None:
    """Return an allowed absolute ``gh`` binary without consulting ``PATH``.

    ``extra_path_root`` is an explicit operator authority passed only through
    the loop CLI.  It contributes exactly ``<root>/bin/gh`` and rejects a
    candidate whose resolved path escapes that root.
    """
    return _shared_trusted_gh_executable(
        extra_path_root,
        system_candidates=_TRUSTED_GH_CANDIDATES,
        system_roots=_TRUSTED_GH_ROOTS,
    )


def _unsafe_local_git_config_key(config: str) -> str | None:  # noqa: C901
    """Return an unsafe repository/worktree config key, if *config* contains one."""
    for entry in config.split("\0"):
        if not entry:
            continue
        key, _separator, _value = entry.partition("\n")
        normalized = key.lower()
        if normalized in {
            "core.askpass",
            "core.attributesfile",
            "core.excludesfile",
            "core.fsmonitor",
            "core.gitproxy",
            "core.hookspath",
            "core.pager",
            "core.sshcommand",
            "core.worktree",
        }:
            return key
        if normalized in {"diff.external", "interactive.difffilter"}:
            return key
        if normalized.startswith("diff.") and normalized.rsplit(".", 1)[-1] in {
            "command",
            "textconv",
        }:
            return key
        if normalized == "credential.helper" or (
            normalized.startswith("credential.") and normalized.endswith(".helper")
        ):
            return key
        if normalized.startswith("remote.") and normalized.rsplit(".", 1)[-1] in {
            "proxy",
            "proxyauthmethod",
            "pushurl",
            "receivepack",
            "uploadpack",
        }:
            return key
        if normalized in {"fetch.recursesubmodules", "submodule.recurse"}:
            return key
        if normalized.startswith(("include.", "includeif.")):
            return key
        if normalized.startswith("filter.") and normalized.rsplit(".", 1)[-1] in {
            "clean",
            "process",
            "smudge",
        }:
            return key
        if normalized.startswith("merge.") and normalized.endswith(".driver"):
            return key
        # A checkout-specific URL rewrite can transform the validated literal
        # GitHub origin when it is later passed to ``git fetch``.  Any local
        # HTTP configuration can similarly proxy traffic or override TLS
        # verification/CA trust, including URL-scoped variants.
        if normalized.startswith(("http.", "url.")):
            return key
    return None


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


def _read_bounded_git_pointer(path: Path) -> str:
    """Read one small regular Git metadata pointer without following a link."""
    parent_fd, _identity = _open_directory_no_follow(path.parent)
    try:
        value, _file_identity = _read_bounded_git_pointer_at(parent_fd, path.name)
    finally:
        os.close(parent_fd)
    return value


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


def _portable_path_identity(path: Path, *, directory: bool) -> _FilesystemIdentity:
    """Validate one absolute path without a symlink, junction, or reparse point."""
    if not path.is_absolute():
        raise RuntimeError("linked worktree metadata path is not absolute")
    current = Path(path.anchor)
    final: os.stat_result | None = None
    for component in path.parts[1:]:
        current /= component
        metadata = current.lstat()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0) & reparse_flag
        ):
            raise RuntimeError("linked worktree metadata path has a reparse point")
        if current != path and not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("linked worktree metadata path prefix is unsafe")
        final = metadata
    if final is None:
        final = path.lstat()
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
    repo_identity = _portable_path_identity(repo_root, directory=True)
    worktree_identity = _portable_path_identity(worktree, directory=True)
    common_dir = repo_root / ".git"
    common_identity = _portable_path_identity(common_dir, directory=True)
    registered_root = common_dir / "worktrees"
    registry_identity = _portable_path_identity(registered_root, directory=True)
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
            repo_identity,
            worktree_identity,
            common_identity,
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
        common_fd, common_identity = _open_directory_at_no_follow(repo_fd, ".git")
        descriptors.append(common_fd)
        registry_fd, registry_identity = _open_directory_at_no_follow(common_fd, "worktrees")
        descriptors.append(registry_fd)
        pointer, marker_identity = _read_bounded_git_pointer_at(worktree_fd, ".git")
        prefix = "gitdir: "
        if not pointer.startswith(prefix):
            raise RuntimeError("linked worktree gitfile is invalid")
        common_dir = repo_root / ".git"
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
                repo_identity,
                worktree_identity,
                common_identity,
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


def _refresh_verified_recovery_index(
    repo_root: Path,
    worktree: Path,
    *,
    expected_git_env: dict[str, str],
    private_git_env: dict[str, str],
    source_sha: str,
    expected_tree: str,
    timeout: int,
) -> None:
    """Refresh the real index from one verified child after an exact rebind."""
    expected_linked = getattr(expected_git_env, "linked_env", expected_git_env)
    rebound = _linked_worktree_git_env(repo_root, worktree)
    if not _linked_binding_matches(expected_linked, rebound, include_index=True):
        raise RuntimeError("remediation writer Git metadata identity changed")
    expected_binding = getattr(expected_linked, "binding", None)
    rebound_binding = getattr(rebound, "binding", None)
    if (
        not isinstance(expected_binding, _LinkedWorktreeBinding)
        or not isinstance(rebound_binding, _LinkedWorktreeBinding)
        or rebound_binding.branch_ref != expected_binding.branch_ref
        or rebound_binding.branch_sha != expected_binding.branch_sha
    ):
        raise RuntimeError("remediation writer branch binding changed")
    refresh_env = dict(private_git_env)
    refresh_env.update(
        {
            "GIT_INDEX_FILE": str(rebound_binding.index),
            "GIT_WORK_TREE": str(worktree),
        }
    )
    git_utils.run(
        ["git", "read-tree", source_sha],
        cwd=worktree,
        timeout=timeout,
        env=refresh_env,
    )
    refreshed_tree = git_utils.run(
        ["git", "write-tree"],
        cwd=worktree,
        timeout=timeout,
        env=refresh_env,
    ).stdout.strip()
    if refreshed_tree != expected_tree:
        raise RuntimeError("remediation writer refreshed index tree changed")
    refreshed = _linked_worktree_git_env(repo_root, worktree)
    refreshed_binding = getattr(refreshed, "binding", None)
    if (
        not _linked_binding_matches(rebound, refreshed, include_index=False)
        or not isinstance(refreshed_binding, _LinkedWorktreeBinding)
        or refreshed_binding.branch_ref != rebound_binding.branch_ref
        or refreshed_binding.branch_sha != rebound_binding.branch_sha
    ):
        raise RuntimeError("remediation writer Git metadata identity changed")


def _compare_and_swap_linked_branch(  # noqa: C901
    binding: _LinkedWorktreeBinding,
    *,
    expected_sha: str,
    new_sha: str,
) -> None:
    """Update one bound local branch without loading repository configuration."""
    if not _is_full_commit_sha(expected_sha) or not _is_full_commit_sha(new_sha):
        raise RuntimeError("remediation branch update has an invalid commit")
    components = tuple(binding.branch_ref.split("/"))
    if (
        len(components) < 3
        or components[:2] != ("refs", "heads")
        or any(component in {"", ".", ".."} for component in components)
    ):
        raise RuntimeError("remediation branch update has an invalid reference")
    if not _secure_dir_fd_supported():
        raise RuntimeError("secure branch update is unavailable")
    common_fd, common_identity = _open_directory_no_follow(binding.common_dir)
    if common_identity != binding.common_identity:
        os.close(common_fd)
        raise RuntimeError("remediation branch metadata identity changed")
    descriptors = [common_fd]
    lock_name = f"{components[-1]}.lock"
    lock_fd: int | None = None
    lock_exists = False
    try:
        parent_fd = common_fd
        for component in components[:-1]:
            try:
                next_fd, _identity = _open_directory_at_no_follow(parent_fd, component)
            except FileNotFoundError:
                with suppress(FileExistsError):
                    os.mkdir(component, 0o755, dir_fd=parent_fd)
                next_fd, _identity = _open_directory_at_no_follow(parent_fd, component)
            parent_fd = next_fd
            descriptors.append(parent_fd)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if os.name == "posix":
            flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_fd = os.open(lock_name, flags, 0o644, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise RuntimeError("remediation branch update is locked") from exc
        lock_exists = True
        observed = _read_branch_ref(common_fd, binding.common_dir, binding.branch_ref)
        if observed == new_sha:
            return
        if observed != expected_sha:
            raise RuntimeError("remediation branch changed before local update")
        payload = f"{new_sha}\n".encode("ascii")
        written = 0
        while written < len(payload):
            count = os.write(lock_fd, payload[written:])
            if count <= 0:
                raise RuntimeError("remediation branch update write failed")
            written += count
        os.fsync(lock_fd)
        os.close(lock_fd)
        lock_fd = None
        os.replace(
            lock_name,
            components[-1],
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        lock_exists = False
        os.fsync(parent_fd)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if lock_exists:
            with suppress(OSError):
                os.unlink(lock_name, dir_fd=descriptors[-1])
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _repo_lock_path(repo: str, lock_dir: Path | None = None) -> Path:
    """Cross-process advisory lock file for *repo*.

    Anchored at ``<repo_root>/<DEFAULT_STATE_DIR>/locks`` (the shared
    automation state dir) rather than the bare CWD, so every process that
    operates on this checkout resolves the SAME sentinel file regardless of
    which subdirectory it was launched from. ``file_lock`` creates the parent
    directory on first acquisition.

    Args:
        repo: Repository slug (``owner/name``); slashes are flattened.
        lock_dir: Override directory for the sentinel files (tests inject a
            temp dir here).

    Returns:
        Path of the sentinel lock file for *repo*.

    """
    if lock_dir is None:
        lock_dir = get_repo_root() / DEFAULT_STATE_DIR / "locks"
    return lock_dir / f"git-{repo.replace('/', '_')}.lock"


@dataclass
class _RepoLockEntry:
    """In-process git lock plus active/waiting user count."""

    lock: threading.Lock
    users: int = 0


class _GitLockTimeoutError(TimeoutError):
    """Raised when a Git job cannot acquire its cross-process repo lock in time."""


class _GitLockInterruptedError(RuntimeError):
    """Raised when shutdown interrupts a Git job while it waits for the repo lock."""


def _git_lock_failure_result(exc: _GitLockTimeoutError | _GitLockInterruptedError) -> JobResult:
    """Map a typed Git-lock failure to the corresponding bounded job result."""
    if isinstance(exc, _GitLockTimeoutError):
        return JobResult(ok=False, error="lock_timeout")
    return JobResult(
        ok=False,
        interrupted=True,
        error="interrupted_waiting_for_git_lock",
    )


def _git_environment_failure_result(
    exc: _RebaseSigningEnvironmentError | _RemoteGitAuthenticationError,
) -> JobResult:
    """Map a controlled Git environment failure to a typed job result."""
    failure_kind = (
        "signing_configuration"
        if isinstance(exc, _RebaseSigningEnvironmentError)
        else "remote_authentication"
    )
    return JobResult(ok=False, error=str(exc), value={"failure_kind": failure_kind})


@contextmanager
def _interruptible_file_lock(
    path: Path,
    *,
    shutdown: threading.Event,
    timeout_s: float,
) -> Iterator[None]:
    """Acquire ``path`` without an unbounded blocking flock wait."""
    deadline = time.monotonic() + max(timeout_s, 0.0)

    while True:
        if shutdown.is_set():
            raise _GitLockInterruptedError

        with ExitStack() as stack:
            try:
                stack.enter_context(file_lock(path, blocking=False))
            except LockUnavailableError as exc:
                now = time.monotonic()
                if now >= deadline:
                    raise _GitLockTimeoutError from exc

                wait_s = min(_GIT_LOCK_WAIT_POLL_S, deadline - now)
                if shutdown.wait(timeout=wait_s):
                    raise _GitLockInterruptedError from exc
                continue

            if shutdown.is_set():
                raise _GitLockInterruptedError
            yield
            return


def _evidence_patch_digest(cwd: Path, *revisions: str) -> str:
    """Hash the exact Git patch tested or committed by the queue."""
    result = git_utils.run(
        ["git", "diff", "--binary", "--no-ext-diff", *revisions, "--"],
        cwd=cwd,
        timeout=30,
    )
    return hashlib.sha256(result.stdout.encode()).hexdigest()


def _git_evidence_fields(job: GitJob, result: JobResult) -> dict[str, object]:
    """Return immutable Git outcome fields for a private queue receipt."""
    fields: dict[str, object] = {"job_type": "git", "operation": job.op}
    if not isinstance(result.value, dict):
        return fields
    fields.update(
        head_sha=result.value.get("head_sha"),
        pushed=result.value.get("pushed"),
    )
    head_sha = result.value.get("head_sha")
    worktree = job.kwargs.get("worktree_path")
    if (
        job.op == "commit_push"
        and result.value.get("pushed") is True
        and isinstance(head_sha, str)
        and isinstance(worktree, str)
    ):
        fields["committed_patch_sha256"] = _evidence_patch_digest(
            Path(worktree), f"{head_sha}^", head_sha
        )
    return fields


def _is_codex_implementation_job(job: AgentJob) -> bool:
    """Return true only for a Codex implementation-role job."""
    return bool(
        agent_runtime.requires_codex_implementation_isolation(job.agent)
        and job.execution_request is not None
        and job.execution_request.role is AgentRole.IMPLEMENTER
    )


@contextmanager
def _codex_git_boundary(cwd: Path) -> Iterator[codex_worktree_boundary.CodexWorktreeBoundary]:
    """Map Git receipt failures to one stable adapter error."""
    try:
        with codex_worktree_boundary.capture_codex_worktree_boundary(cwd) as boundary:
            yield boundary
    except codex_worktree_boundary.CodexWorktreeBoundaryError:
        raise CodexIsolationError("codex_adapter_request_mismatch") from None


def _codex_implementation_command(
    *,
    executable: Path,
    worktree: Path,
    model: str,
    session_id: str | None,
    sandbox: str,
    operation: AgentOperation,
    allowed_tools: tuple[str, ...],
) -> tuple[str, ...]:
    """Build one exact guest command from frozen worker inputs."""
    command = [str(executable), "exec"]
    if session_id:
        command.extend(("resume", session_id))
    selection = resolve_codex_model_selection(model)
    if selection.model:
        command.extend(("--model", selection.model))
    if selection.reasoning_effort not in {"", "default"}:
        command.extend(("-c", f"model_reasoning_effort={json.dumps(selection.reasoning_effort)}"))
    if session_id:
        command.extend(
            (
                "-c",
                f"sandbox_mode={json.dumps(sandbox)}",
                "-c",
                'approval_policy="never"',
            )
        )
    else:
        command.extend(
            (
                "--cd",
                str(worktree),
                "--sandbox",
                sandbox,
                "-c",
                'approval_policy="never"',
            )
        )
    command.extend(
        (
            "-c",
            f"hephaestus_automation.operation={json.dumps(operation.value)}",
            "-c",
            "hephaestus_automation.allowed_tools="
            + json.dumps(list(allowed_tools), separators=(",", ":")),
            "--json",
            "-",
        )
    )
    return tuple(command)


_CODEX_TOOL_CAPABILITIES = {
    "Bash": "bash",
    "Edit": "edit",
    "Glob": "find",
    "Grep": "grep",
    "Read": "read",
    "Write": "write",
}
_CODEX_NON_APPLICABLE_TOOLS = {
    AgentOperation.ADDRESS_REVIEW: frozenset({"Skill", "Task"}),
}
_CODEX_OPERATION_TOOLS = {
    AgentOperation.IMPLEMENT_INSPECT: ("Glob", "Grep", "Read"),
    AgentOperation.IMPLEMENT: ("Bash", "Edit", "Glob", "Grep", "Read", "Write"),
    AgentOperation.TEST_FIX: ("Bash", "Edit", "Glob", "Grep", "Read", "Write"),
    AgentOperation.ADDRESS_REVIEW: ("Bash", "Edit", "Glob", "Grep", "Read", "Write"),
}


def _codex_implementation_grants(job: AgentJob) -> tuple[str, tuple[str, ...], bool]:
    """Resolve and validate the operation-specific Codex grants."""
    execution = job.execution_request
    if execution is None or execution.role is not AgentRole.IMPLEMENTER:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    try:
        operation_policy = resolve_policy(execution)
    except ExecutionPolicyError:
        raise CodexIsolationError("codex_adapter_request_mismatch") from None
    workspace_write = operation_policy.filesystem is FilesystemMode.WORKTREE_RW
    sandbox = "workspace-write" if workspace_write else "read-only"
    if job.sandbox != sandbox:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    expected_tools = _CODEX_OPERATION_TOOLS.get(execution.operation)
    if expected_tools is None:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    declared_tools = {
        value.strip()
        for value in (job.allowed_tools or ",".join(expected_tools)).split(",")
        if value.strip()
    }
    non_applicable = _CODEX_NON_APPLICABLE_TOOLS.get(execution.operation, frozenset())
    allowed_tools = tuple(sorted(declared_tools - non_applicable))
    capabilities = {_CODEX_TOOL_CAPABILITIES.get(value, "") for value in allowed_tools}
    if (
        allowed_tools != expected_tools
        or "" in capabilities
        or not capabilities <= operation_policy.builtins
    ):
        raise CodexIsolationError("codex_adapter_request_mismatch")
    return sandbox, allowed_tools, workspace_write


def _reject_codex_terminal_cleanup_tombstone(profiles: Path) -> None:
    """Reject a session store while terminal cleanup is incomplete."""
    terminal_cleanup = profiles.with_name(profiles.name + ".terminal-cleanup")
    try:
        terminal_cleanup.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise CodexIsolationError("codex_adapter_inventory_uncertain") from None
    raise CodexIsolationError("codex_adapter_inventory_uncertain")


def _codex_private_profile(job: AgentJob, build_root: Path) -> Path:
    """Return one durable profile that is bound to the issue and cycle."""
    logical_session = job.session_key or job.session_agent
    if not logical_session:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    identity = canonical_sha256(
        (job.repo, int(job.issue), logical_session, str(job.cwd.resolve(strict=True)), job.model)
    )
    worktree = job.cwd.resolve(strict=True)
    profiles = worktree.parent / f".{worktree.name}-codex-sessions"
    _reject_codex_terminal_cleanup_tombstone(profiles)
    descriptor = -1
    try:
        with suppress(FileExistsError):
            profiles.mkdir(mode=0o700)
        lexical = profiles.lstat()
        descriptor = os.open(
            profiles,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(lexical.st_mode)
            or lexical.st_uid != os.geteuid()
            or (lexical.st_dev, lexical.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError("invalid Codex profile root")
        os.fchmod(descriptor, 0o700)
        canonical_profiles = profiles.resolve(strict=True)
        if canonical_profiles != profiles.absolute():
            raise OSError("Codex profile root is not canonical")
    except OSError:
        raise CodexIsolationError("codex_adapter_protocol_mismatch") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if canonical_profiles.is_relative_to(worktree) or not build_root.is_relative_to(worktree):
        raise CodexIsolationError("codex_adapter_protocol_mismatch")
    profile = canonical_profiles / identity
    if profile.is_symlink():
        raise CodexIsolationError("codex_adapter_protocol_mismatch")
    receipt_paths = (profile / ".active.json", profile / ".quarantine.json")
    if any(path.exists() or path.is_symlink() for path in receipt_paths):
        raise CodexIsolationError("codex_adapter_inventory_uncertain")
    try:
        with suppress(FileExistsError):
            profile.mkdir(mode=0o700)
        profile_status = profile.lstat()
        if (
            not stat.S_ISDIR(profile_status.st_mode)
            or profile_status.st_uid != os.geteuid()
            or stat.S_IMODE(profile_status.st_mode) != 0o700
        ):
            raise OSError("invalid Codex durable store")
        runs = profile / ".runs"
        with suppress(FileExistsError):
            runs.mkdir(mode=0o700)
        runs_status = runs.lstat()
        if (
            not stat.S_ISDIR(runs_status.st_mode)
            or runs_status.st_uid != os.geteuid()
            or stat.S_IMODE(runs_status.st_mode) != 0o700
        ):
            raise OSError("invalid Codex run store")
    except OSError:
        raise CodexIsolationError("codex_adapter_protocol_mismatch") from None
    return profile


def _initialize_codex_adapter(
    admission: codex_adapter_admission.CodexAdapterAdmission,
) -> CodexIsolationAdapterV1:
    """Initialize one admitted adapter and check its locked identity."""
    factory = admission.factory
    if (
        not callable(factory)
        or getattr(factory, "codex_isolation_api_version", None)
        != admission.lock.adapter_api_version
    ):
        raise CodexIsolationError("codex_adapter_protocol_mismatch")
    try:
        adapter = factory()
        admission.validate_adapter_identity(
            distribution=getattr(adapter, "adapter_distribution", ""),
            version=getattr(adapter, "adapter_version", ""),
            installed_tree_sha256=getattr(adapter, "installed_tree_sha256", ""),
        )
    except CodexIsolationError:
        raise
    except BaseException:
        raise CodexIsolationError("codex_adapter_initialization_failed") from None
    return cast(CodexIsolationAdapterV1, adapter)


@contextmanager
def _owned_codex_adapter(
    admission: codex_adapter_admission.CodexAdapterAdmission,
) -> Iterator[CodexIsolationAdapterV1]:
    """Close one admitted helper on all initialization and execution paths."""
    adapter: CodexIsolationAdapterV1 | None = None
    try:
        adapter = _initialize_codex_adapter(admission)
        yield adapter
    finally:
        owner = adapter if adapter is not None else admission.factory
        close_adapter = getattr(owner, "_close", None)
        if callable(close_adapter):
            close_adapter()


def _validate_staged_codex_executable(executable: StagedLinuxExecutable) -> None:
    """Recheck the staged path identity and bytes without following a link."""
    descriptor = -1
    try:
        descriptor = os.open(
            executable.path,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        value = os.fstat(descriptor)
        identity = (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_uid,
            value.st_size,
            value.st_mtime_ns,
        )
        digest = hashlib.sha256()
        offset = 0
        while True:
            chunk = os.pread(descriptor, 1024 * 1024, offset)
            if not chunk:
                break
            digest.update(chunk)
            offset += len(chunk)
        if identity != executable.file_identity or digest.hexdigest() != executable.digest:
            raise CodexIsolationError("codex_adapter_request_mismatch")
    except CodexIsolationError:
        raise
    except OSError:
        raise CodexIsolationError("codex_adapter_request_mismatch") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _codex_implementation_request(
    *,
    job: AgentJob,
    worktree: Path,
    prompt: str,
    private_profile: Path,
    admission: codex_adapter_admission.CodexAdapterAdmission,
    git_receipt: CodexGitReceiptV1,
    executable: StagedLinuxExecutable,
) -> CodexIsolationRequestV1:
    """Build the complete frozen request for one admitted adapter."""
    lock = admission.lock
    execution = job.execution_request
    if execution is None:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    sandbox, allowed_tools, workspace_write = _codex_implementation_grants(job)
    if executable.digest != lock.extracted_elf_sha256:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    run_nonce = new_run_nonce()
    ephemeral_profile = private_profile / ".runs" / run_nonce / "profile"
    fixed_git_environment = dict(git_receipt.fixed_environment)
    environment = tuple(
        sorted(
            build_codex_implementation_child_env(
                codex_home=ephemeral_profile,
                fixed_git_environment=fixed_git_environment,
            ).items()
        )
    )
    if execution.lifecycle is SessionLifecycle.RESUME_REQUIRED:
        if job.resume_binding is not None:
            raise CodexIsolationError("codex_adapter_request_mismatch")
        session_id = job.resume_session_id
        if not session_id:
            raise CodexIsolationError("codex_adapter_request_mismatch")
    elif job.resume_binding is not None or job.resume_session_id is not None:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    else:
        session_id = None
    session = json.dumps(
        {
            "allowed_tools": list(allowed_tools),
            "lifecycle": execution.lifecycle.value,
            "operation": execution.operation.value,
            "session_id": session_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    command = _codex_implementation_command(
        executable=executable.path,
        worktree=worktree,
        model=job.model,
        session_id=session_id,
        sandbox=sandbox,
        operation=execution.operation,
        allowed_tools=allowed_tools,
    )
    profile_read_only, profile_read_write = agent_runtime._codex_profile_policy_paths(
        ephemeral_profile,
        run_nonce,
    )
    read_only_mounts = {
        *git_receipt.read_only_paths,
        str(executable.path),
        *profile_read_only,
    }
    read_write_mounts = set(profile_read_write)
    if workspace_write:
        read_write_mounts.update(git_receipt.read_write_paths)
    else:
        read_only_mounts.update(git_receipt.read_write_paths)
    policy = CodexExecutionPolicyV1(
        schema_version=1,
        read_only_mounts=tuple(sorted(read_only_mounts)),
        read_write_mounts=tuple(sorted(read_write_mounts)),
        protected_overlay_mounts=tuple(sorted(git_receipt.protected_paths)),
        provider_relay=_CODEX_IMPLEMENTATION_PROVIDER_RELAY,
        command_network="deny",
        max_output_bytes=_CODEX_IMPLEMENTATION_OUTPUT_MAX_BYTES,
        term_grace_seconds=_CODEX_IMPLEMENTATION_GRACE_SECONDS,
        kill_grace_seconds=_CODEX_IMPLEMENTATION_GRACE_SECONDS,
        pipe_close_grace_seconds=_CODEX_IMPLEMENTATION_GRACE_SECONDS,
        inventory_quiescence_seconds=_CODEX_IMPLEMENTATION_INVENTORY_QUIESCENCE_SECONDS,
        total_deadline=float(job.timeout_s),
    )
    worktree_identity = git_receipt.canonical_worktree
    model = job.model or "default"
    issue = int(job.issue)
    session_identity = (
        job.repo,
        issue,
        "implementer",
        worktree_identity,
        model,
        session,
    )
    return CodexIsolationRequestV1(
        schema_version=1,
        run_nonce=run_nonce,
        entry_point_name=lock.entry_point_name,
        adapter_api_version=lock.adapter_api_version,
        package_version=lock.adapter_version,
        deployment_lock_digest=admission.deployment_lock_sha256,
        wheel_digest=lock.wheel_sha256,
        installed_tree_digest=lock.installed_tree_sha256,
        command=command,
        command_digest=canonical_sha256(command),
        executable_platform="linux",
        executable_target=lock.codex_target,
        executable_release=lock.codex_release_tag,
        executable_asset_name=lock.codex_archive_asset,
        executable_path=str(executable.path),
        executable_digest=executable.digest,
        executable_file_identity=executable.file_identity,
        guest_image_digest=lock.guest_image_sha256,
        environment=environment,
        environment_digest=canonical_sha256(environment),
        prompt=prompt,
        prompt_digest=canonical_sha256(prompt),
        worktree_path=str(worktree),
        private_profile_path=str(ephemeral_profile),
        policy=policy,
        policy_digest=canonical_sha256(policy),
        git_receipt=git_receipt,
        git_receipt_digest=canonical_sha256(git_receipt),
        repository=job.repo,
        issue=issue,
        role="implementer",
        worktree_identity=worktree_identity,
        model=model,
        session=session,
        session_identity_digest=canonical_sha256(session_identity),
        monotonic_deadline=time.monotonic() + job.timeout_s,
    )


class WorkerPool:
    """Thread pool executor for submitting and tracking frozen jobs.

    Jobs are executed via :meth:`submit`; a future callback drains results to
    the completion queue. Workers never mutate ``WorkItem`` objects or stage
    queues. Agent jobs do build prompts in the worker; prompt builders may do
    read-only GitHub fetches, while durable GitHub mutations remain coordinator
    responsibilities.

    Completion contract: every non-cancelled :meth:`submit` produces EXACTLY
    ONE ``(handle, result)`` tuple on the completion queue — normal job
    failures are converted to error results in :meth:`_run`, and any exception
    that still escapes the future is converted to a ``worker_crash`` result in
    :meth:`_on_future_done`. Only futures cancelled before starting (via
    :meth:`shutdown`'s ``cancel_futures=True``) emit no completion; the
    coordinator synthesizes those.
    """

    def __init__(
        self,
        size: int,
        shutdown: threading.Event,
        completion_q: CompletionQueue,
        lock_dir: Path | None = None,
        gh_extra_path_root: Path | None = None,
        github_job_runner: GitHubJobRunner | None = None,
        athena_skill_executor: AthenaSkillExecutor | None = None,
        rebase_policy_selector: RebasePolicySelector | None = None,
        evidence_receipt_dir: Path | None = None,
    ) -> None:
        """Initialize the pool.

        Args:
            size: Number of worker threads.
            shutdown: Event that signals pool shutdown; workers check it before
                starting and after completing each job.
            completion_q: Queue to which ``(JobHandle, JobResult)`` tuples are
                sent when jobs complete.
            lock_dir: Optional override for the cross-process git lock
                directory (tests inject a temp dir; defaults to the shared
                automation state dir — see :func:`_repo_lock_path`).
            gh_extra_path_root: Explicit CLI-provided root that may supply
                only ``bin/gh`` for checkout synchronization.
            github_job_runner: Closed worker-side GitHub operation runner.
            athena_skill_executor: Closed host-owned Athena skill executor.
            rebase_policy_selector: Host-trusted selector for the policy that
                applies to each repository.  ``None`` keeps the shared executor
                repository-agnostic.
            evidence_receipt_dir: Optional private directory for bounded typed
                agent and Athena result receipts.

        """
        self._executor = ThreadPoolExecutor(
            max_workers=size,
            thread_name_prefix="hephaestus-pipeline-worker",
        )
        self._shutdown = shutdown
        self._completion_q = completion_q
        self._completion_wakeup: threading.Event | None = None
        self._completion_saturation: threading.Event | None = None
        self._repo_locks: dict[str, _RepoLockEntry] = {}
        self._repo_locks_guard = threading.Lock()
        self._lock_dir = lock_dir
        self._gh_extra_path_root = gh_extra_path_root
        self._github_job_runner = github_job_runner
        self._athena_skill_executor = athena_skill_executor
        self._rebase_policy_selector = rebase_policy_selector
        self._evidence_receipt_dir = evidence_receipt_dir

    @contextmanager
    def _repo_lock(self, repo: str, *, deadline_s: float | None = None) -> Iterator[None]:
        """Serialize in-process worker operations for one repository."""
        with self._repo_locks_guard:
            entry = self._repo_locks.get(repo)
            if entry is None:
                entry = _RepoLockEntry(threading.Lock())
                self._repo_locks[repo] = entry
            entry.users += 1

        acquired = False
        try:
            if deadline_s is None:
                entry.lock.acquire()
                acquired = True
            else:
                remaining_s = deadline_s - time.monotonic()
                if remaining_s <= 0 or not entry.lock.acquire(timeout=remaining_s):
                    raise _GitLockTimeoutError
                acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            with self._repo_locks_guard:
                entry.users -= 1
                if entry.users == 0 and self._repo_locks.get(repo) is entry:
                    self._repo_locks.pop(repo, None)

    def set_completion_notifiers(
        self,
        *,
        wakeup: threading.Event,
        saturation: threading.Event,
    ) -> None:
        """Bind coordinator-owned completion wake and saturation latches.

        The coordinator creates these latches before it submits work.  A
        successful non-blocking completion write wakes its event loop; an
        impossible full completion queue instead latches a fatal coordinator
        fault.  The callback deliberately has no overflow buffer: retaining
        the owning item in the coordinator's in-flight registry makes it
        resumable during that fatal teardown.
        """
        self._completion_wakeup = wakeup
        self._completion_saturation = saturation

    def submit(
        self,
        job: AgentJob | BuildTestJob | GitJob | GitHubJob | CompactJob | AthenaSkillJob,
        on_done_state: str | StageName,
        *,
        claim_key: str = "",
        claim_stage: str = "",
    ) -> JobHandle:
        """Submit a job for execution.

        Args:
            job: Immutable frozen job spec.
            on_done_state: Pipeline stage the item should transition to when
                this job completes.
            claim_key: Optional coordinator item key for worker-claim logging.
            claim_stage: Optional stage queue name for worker-claim logging.

        Returns:
            JobHandle carrying the submitted job and target state; the
            coordinator uses the handle to route the completion back to the
            work item.

        """
        handle = JobHandle(job=job, on_done_state=on_done_state)
        # Capture the caller's ContextVar snapshot so worker-thread prompt
        # builders see the same CLI-selected prompt catalog as the coordinator.
        context = copy_context()
        future = self._executor.submit(context.run, self._run, job, claim_key, claim_stage)
        future.add_done_callback(lambda f: self._on_future_done(handle, f))
        return handle

    def shutdown(self, *, mark_interrupted: bool = True) -> None:
        """Shut down the pool.

        When ``mark_interrupted`` is true, sets the shutdown event before
        cancelling pending futures and SIGTERMing every in-flight agent process
        group. Coordinators pass false for ordinary ``finally`` cleanup so
        releasing pool resources cannot reclassify a completed run as a signal
        interruption. ``executor.shutdown(cancel_futures=True)`` only cancels
        UN-STARTED futures; a job already blocked in a ``claude`` subprocess
        would keep running and pin its non-daemon worker thread (holding the
        interpreter open at exit — the #2059 leak). Terminating tracked process
        groups frees those workers promptly.
        """
        if mark_interrupted:
            self._shutdown.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
        subprocess_registry.terminate_all()

    def _on_future_done(self, handle: JobHandle, future: Future[JobResult]) -> None:
        """Drain result to completion queue when a job future completes.

        If the future was cancelled, do not emit a completion (the coordinator
        synthesizes one later). For every OTHER outcome a completion MUST be
        queued: ``_run`` already converts normal job failures into error
        results, and anything that still escapes ``future.result()`` -- any
        ``Exception`` plus the process-control escapes ``KeyboardInterrupt``,
        ``SystemExit``, and ``GeneratorExit`` -- is converted here to a
        ``worker_crash`` result so a non-cancelled submit never silently loses
        its completion. Process-control escapes are logged without traceback at
        warning/info severity; genuine ``Exception`` crashes keep
        ``logger.exception``. ``KeyboardInterrupt`` is intentionally NOT
        re-raised after queuing: this callback runs on an executor worker
        thread where a re-raise would only print a traceback, not stop the
        process.
        """
        if future.cancelled():
            return  # cancel_futures synthesizes NO completion
        worker_id = threading.current_thread().name
        try:
            result = future.result()
        except KeyboardInterrupt as exc:
            logger.warning("Worker future interrupted; converting to worker_crash result")
            result = JobResult(
                ok=False,
                error=f"worker_crash: {type(exc).__name__}: {exc!s}"[:_ERR_MAX],
                worker_id=worker_id,
            )
        except (SystemExit, GeneratorExit) as exc:
            logger.info("Worker future exited during shutdown; converting to worker_crash result")
            result = JobResult(
                ok=False,
                error=f"worker_crash: {type(exc).__name__}: {exc!s}"[:_ERR_MAX],
                worker_id=worker_id,
            )
        except Exception as exc:
            logger.exception("Worker future raised; converting to worker_crash result")
            result = JobResult(
                ok=False,
                error=f"worker_crash: {type(exc).__name__}: {exc!s}"[:_ERR_MAX],
                worker_id=worker_id,
            )
        try:
            self._completion_q.put_nowait((handle, result))
        except queue_mod.Full:
            # With the coordinator's global C-in-flight invariant, a C-sized
            # completion queue cannot fill before a worker has a slot to
            # publish.  Treat a violation as an internal fault rather than
            # blocking this callback forever.  There is intentionally no
            # unbounded spill structure: finalization retains the in-flight
            # WorkItem as RESUMABLE for the next run.
            logger.error("completion queue saturated; refusing to block worker callback")
            if self._completion_saturation is not None:
                self._completion_saturation.set()
            if self._completion_wakeup is not None:
                self._completion_wakeup.set()
            return

        if self._completion_wakeup is not None:
            self._completion_wakeup.set()

    def _run(
        self,
        job: AgentJob | BuildTestJob | GitJob | GitHubJob | CompactJob | AthenaSkillJob,
        claim_key: str = "",
        claim_stage: str = "",
    ) -> JobResult:
        """Execute a job and return its result.

        Converts normal job exceptions and process-control escapes into
        ``JobResult`` values so a single job failure does not crash the worker
        thread. After every job, post-checks the shutdown event and marks
        interrupted=True if it was set (SIGINT to the process group makes
        children return normally; the interrupt flag prevents misreading a
        killed job as success).
        """
        start = time.monotonic()
        worker_id = threading.current_thread().name
        logger.info(
            "worker_claim: worker_id=%s item=%s stage=%s job=%s repo=%s descr=%s",
            worker_id,
            claim_key or "-",
            claim_stage or "-",
            type(job).__name__,
            getattr(job, "repo", ""),
            getattr(job, "descr", ""),
        )

        # Pre-check: do not start a queued job if shutdown is set.
        if self._shutdown.is_set():
            result = JobResult(
                ok=False,
                interrupted=True,
                error="interrupted_before_start",
            )
        else:
            try:
                if isinstance(job, AthenaSkillJob):
                    result = self._run_athena_skill(job)
                elif isinstance(job, AgentJob):
                    result = self._run_agent(job)
                elif isinstance(job, BuildTestJob):
                    result = self._run_build_test(job)
                elif isinstance(job, GitJob):
                    result = self._run_git(job)
                elif isinstance(job, GitHubJob):
                    result = self._run_github(job)
                elif isinstance(job, CompactJob):
                    result = self._run_compact(job)
                else:
                    raise TypeError(f"unknown job type {type(job)}")
            except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                # Preserve the executing worker identity for process-control
                # escapes. The future callback may run outside the worker
                # thread if the future completed before callback registration.
                logger.info(
                    "Job %s exited via %s, returning worker_crash result",
                    job,
                    type(exc).__name__,
                )
                result = JobResult(
                    ok=False,
                    error=f"worker_crash: {type(exc).__name__}: {exc!s}"[:_ERR_MAX],
                )
            except Exception as exc:
                # Convert job execution failures into a JobResult so the callback
                # never re-raises into its thread.
                logger.exception("Job %s raised, returning error result", job)
                result = JobResult(
                    ok=False,
                    error=f"{type(exc).__name__}: {exc!s}"[:_ERR_MAX],
                )

            # Mandatory post-check: SIGINT to the process group makes subprocess
            # children return "normally" (rc=0 or some other code), so an
            # interrupted job must never read as success.
            if self._shutdown.is_set():
                result = replace(result, interrupted=True, ok=False)

        try:
            self._persist_evidence_receipt(job, result, claim_key, claim_stage)
        except (OSError, TypeError, ValueError) as exc:
            logger.error("failed to persist pipeline evidence receipt: %s", type(exc).__name__)
            result = replace(result, ok=False, error="evidence_receipt_failed")

        return replace(
            result,
            duration_s=time.monotonic() - start,
            stdout_tail=result.stdout_tail[-_TAIL:] if result.stdout_tail else "",
            stderr_tail=result.stderr_tail[-_TAIL:] if result.stderr_tail else "",
            worker_id=worker_id,
        )

    def _persist_evidence_receipt(
        self,
        job: AgentJob | BuildTestJob | GitJob | GitHubJob | CompactJob | AthenaSkillJob,
        result: JobResult,
        claim_key: str,
        claim_stage: str,
    ) -> None:
        """Persist one output-free typed receipt when explicitly configured."""
        receipt_dir = self._evidence_receipt_dir
        if receipt_dir is None or isinstance(job, CompactJob):
            return
        receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt_dir.chmod(0o700)
        issue = getattr(job, "issue", None)
        if issue is None and "#" in claim_key:
            _, _, candidate = claim_key.rpartition("#")
            if candidate.isdigit():
                issue = int(candidate)
        payload: dict[str, object] = {
            "schema_version": 1,
            "capture_nonce": (
                match.group(1)
                if (match := re.fullmatch(r"pipeline-receipts-([0-9a-f]{32})", receipt_dir.name))
                else ""
            ),
            "claim_key": claim_key,
            "claim_stage": claim_stage,
            "repo": job.repo,
            "issue": issue,
            "descr": job.descr,
            "ok": result.ok,
            "interrupted": result.interrupted,
        }

        if isinstance(job, AthenaSkillJob):
            payload["job_type"] = "athena"
            if isinstance(result.value, AthenaSkillResult):
                payload["result"] = asdict(result.value)
        elif isinstance(job, AgentJob):
            payload.update(
                {
                    "job_type": "agent",
                    "provider": job.agent,
                    "session_id": (
                        result.session_binding.session_id
                        if result.session_binding is not None
                        else result.session_id
                    ),
                    "observed_skill_invocations": list(result.observed_skill_invocations),
                }
            )
            if job.execution_request is not None:
                request = job.execution_request
                policy = resolve_policy(request)
                payload["execution_request"] = {
                    "role": request.role.value,
                    "operation": request.operation.value,
                    "lifecycle": request.lifecycle.value,
                }
                payload["tool_scopes"] = sorted(policy.builtins)
            else:
                payload["execution_request"] = None
                payload["tool_scopes"] = sorted(
                    scope.strip() for scope in (job.allowed_tools or "").split(",") if scope.strip()
                )
        elif isinstance(job, BuildTestJob):
            payload.update(
                {
                    "job_type": "build_test",
                    "argv_sha256": hashlib.sha256(
                        json.dumps(job.argv, separators=(",", ":")).encode()
                    ).hexdigest(),
                    "expected_head_sha": job.expected_head_sha,
                    "verified_runner_source_revision": job.verified_runner_source_revision,
                    "succeeded": result.ok,
                    "tested_patch_sha256": (
                        _evidence_patch_digest(job.cwd)
                        if result.ok and job.descr == "pre_pr_tests"
                        else None
                    ),
                }
            )
        elif isinstance(job, GitJob):
            payload.update(_git_evidence_fields(job, result))
        elif isinstance(job, GitHubJob):
            payload.update(
                {
                    "job_type": "github",
                    "operation": type(job.request).__name__,
                    "pr_number": getattr(job.request, "pr_number", None),
                    "request_issue": getattr(job.request, "issue_number", None),
                    "request_head_sha": getattr(job.request, "reviewed_head_sha", None),
                    "result_type": type(result.value).__name__
                    if result.value is not None
                    else None,
                    "result_action": getattr(result.value, "action", None),
                    "result_outcome": getattr(result.value, "outcome", None),
                }
            )
        filename = f"{time.time_ns()}-{threading.get_ident()}.json"
        write_secure(
            receipt_dir / filename,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )

    def _run_github(self, job: GitHubJob) -> JobResult:
        """Execute one closed GitHub operation exactly once per submission."""
        if self._github_job_runner is None:
            raise RuntimeError("GitHubJob submitted without a GitHubJobRunner")
        try:
            deadline_s = getattr(job.request, "deadline_s", None)
            with self._repo_lock(job.repo, deadline_s=deadline_s):
                receipt = self._github_job_runner.run(job)
        except Exception as exc:
            failure = _classify_github_failure(exc, now_epoch=time.time())
            if failure is None:
                raise
            failure_kind, retry_delay_s = failure
            logger.warning("GitHub job failed: %s", failure_kind)
            return JobResult(
                ok=False,
                error=failure_kind,
                value={
                    "failure_kind": failure_kind,
                    "retry_delay_s": retry_delay_s,
                },
            )
        return JobResult(ok=True, value=receipt)

    def _run_athena_skill(self, job: AthenaSkillJob) -> JobResult:
        """Run one host-owned skill without dispatching an agent harness."""
        if self._athena_skill_executor is None:
            raise RuntimeError("AthenaSkillJob submitted without an AthenaSkillExecutor")
        with _athena_workspace_lease(job):
            result = self._athena_skill_executor.execute(job.request)
        if not result.ok:
            return JobResult(ok=False, value=result, error=result.error)
        return JobResult(ok=True, value=result)

    @staticmethod
    def _run_codex_implementation(job: AgentJob, cwd: Path) -> agent_runtime.AgentRunResult:
        """Run one implementation job through the selected external adapter."""
        if (
            not job.codex_isolation_adapter
            or job.codex_isolation_deployment_lock is None
            or job.codex_isolation_deployment_lock_sha256 is None
        ):
            raise CodexIsolationError("codex_adapter_not_selected")
        with _agent_workspace_lease(job) as leased:
            if leased != cwd:
                raise CodexIsolationError("codex_adapter_request_mismatch")
            with _codex_git_boundary(cwd) as boundary:
                try:
                    admission = codex_adapter_admission.admit_codex_adapter(
                        lock_path=job.codex_isolation_deployment_lock,
                        expected_sha256=job.codex_isolation_deployment_lock_sha256,
                        selected_entry_point=job.codex_isolation_adapter,
                    )
                except codex_adapter_admission.CodexAdapterAdmissionError:
                    raise CodexIsolationError("codex_adapter_initialization_failed") from None
                with _owned_codex_adapter(admission) as adapter:
                    build_root = cwd / "build"
                    if build_root.is_symlink():
                        raise CodexIsolationError("codex_adapter_protocol_mismatch")
                    try:
                        build_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                        canonical_build_root = build_root.resolve(strict=True)
                    except OSError:
                        raise CodexIsolationError("codex_adapter_protocol_mismatch") from None
                    if not canonical_build_root.is_relative_to(cwd):
                        raise CodexIsolationError("codex_adapter_protocol_mismatch")
                    private_profile = _codex_private_profile(job, canonical_build_root)
                    with tempfile.TemporaryDirectory(
                        prefix="codex-implementation-",
                        dir=canonical_build_root,
                    ) as temporary:
                        job_root = Path(temporary)
                        job_root.chmod(0o700)
                        executable = stage_linux_executable(
                            Path(admission.lock.extracted_elf_path),
                            job_root,
                        )
                        try:
                            with plugin_skills_context(job.plugin_skills_dir):
                                prompt = job.prompt_builder(**job.prompt_kwargs)
                            request = _codex_implementation_request(
                                job=job,
                                worktree=cwd,
                                prompt=prompt,
                                private_profile=private_profile,
                                admission=admission,
                                git_receipt=boundary.receipt,
                                executable=executable,
                            )
                            boundary.verify_before_launch()
                            _validate_staged_codex_executable(executable)
                            try:
                                execution_request = job.execution_request
                                if execution_request is None:
                                    raise CodexIsolationError("codex_adapter_request_mismatch")
                                terminal_reaper = getattr(adapter, "_close", None)
                                if not callable(terminal_reaper):
                                    raise CodexIsolationError("codex_adapter_protocol_mismatch")
                                return agent_runtime._run_admitted_codex_implementation_session(
                                    adapter=adapter,
                                    request=request,
                                    execution_request=execution_request,
                                    executable_descriptor=executable.descriptor,
                                    terminal_reaper=cast(Callable[[], None], terminal_reaper),
                                )
                            finally:
                                try:
                                    _validate_staged_codex_executable(executable)
                                finally:
                                    boundary.verify_after_return()
                        finally:
                            close_staged_linux_executable(executable)

    def _run_agent(  # noqa: C901 - provider and session dispatch are one atomic boundary
        self, job: AgentJob
    ) -> JobResult:
        """Run an agent job (Claude or other runtime).

        Retry tradeoff: the whole agent invocation is wrapped in
        :func:`resilient_call`, so a *transient* failure (network reset, gh
        flake) re-runs the ENTIRE agent session — expensive, and the retried
        session may redo work the failed one partially completed. We accept
        that because agent invocations are idempotent-by-design at the
        workflow level (plan/review comments upsert; implementation re-runs
        converge on the same branch), and the alternative — no retry — turns
        every blip into a failed pipeline stage. Non-transient errors (rc!=0
        with non-transient stderr, timeouts) are NOT retried; they surface
        immediately as error results.

        Unexpected Exception subclasses from agent resolution, prompt
        construction, and the resilience wrapper are classified in this method
        for symmetry with the specific agent failures below. Process-control
        escapes are converted by :meth:`_run` so the returned result preserves
        the executing worker identity.
        """

        def remaining_timeout() -> int:
            """Return the checked time for the next recovery subprocess."""
            if job.deadline_s is None:
                return int(job.timeout_s)
            remaining = float(job.deadline_s) - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("agent operation deadline", 0)
            bounded = int(min(float(job.timeout_s), remaining))
            if bounded <= 0:
                raise subprocess.TimeoutExpired("agent operation deadline", 0)
            return bounded

        try:
            remaining_timeout()
            cwd = validate_job_workspace(job)
            if _is_codex_implementation_job(job):
                validate_agent_execution_support("codex", job.execution_request)
                agent_result = self._run_codex_implementation(job, cwd)
                session_id = agent_result.session_id or job.resume_session_id
                if session_id is not None and job.session_checkpoint is not None:
                    job.session_checkpoint(session_id, agent_result.session_binding)
                stdout = agent_result.stdout or ""
                value = None
                if job.parse is not None:
                    try:
                        value = job.parse(stdout)
                    except Exception as exc:
                        logger.exception("Parse callable raised for Codex implementation job")
                        return JobResult(
                            ok=False,
                            error=f"parse failed: {type(exc).__name__}: {exc!s}"[:_ERR_MAX],
                            stdout_tail=stdout[-_TAIL:],
                            session_id=session_id,
                            session_binding=agent_result.session_binding,
                        )
                return JobResult(
                    ok=True,
                    value=value if value is not None else stdout,
                    stdout_tail=stdout[-_TAIL:],
                    session_id=session_id,
                    session_binding=agent_result.session_binding,
                    observed_skill_invocations=agent_result.observed_skill_invocations,
                )
            agent = resolve_agent(
                job.agent,
                cwd=cwd,
                disable_pi_automation=job.disable_pi_automation,
                auth_status_timeout=job.auth_status_timeout,
                pi_isolation_adapter=job.pi_isolation_adapter,
                pi_dir=job.pi_dir,
                model_references=(job.model,),
            )
            is_claude = agent == "claude"
            validate_agent_execution_support(agent, job.execution_request)
            session_agent = job.session_agent or job.agent
            session_key = job.session_key or session_agent
            with plugin_skills_context(job.plugin_skills_dir):
                prompt = job.prompt_builder(**job.prompt_kwargs)
            remaining_timeout()

            def _invoke() -> tuple[str, str | None, AgentSessionBinding | None, tuple[str, ...]]:
                if is_claude:
                    # Scope priority: an explicit per-job grant (a stage that
                    # knows its exact needs, e.g. pr_review) wins; a read-only
                    # sandbox without one clamps to the fail-closed default;
                    # everything else resolves by session-agent role.
                    if job.allowed_tools is not None:
                        scope = ToolScope(job.allowed_tools)
                    elif job.sandbox == "read-only":
                        scope = DEFAULT_TOOL_SCOPE
                    else:
                        scope = tool_scope_for(session_agent)
                    stdout, claude_session_id = claude_invoke.invoke_claude_with_session(
                        repo=job.repo,
                        issue=job.issue,
                        agent=session_key,
                        prompt=prompt,
                        model=job.model,
                        cwd=cwd,
                        timeout=remaining_timeout(),
                        output_format=job.output_format,
                        allowed_tools=scope.allowed_tools,
                        permission_mode=scope.permission_mode,
                        input_via_stdin=True,
                        session_lifecycle=(
                            job.execution_request.lifecycle.value
                            if job.execution_request is not None
                            else None
                        ),
                    )
                    return stdout, claude_session_id, None, ()
                if job.resume_binding is not None:
                    agent_result = resume_agent_session(
                        agent=agent,
                        session_id=job.resume_binding.session_id,
                        prompt=prompt,
                        cwd=cwd,
                        timeout=remaining_timeout(),
                        model=job.model,
                        sandbox=job.sandbox,
                        approval="never",
                        process_tracker=subprocess_registry.track_process_group,
                        execution_request=job.execution_request,
                        resume_binding=job.resume_binding,
                        disable_pi_automation=job.disable_pi_automation,
                        pi_dir=job.pi_dir,
                    )
                elif job.resume_session_id:
                    agent_result = resume_agent_session(
                        agent=agent,
                        session_id=job.resume_session_id,
                        prompt=prompt,
                        cwd=cwd,
                        timeout=remaining_timeout(),
                        model=job.model,
                        sandbox=job.sandbox,
                        approval="never",
                        process_tracker=subprocess_registry.track_process_group,
                        execution_request=job.execution_request,
                        resume_binding=job.resume_binding,
                        disable_pi_automation=job.disable_pi_automation,
                        pi_dir=job.pi_dir,
                    )
                else:
                    agent_result = run_agent_session(
                        agent=agent,
                        prompt=prompt,
                        cwd=cwd,
                        timeout=remaining_timeout(),
                        model=job.model,
                        sandbox=job.sandbox,
                        approval="never",
                        process_tracker=subprocess_registry.track_process_group,
                        execution_request=job.execution_request,
                        resume_binding=job.resume_binding,
                        disable_pi_automation=job.disable_pi_automation,
                        pi_dir=job.pi_dir,
                    )
                # A resumed command may not repeat the session-start event;
                # retain the known id in that case.
                return (
                    agent_result.stdout or "",
                    agent_result.session_id or job.resume_session_id,
                    agent_result.session_binding,
                    agent_result.observed_skill_invocations,
                )

            def _invoke_leased() -> tuple[
                str,
                str | None,
                AgentSessionBinding | None,
                tuple[str, ...],
            ]:
                with _agent_workspace_lease(job):
                    return _invoke()

            stdout, session_id, session_binding, observed_skill_invocations = resilient_call(
                _invoke_leased,
                circuit_breaker_name=f"agent:{agent}",
                retry_predicate=lambda exc: (
                    not self._shutdown.is_set()
                    and not isinstance(
                        exc,
                        (AgentExecutionError, ExecutionPolicyError, PiSessionBindingError),
                    )
                ),
            )
            if session_id is not None and job.session_checkpoint is not None:
                job.session_checkpoint(session_id, session_binding)

            value = None
            if job.parse is not None:
                try:
                    value = job.parse(stdout)
                except Exception as exc:
                    logger.exception("Parse callable raised for agent job")
                    return JobResult(
                        ok=False,
                        error=f"parse failed: {type(exc).__name__}: {exc!s}"[:_ERR_MAX],
                        stdout_tail=stdout[-_TAIL:],
                        session_id=session_id,
                        session_binding=session_binding,
                    )

            return JobResult(
                ok=True,
                value=value if value is not None else stdout,
                stdout_tail=stdout[-_TAIL:],
                session_id=session_id,
                session_binding=session_binding,
                observed_skill_invocations=observed_skill_invocations,
            )

        except CodexIsolationError as exc:
            return JobResult(ok=False, error=exc.code)
        except CircuitBreakerOpenError:
            return JobResult(ok=False, error="circuit_open")
        except subprocess.TimeoutExpired:
            return JobResult(ok=False, error="timeout")
        except AgentExecutionError as exc:
            message = str(exc).casefold()
            resume_lost = bool(job.resume_session_id or job.resume_binding) and any(
                phrase in message
                for phrase in (
                    "session not found",
                    "session expired",
                    "cannot resume",
                    "resume failed",
                    "no conversation found",
                )
            )
            if resume_lost:
                return JobResult(ok=False, error="review-session-lost", session_lost=True)
            return _agent_exception_result(exc)
        except subprocess.CalledProcessError as exc:
            message = f"{exc.stdout or ''}\n{exc.stderr or ''}".casefold()
            resume_lost = bool(job.resume_session_id or job.resume_binding) and any(
                phrase in message
                for phrase in (
                    "session not found",
                    "session expired",
                    "cannot resume",
                    "resume failed",
                    "no conversation found",
                )
            )
            if resume_lost:
                return JobResult(ok=False, error="review-session-lost", session_lost=True)
            return JobResult(
                ok=False,
                error=f"rc={exc.returncode}",
                stdout_tail=(exc.stdout or "")[-_TAIL:],
                stderr_tail=(exc.stderr or "")[-_TAIL:],
            )
        except AgentSessionLostError:
            return JobResult(ok=False, error="review-session-lost", session_lost=True)
        except Exception as exc:
            return _agent_exception_result(exc)

    @staticmethod
    def _run_compact(job: CompactJob) -> JobResult:
        """Compact an agent session without making compaction a hard gate."""
        compacted = compact_agent_session(
            repo=job.repo,
            issue=job.issue,
            provider=job.agent,
            session_agent=job.session_agent,
            cwd=job.cwd,
            timeout=job.timeout_s,
            model=job.model,
            session_id=job.session_id,
            sandbox=job.sandbox,
            execution_request=job.execution_request,
            session_binding=job.session_binding,
            disable_pi_automation=job.disable_pi_automation,
            auth_status_timeout=job.auth_status_timeout,
            pi_isolation_adapter=job.pi_isolation_adapter,
            pi_dir=job.pi_dir,
        )
        # ``compact_agent_session`` intentionally swallows expected failures; a
        # missing or uncompactable transcript must not stall a review cycle.
        return JobResult(ok=True, value=compacted)

    def _run_build_test(self, job: BuildTestJob) -> JobResult:
        """Run a build/test job (subprocess with argv)."""
        if job.immutable_source:
            if not _is_full_commit_sha(job.expected_head_sha):
                return JobResult(ok=False, error="immutable_source_requires_full_head_sha")
            return self._run_immutable_build_test(job)
        argv = job.argv
        if job.verified_runner_source_revision is not None:
            argv = build_verified_runner_argv(
                job.argv,
                job.verified_runner_source_revision,
            )
        try:
            result = subprocess.run(
                argv,
                cwd=str(job.cwd),
                capture_output=True,
                text=True,
                timeout=job.timeout_s,
                check=False,  # we inspect rc below
                env=build_python_phase_env(job.cwd),
            )
            return JobResult(
                ok=result.returncode == 0,
                value=None,
                stdout_tail=result.stdout[-_TAIL:],
                stderr_tail=result.stderr[-_TAIL:],
                error=None if result.returncode == 0 else f"rc={result.returncode}",
            )
        except subprocess.TimeoutExpired as exc:
            return JobResult(
                ok=False,
                error="timeout",
                stdout_tail=str(exc.stdout or "")[-_TAIL:],
                stderr_tail=str(exc.stderr or "")[-_TAIL:],
            )

    def _run_immutable_build_test(self, job: BuildTestJob) -> JobResult:
        """Run a fixed host check in an archive of the proven review commit."""
        checkout_error = _checkout_matches_immutable_head(job.cwd, job.expected_head_sha)
        if checkout_error is not None:
            return JobResult(ok=False, error=checkout_error)

        # The reviewed isolation backend is currently macOS-only.  Record an
        # explicit platform-bound skip before resolving tools, archiving the
        # source, or executing any PR-controlled bytes.  A missing macOS
        # primitive still fails closed below; this branch is not a fallback.
        if sys.platform != "darwin":
            return JobResult(
                ok=False,
                error="unsupported_host_verification_boundary",
                value={
                    "head_sha": job.expected_head_sha,
                    "immutable_source": False,
                    "failure_kind": "runner",
                    "platform": sys.platform,
                    "status": "skipped",
                },
            )

        executable = (
            _trusted_uv_executable()
            if job.argv[0] == "uv"
            else _trusted_executable(job.argv[0], path=os.defpath)
        )
        if executable is None:
            return JobResult(ok=False, error="host_verification_executable_unavailable")
        git_executable = _trusted_git_executable()
        if git_executable is None:
            return JobResult(ok=False, error="host_verification_git_unavailable")
        argv = (executable, *job.argv[1:])
        try:
            runtime_environment = _verifier_owned_runtime_environment(job.cwd)
        except _HostVerificationBoundaryError as exc:
            return JobResult(ok=False, error=str(exc))

        try:
            with tempfile.TemporaryDirectory(prefix="hephaestus-host-verification-") as temp_dir:
                root = Path(temp_dir)
                # PR code executes from a separately archived source tree.
                # The sandbox grants write permission only to ``scratch``;
                # source is never a child of that writable root.
                source = root / "source"
                source.mkdir()
                archive, _archive_stderr = _bounded_git_archive(
                    job.cwd, job.expected_head_sha, job.timeout_s
                )
                _extract_immutable_archive(archive, source)
                git_metadata = _prepare_immutable_git_metadata(
                    job.cwd, job.expected_head_sha, source, root, git_executable
                )
                with _quota_backed_scratch(root) as scratch:
                    with _quota_backed_pi_smoke_logs(root, source) as pi_smoke_logs:
                        _prepare_host_output_aliases(source, scratch)
                        command = _host_verification_command(
                            argv=argv,
                            source=source,
                            scratch=scratch,
                            runtime_environment=runtime_environment,
                            git_metadata=git_metadata,
                            pi_smoke_logs=pi_smoke_logs,
                        )
                        result = _run_bounded_host_command(
                            command,
                            validation_argv=job.argv,
                            source=source,
                            scratch=scratch,
                            environment=_host_verification_env(
                                scratch, executable, runtime_environment, git_executable
                            ),
                            timeout_s=job.timeout_s,
                            shutdown=self._shutdown,
                        )
                checkout_error = _checkout_matches_immutable_head(job.cwd, job.expected_head_sha)
                if checkout_error is not None:
                    return JobResult(
                        ok=False,
                        error=checkout_error,
                        stdout_tail=result.stdout_tail,
                        stderr_tail=result.stderr_tail,
                    )
                return replace(
                    result,
                    value={
                        "head_sha": job.expected_head_sha,
                        "immutable_source": True,
                        "failure_kind": (
                            result.value.get("failure_kind", "runner")
                            if isinstance(result.value, dict)
                            else "runner"
                        ),
                        "platform": sys.platform,
                        "status": "passed" if result.ok else "failed",
                    },
                )
        except _HostVerificationBoundaryError as exc:
            return JobResult(ok=False, error=str(exc))
        except subprocess.TimeoutExpired as exc:
            return JobResult(
                ok=False,
                error="timeout",
                stdout_tail=str(exc.stdout or "")[-_TAIL:],
                stderr_tail=str(exc.stderr or "")[-_TAIL:],
            )
        except OSError as exc:
            return JobResult(ok=False, error=f"host_verification_failed: {exc!s}"[:_ERR_MAX])

    def _run_git(self, job: GitJob) -> JobResult:
        """Run a git job (serialized per-repo, in-process AND cross-process).

        Lock layering (documented invariant): the in-process
        ``threading.Lock`` is OUTER and the cross-process
        :func:`~hephaestus.utils.file_lock.file_lock` is INNER. The thread
        lock elects a single thread per process first, so at most one thread
        per process ever opens/holds the flock descriptor — sidestepping
        flock's confusing same-process semantics (multiple fds on one file
        within one process can still exclude each other) and keeping the
        blocking flock wait to one thread. Both locks are held for the entire
        operation because worktrees share ``.git``.
        """
        lock_path = _repo_lock_path(job.repo, self._lock_dir)
        try:
            with (
                git_utils.operation_deadline(job.deadline_s),
                self._repo_lock(job.repo, deadline_s=job.deadline_s),
                _interruptible_file_lock(
                    lock_path,
                    shutdown=self._shutdown,
                    timeout_s=cast(
                        float,
                        git_utils.remaining_operation_timeout(job.timeout_s),
                    ),
                ),
            ):
                return self._dispatch_git_op(job)
        except (_GitLockTimeoutError, _GitLockInterruptedError) as exc:
            return _git_lock_failure_result(exc)
        except (_RebaseSigningEnvironmentError, _RemoteGitAuthenticationError) as exc:
            return _git_environment_failure_result(exc)
        except BranchWorktreeOwnedError as exc:
            return JobResult(
                ok=False,
                error=BRANCH_WORKTREE_OWNED,
                value={"branch": exc.branch, "owner_path": str(exc.owner_path)},
            )
        except git_utils.DetachedHeadPushRemoteHeadChangedError as exc:
            if exc.failure_kind == "lease_drift":
                return JobResult(
                    ok=False,
                    error="publish failed: lease drift",
                    value={"failure_kind": "publish_lease_drift"},
                )
            return JobResult(
                ok=False,
                error="publish failed: remote head changed",
                value={"failure_kind": "publish_remote_head_changed"},
            )
        except git_utils.DetachedHeadPushRemoteHeadUnchangedError as exc:
            unchanged_failure = {
                "unknown": ("publish failed: unknown publication failure", "publish_unknown"),
                "timeout": ("publish failed: timeout", "publish_timeout"),
                "transport": ("publish failed: transport failure", "publish_transport_failed"),
            }.get(exc.failure_kind)
            if unchanged_failure is not None:
                error, failure_kind = unchanged_failure
                return JobResult(ok=False, error=error, value={"failure_kind": failure_kind})
            return JobResult(
                ok=False,
                error="publish failed: remote head unchanged",
                value={"failure_kind": "publish_remote_head_unchanged"},
            )
        except git_utils.DetachedHeadPushRemoteProbeError as exc:
            probe_failure = {
                "timeout": ("publish failed: remote probe timeout", "publish_timeout"),
                "transport": (
                    "publish failed: remote probe transport failure",
                    "publish_transport_failed",
                ),
            }.get(exc.failure_kind)
            if probe_failure is not None:
                error, failure_kind = probe_failure
                return JobResult(ok=False, error=error, value={"failure_kind": failure_kind})
            return JobResult(
                ok=False,
                error="publish failed: remote head probe failed",
                value={"failure_kind": "publish_remote_probe_failed"},
            )
        except subprocess.TimeoutExpired as exc:
            return JobResult(
                ok=False,
                error="timeout",
                stdout_tail=str(exc.stdout or "")[-_TAIL:],
                stderr_tail=str(exc.stderr or "")[-_TAIL:],
            )
        except subprocess.CalledProcessError as exc:
            return JobResult(
                ok=False,
                error=f"rc={exc.returncode}",
                stdout_tail=(exc.stdout or "")[-_TAIL:],
                stderr_tail=(exc.stderr or "")[-_TAIL:],
            )

    def _run_cleanup_git(self, job: GitJob) -> JobResult:
        """Run one cleanup job through the existing serialized Git boundary."""
        if job.op not in {"remove_worktree", "release_branch_reservation"}:
            raise TypeError(f"unsupported cleanup Git operation: {job.op}")
        return self._run_git(job)

    def _dispatch_git_op(self, job: GitJob) -> JobResult:  # noqa: C901
        """Dispatch a git operation to its handler.

        ``job.timeout_s`` is threaded into every git helper call so network
        operations cannot outlive the job budget while holding repo locks.
        """
        if job.op == "create_worktree":
            return self._git_create_worktree(job)

        elif job.op == "inspect_implementation_worktree":
            return self._git_inspect_implementation_worktree(job)

        elif job.op == "recover_dirty_worktree":
            return self._git_recover_dirty_worktree(job)

        elif job.op == "verify_pr_review_checkout":
            return self._git_verify_pr_review_checkout(job)

        elif job.op == "verify_remediation_journal":
            return self._git_verify_remediation_journal(job)

        elif job.op == "remove_worktree":
            from .git_cleanup import run_cleanup_job

            return run_cleanup_job(job, worktree_manager_type=WorktreeManager)

        elif job.op == "rebase":
            return self._git_rebase(job)

        elif job.op == "continue_rebase":
            return self._git_continue_rebase(job)

        elif job.op == "push":
            cwd = Path(str(job.kwargs.get("cwd") or ""))

            revalidate_remote = self._authenticated_remote_revalidator(
                cwd=cwd, expected_repo=job.transport_repository, timeout=job.timeout_s
            )
            remote_env, remote_config = revalidate_remote()
            git_utils.push_current_branch_with_lease_on_divergence(
                **job.kwargs,
                timeout=job.timeout_s,
                env=remote_env,
                remote_config=remote_config,
                revalidate_remote=revalidate_remote,
            )
            return JobResult(ok=True)

        elif job.op == "commit_push" or job.op == "prepare_remediation_recovery":
            return self._git_commit_push(job)

        elif job.op == "publish_remediation_recovery":
            return self._git_publish_remediation_recovery(job)

        elif job.op == "release_branch_reservation":
            from .git_cleanup import run_cleanup_job

            repo_root = Path(str(job.kwargs.get("repo_root") or ""))

            revalidate_remote = self._authenticated_remote_revalidator(
                cwd=repo_root, expected_repo=job.transport_repository, timeout=job.timeout_s
            )
            remote_env, remote_config = revalidate_remote()
            return run_cleanup_job(
                job,
                worktree_manager_type=WorktreeManager,
                remote_env=remote_env,
                remote_config=remote_config,
                revalidate_remote=revalidate_remote,
            )

        elif job.op == "clone":
            # gh repo clone <repo> <dest>
            repo = str(job.kwargs.get("repo") or "")
            dest = str(job.kwargs.get("dest") or "")
            if not repo or not dest:
                return JobResult(
                    ok=False,
                    error="clone requires non-empty 'repo' and 'dest' kwargs",
                )
            git_utils.run(["gh", "repo", "clone", repo, dest], cwd=None, timeout=job.timeout_s)
            return JobResult(ok=True)

        elif job.op == "sync_checkout":
            return self._git_sync_checkout(job)

        elif job.op == "verify_issue_wave_ancestry":
            return self._git_verify_issue_wave_ancestry(job)

        else:
            # Should be impossible due to GitJob.__post_init__ validation
            return JobResult(ok=False, error=f"unknown op {job.op!r}")

    def _git_publish_remediation_recovery(self, job: GitJob) -> JobResult:
        """Validate and publish one already-prepared remediation commit."""
        try:
            receipt = RemediationRecoveryReceipt.from_dict(job.kwargs.get("recovery_receipt"))
            reply_result = RemediationReplyResult.from_dict(job.kwargs.get("reply_result"))
        except ValueError as error:
            return JobResult(ok=False, error=f"remediation publication receipt is invalid: {error}")
        review_input = receipt.review_input
        batch_nonce = job.kwargs.get("remediation_batch_nonce")
        if (
            reply_result.review_input_sha256 != receipt.review_input_sha256
            or job.transport_repository.casefold() != review_input.repository
            or not isinstance(batch_nonce, str)
        ):
            return JobResult(ok=False, error="remediation publication identity is invalid")
        if job.kwargs.get("already_published") is True:
            worktree = Path(review_input.worktree_path)
            remote_head = self._read_remote_branch_head(
                worktree,
                remote="origin",
                branch=review_input.branch,
                expected_repo=job.transport_repository,
                timeout=job.timeout_s,
            )
            exact_commit = self._is_exact_recovery_commit(
                worktree,
                review_input.recovery_commit_sha,
                parent=review_input.reviewed_parent_sha,
                tree=review_input.candidate_tree_sha,
                timeout=job.timeout_s,
            )
            if remote_head != review_input.recovery_commit_sha or not exact_commit:
                return JobResult(
                    ok=False,
                    error="published remediation recovery commit is not exact",
                    value={"failure_kind": "publication_unavailable"},
                )
            artifact_job = replace(
                job,
                kwargs={
                    **job.kwargs,
                    "remediation_repository": review_input.repository,
                    "remediation_pr_number": review_input.pr_number,
                    "remediation_thread_snapshots": json.loads(review_input.thread_snapshot_json),
                    "remediation_replies": dict(reply_result.replies),
                    "remediation_failure_diagnostic": review_input.failure_diagnostic,
                    "remediation_journal_input_encoding": receipt.journal_input_encoding,
                    "remediation_journal_input_data": receipt.journal_input_data,
                },
            )
            try:
                handoff, journal = _remediation_recovery_artifacts(
                    artifact_job,
                    repo_root=Path(review_input.repo_root),
                    worktree=worktree,
                    branch=review_input.branch,
                    parent_sha=review_input.reviewed_parent_sha,
                    candidate_tree_sha=review_input.candidate_tree_sha,
                    recovery_commit_sha=review_input.recovery_commit_sha,
                    paths=CommitPaths(receipt.add_paths, receipt.update_paths),
                    committed_diff=review_input.committed_diff,
                    committed_diff_sha256=review_input.committed_diff_sha256,
                )
            except (TypeError, ValueError) as exc:
                return JobResult(ok=False, error=f"remediation journal is unavailable: {exc}")
            return JobResult(
                ok=True,
                value={
                    "pushed": True,
                    "head_sha": review_input.recovery_commit_sha,
                    "remediation_handoff": handoff,
                    "remediation_journal": {"marker": journal[0], "body": journal[1]},
                },
            )
        retry_job = replace(
            job,
            kwargs={
                "issue_number": review_input.issue_number,
                "worktree_path": review_input.worktree_path,
                "repo_root": review_input.repo_root,
                "branch": review_input.branch,
                "expected_recovery_head": receipt.expected_remote_sha,
                "expected_recovery_content_snapshot": dict(receipt.content_snapshot),
                "expected_recovery_tree_sha": review_input.candidate_tree_sha,
                "expected_recovery_diff": review_input.committed_diff,
                "expected_recovery_diff_sha256": review_input.committed_diff_sha256,
                "expected_recovery_add_paths": receipt.add_paths,
                "expected_recovery_update_paths": receipt.update_paths,
                "expected_recovery_commit_sha": review_input.recovery_commit_sha,
                "remediation_repository": review_input.repository,
                "remediation_pr_number": review_input.pr_number,
                "remediation_thread_snapshots": json.loads(review_input.thread_snapshot_json),
                "remediation_replies": dict(reply_result.replies),
                "remediation_batch_nonce": batch_nonce,
                "remediation_failure_diagnostic": review_input.failure_diagnostic,
                "remediation_journal_input_encoding": receipt.journal_input_encoding,
                "remediation_journal_input_data": receipt.journal_input_data,
            },
        )
        return self._git_commit_push(retry_job)

    def _git_verify_remediation_journal(self, job: GitJob) -> JobResult:
        """Prove one recovered journal against exact local Git objects."""
        raw_handoff = job.kwargs.get("handoff")
        raw_repo_root = job.kwargs.get("repo_root")
        if not isinstance(raw_handoff, dict) or not isinstance(raw_repo_root, str):
            return JobResult(ok=False, error="remediation journal Git identity is invalid")
        try:
            review_input_bytes = raw_handoff.get("review_input_bytes")
            if not isinstance(review_input_bytes, str):
                raise ValueError("remediation journal review input is unavailable")
            review_input = RemediationReviewInput.from_canonical_bytes(
                review_input_bytes.encode("utf-8")
            )
            repo_root = Path(raw_repo_root).resolve(strict=True)
            if (
                str(repo_root) != review_input.repo_root
                or job.transport_repository.casefold() != review_input.repository
                or raw_handoff.get("head_sha") != review_input.recovery_commit_sha
            ):
                raise ValueError("remediation journal repository binding is invalid")
            git_env = _isolated_checkout_git_env()
            if not self._is_exact_recovery_commit(
                repo_root,
                review_input.recovery_commit_sha,
                parent=review_input.reviewed_parent_sha,
                tree=review_input.candidate_tree_sha,
                timeout=job.timeout_s,
                git_env=git_env,
            ):
                raise ValueError("remediation journal commit identity is invalid")
            committed_diff = _run_bounded_git_output(
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
                    review_input.reviewed_parent_sha,
                    review_input.candidate_tree_sha,
                ),
                cwd=repo_root,
                timeout=job.timeout_s,
                max_bytes=IMPLEMENTATION_INSPECTION_DIFF_MAX_BYTES,
                retain_text=True,
                env=git_env,
            )
            changed = _run_bounded_git_output(
                (
                    "git",
                    "diff",
                    "--name-only",
                    "--no-renames",
                    "-z",
                    review_input.reviewed_parent_sha,
                    review_input.candidate_tree_sha,
                ),
                cwd=repo_root,
                timeout=job.timeout_s,
                max_bytes=IMPLEMENTATION_INSPECTION_STATUS_MAX_BYTES,
                retain_text=True,
                env=git_env,
            )
            changed_paths = tuple(path for path in changed.text.split("\0") if path)
            if (
                committed_diff.text != review_input.committed_diff
                or committed_diff.sha256 != review_input.committed_diff_sha256
                or len(changed_paths) != len(review_input.changed_paths)
                or set(changed_paths) != set(review_input.changed_paths)
            ):
                raise ValueError("remediation journal committed evidence is invalid")
        except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
            return JobResult(ok=False, error=f"remediation journal Git verification failed: {exc}")
        return JobResult(
            ok=True,
            value={
                "verified": True,
                "review_input_sha256": review_input.review_input_sha256,
                "head_sha": review_input.recovery_commit_sha,
            },
        )

    def _git_verify_issue_wave_ancestry(self, job: GitJob) -> JobResult:
        """Verify checkpoint commits are ancestors of synchronized main."""
        repo_root_value = job.kwargs.get("repo_root")
        main_sha = job.kwargs.get("main_sha")
        ancestor_values = job.kwargs.get("ancestor_shas")
        repo_root = Path(str(repo_root_value or ""))
        if (
            not repo_root.is_dir()
            or repo_root.is_symlink()
            or not (repo_root / ".git").exists()
            or not _is_full_commit_sha(main_sha)
            or not isinstance(ancestor_values, (tuple, list))
            or not all(_is_full_commit_sha(value) for value in ancestor_values)
        ):
            return JobResult(ok=False, error="invalid issue-wave ancestry request")
        try:
            for ancestor_sha in ancestor_values:
                result = git_utils.run(
                    ["git", "merge-base", "--is-ancestor", str(ancestor_sha), str(main_sha)],
                    cwd=repo_root,
                    check=False,
                    log_errors=False,
                    timeout=job.timeout_s,
                    env=_controlled_git_env(),
                )
                if result.returncode != 0:
                    return JobResult(
                        ok=False,
                        error=f"{ancestor_sha} is not an ancestor of synchronized main",
                    )
        except (OSError, subprocess.SubprocessError) as exc:
            return JobResult(ok=False, error=f"issue-wave ancestry verification failed: {exc}")
        return JobResult(
            ok=True,
            value={"main_sha": main_sha, "ancestors": tuple(ancestor_values)},
        )

    def _git_rebase(self, job: GitJob) -> JobResult:  # noqa: C901
        """Rebase an implementation writer and optionally lease-publish its head."""
        kwargs = dict(job.kwargs)
        if "publish_detached_head" in kwargs:
            return JobResult(
                ok=False,
                error="detached reviewer rebase publication is unsupported",
            )
        publish_rebased_head = bool(kwargs.pop("publish_rebased_head", False))
        abort_on_conflict = bool(kwargs.pop("abort_on_conflict", False))
        required_ancestor_shas = kwargs.pop("required_ancestor_shas", ())
        sync_to_expected_remote_head = bool(kwargs.pop("sync_to_expected_remote_head", False))
        branch = str(kwargs.pop("branch", "") or "")
        expected_remote_sha = kwargs.pop("expected_remote_sha", None)
        pr_number = kwargs.pop("pr_number", None)
        cwd = Path(str(kwargs.get("cwd") or ""))

        revalidate_remote = self._authenticated_remote_revalidator(
            cwd=cwd, expected_repo=job.transport_repository, timeout=job.timeout_s
        )
        remote_env, remote_config = revalidate_remote()
        if not isinstance(required_ancestor_shas, (list, tuple)) or not all(
            _is_full_commit_sha(value) for value in required_ancestor_shas
        ):
            return JobResult(ok=False, error="required rebase ancestors are invalid")

        def verify_required_ancestors() -> JobResult | None:
            for ancestor_sha in required_ancestor_shas:
                ancestry = git_utils.run(
                    ["git", "merge-base", "--is-ancestor", ancestor_sha, "HEAD"],
                    cwd=cwd,
                    check=False,
                    timeout=job.timeout_s,
                )
                if ancestry.returncode != 0:
                    return JobResult(
                        ok=False,
                        error=f"required dependency {ancestor_sha} is not in the source head",
                    )
            return None

        if publish_rebased_head:
            if not branch or not _is_full_commit_sha(expected_remote_sha) or not cwd.is_dir():
                return JobResult(ok=False, error="writer rebase publish arguments invalid")
            remote = str(kwargs.get("remote", "origin"))
            base_branch = str(kwargs.get("base_branch", "main"))
            base_ref = f"{remote}/{base_branch}"
            synced = self._sync_writer_to_expected_remote_head(
                cwd,
                enabled=sync_to_expected_remote_head,
                branch=branch,
                remote=remote,
                expected_repo=job.transport_repository,
                pr_number=pr_number,
                expected_remote_sha=expected_remote_sha,
                timeout=job.timeout_s,
            )
            if synced is not None:
                return synced
            git_utils.run(
                ["git", *remote_config, "fetch", remote, base_branch],
                cwd=cwd,
                timeout=job.timeout_s,
                env=remote_env,
            )
            ancestry = git_utils.run(
                ["git", "merge-base", "--is-ancestor", base_ref, "HEAD"],
                cwd=cwd,
                check=False,
                timeout=job.timeout_s,
            )
            if ancestry.returncode == 0:
                if required_error := verify_required_ancestors():
                    return required_error
                return self._verify_noop_writer_rebase(
                    cwd,
                    remote=remote,
                    branch=branch,
                    expected_repo=job.transport_repository,
                    expected_remote_sha=expected_remote_sha,
                    timeout=job.timeout_s,
                )
            if ancestry.returncode != 1:
                return JobResult(ok=False, error="cannot determine writer base ancestry")
        signing_env = _required_git_signing_env(cwd, timeout=job.timeout_s)
        result = git_utils.rebase_worktree_onto(
            **kwargs,
            preserve_conflicts=publish_rebased_head and not abort_on_conflict,
            timeout=job.timeout_s,
            env=signing_env,
            fetch_env=remote_env,
            fetch_config=remote_config,
        )
        if not result:
            if not publish_rebased_head or abort_on_conflict:
                return JobResult(
                    ok=False,
                    value=False,
                    error="mechanical rebase hit conflicts; aborted",
                )
            receipt = self._conflict_receipt(
                cwd,
                remote=remote,
                base_branch=base_branch,
                expected_remote_sha=expected_remote_sha,
                timeout=job.timeout_s,
            )
            if isinstance(receipt, JobResult):
                return receipt
            return JobResult(
                ok=False,
                value=receipt,
                error="mechanical rebase hit conflicts; resolution required",
            )
        if not publish_rebased_head:
            return JobResult(ok=True, value=True)
        if required_error := verify_required_ancestors():
            return required_error
        source_sha = self._read_publish_head(cwd, timeout=job.timeout_s)
        if isinstance(source_sha, JobResult):
            return source_sha
        remote_env, remote_config = self._authenticated_remote_git_configuration(
            cwd=cwd,
            expected_repo=job.transport_repository,
            timeout=job.timeout_s,
        )
        git_utils.push_head_to_branch(
            branch,
            expected_remote_sha,
            cwd,
            source_sha=source_sha,
            timeout=job.timeout_s,
            env=remote_env,
            remote_config=remote_config,
            revalidate_remote=revalidate_remote,
        )
        return JobResult(
            ok=True,
            value={
                "rebased": True,
                "published": True,
                "head_sha": source_sha,
            },
        )

    def _sync_writer_to_expected_remote_head(
        self,
        cwd: Path,
        *,
        enabled: bool,
        branch: str,
        remote: str,
        expected_repo: str,
        pr_number: object,
        expected_remote_sha: str,
        timeout: int,
    ) -> JobResult | None:
        """Sync a restored writer checkout and prove it matches the rebase lease."""
        if not enabled:
            return None
        try:
            if not git_utils.is_clean_working_tree(cwd, timeout=timeout):
                return JobResult(
                    ok=False,
                    error="restored writer checkout dirty before remote sync",
                )
            try:
                sync_pr_number = (
                    int(pr_number)
                    if isinstance(pr_number, (int, str)) and not isinstance(pr_number, bool)
                    else None
                )
            except ValueError:
                sync_pr_number = None
            self._sync_worktree_to_remote_branch(
                cwd,
                branch,
                remote=remote,
                expected_repo=expected_repo,
                pr_number=sync_pr_number,
                timeout=timeout,
            )
            source_sha = self._read_publish_head(cwd, timeout=timeout)
            if isinstance(source_sha, JobResult):
                return source_sha
            if source_sha != expected_remote_sha:
                return JobResult(
                    ok=True,
                    value={
                        "rebased": False,
                        "published": False,
                        "head_drift": True,
                        "head_sha": source_sha,
                    },
                )
            if not git_utils.is_clean_working_tree(cwd, timeout=timeout):
                return JobResult(
                    ok=False,
                    error="restored writer checkout dirty after remote sync",
                )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return JobResult(ok=False, error=f"restored writer remote sync failed: {exc}")
        return None

    def _authenticated_remote_git_configuration(
        self,
        *,
        cwd: Path | None = None,
        expected_repo: str | None = None,
        timeout: int = 60,
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        """Return the controlled environment and trusted GitHub transport config."""
        gh_command = _trusted_gh_executable(self._gh_extra_path_root)
        if gh_command is None:
            raise _RemoteGitAuthenticationError("required GitHub executable is unavailable")
        remote_config = _trusted_remote_git_config(gh_command)
        if remote_config is None:
            raise _RemoteGitAuthenticationError("required SSH executable is unavailable")
        env = _controlled_git_env()
        if cwd is not None and expected_repo is not None and (cwd / ".git").exists():
            if preflight_error := _checkout_preflight_error(cwd, timeout):
                raise _RemoteGitAuthenticationError(preflight_error)
            origin = git_utils.run(
                ["git", "remote", "get-url", "origin"],
                cwd=cwd,
                timeout=timeout,
                env=env,
            ).stdout.strip()
            normalized_origin = origin.rstrip("/").removesuffix(".git")
            expected_origins = {
                f"https://github.com/{expected_repo}",
                f"ssh://git@github.com/{expected_repo}",
                f"git@github.com:{expected_repo}",
            }
            if normalized_origin not in expected_origins:
                raise _RemoteGitAuthenticationError(
                    f"checkout has unexpected origin; expected origin {expected_repo}"
                )
        return env, remote_config

    def _authenticated_remote_revalidator(
        self,
        *,
        cwd: Path,
        expected_repo: str,
        timeout: int,
    ) -> Callable[[], tuple[dict[str, str], tuple[str, ...]]]:
        """Return a callback that validates a Git remote again before reuse."""

        def revalidate() -> tuple[dict[str, str], tuple[str, ...]]:
            return self._authenticated_remote_git_configuration(
                cwd=cwd,
                expected_repo=expected_repo,
                timeout=timeout,
            )

        return revalidate

    def _sync_worktree_to_remote_branch(
        self,
        cwd: Path,
        branch: str,
        *,
        remote: str = "origin",
        expected_repo: str,
        pr_number: int | None = None,
        timeout: int | None = None,
    ) -> None:
        """Synchronize a PR checkout with the trusted GitHub credential helper."""
        remote_env, remote_config = self._authenticated_remote_git_configuration(
            cwd=cwd,
            expected_repo=expected_repo,
            timeout=timeout or 60,
        )
        git_utils.sync_worktree_to_remote_branch(
            cwd,
            branch,
            remote=remote,
            pr_number=pr_number,
            timeout=timeout,
            env=remote_env,
            fetch_config=remote_config,
        )

    def _conflict_receipt(
        self,
        cwd: Path,
        *,
        remote: str,
        base_branch: str,
        expected_remote_sha: str,
        timeout: int,
    ) -> dict[str, object] | JobResult:
        """Capture the immutable inputs and file snapshot of a paused rebase."""
        try:
            paths_result = git_utils.run(
                ["git", "diff", "--name-only", "--diff-filter=U", "-z"],
                cwd=cwd,
                timeout=timeout,
            )
            paths = tuple(path for path in paths_result.stdout.split("\0") if path)
            if not paths or any(not is_safe_scope_retraction_path(path) for path in paths):
                return JobResult(ok=False, error="paused rebase conflict paths invalid")
            index_result = git_utils.run(
                ["git", "ls-files", "--stage", "-z"],
                cwd=cwd,
                timeout=timeout,
            )
            if not index_result.stdout:
                return JobResult(ok=False, error="paused rebase conflict index invalid")
            index_snapshot = hashlib.sha256(index_result.stdout.encode()).hexdigest()
            paused_head_sha = git_utils.run(
                ["git", "rev-parse", "HEAD"],
                cwd=cwd,
                timeout=timeout,
            ).stdout.strip()
            if not _is_full_commit_sha(paused_head_sha):
                return JobResult(ok=False, error="paused rebase head invalid")
            base_sha = git_utils.run(
                ["git", "rev-parse", f"{remote}/{base_branch}"],
                cwd=cwd,
                timeout=timeout,
            ).stdout.strip()
            if not _is_full_commit_sha(base_sha):
                return JobResult(ok=False, error="paused rebase base head invalid")
            snapshot = {path: self._conflict_path_digest(cwd, path) for path in paths}
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            return JobResult(ok=False, error=f"cannot capture paused rebase: {exc}")
        return {
            "rebased": False,
            "conflict_paths": paths,
            "conflict_snapshot": snapshot,
            "conflict_index_snapshot": index_snapshot,
            "paused_head_sha": paused_head_sha,
            "base_sha": base_sha,
            "expected_remote_sha": expected_remote_sha,
        }

    @staticmethod
    def _conflict_path_digest(cwd: Path, path: str) -> str:
        """Return a stable digest for one host-validated conflict path."""
        target = cwd / path
        if not target.exists():
            return "<absent>"
        return hashlib.sha256(target.read_bytes()).hexdigest()

    @staticmethod
    def _annotate_rebase_policy_failure(
        result: JobResult,
        policy: RebaseValidationPolicy,
        gate: str,
    ) -> JobResult:
        """Add policy identity without changing diagnostics from a gate."""
        value = dict(result.value) if isinstance(result.value, dict) else {}
        value["rebase_policy"] = policy.name
        raw_cause = value.get("cause")
        cause = result.error or (raw_cause if isinstance(raw_cause, str) else None)
        cause = cause or "validation failed"
        return replace(
            result,
            value=value,
            error=f"rebase policy {policy.name} {gate} failed: {cause}",
        )

    def _validate_rebased_tree(
        self,
        cwd: Path,
        *,
        policy: RebaseValidationPolicy | None = None,
    ) -> JobResult | None:
        """Run the selected semantic validator, if one applies."""
        if policy is None:
            return None
        try:
            result = policy.semantic_validator(cwd)
        except Exception as exc:
            result = JobResult(
                ok=False,
                value={"failure_kind": "semantic_validation"},
                error=f"{type(exc).__name__}: {exc}",
            )
        if result is None or result.ok:
            return None
        return self._annotate_rebase_policy_failure(result, policy, "semantic validation")

    def _select_rebase_policy(self, repo: str) -> RebaseValidationPolicy | None:
        """Select one host policy for a repository rebase."""
        selector = self._rebase_policy_selector
        return selector(repo) if selector is not None else None

    def _run_rebase_structural_validation(
        self,
        cwd: Path,
        *,
        timeout: int,
        policy: RebaseValidationPolicy | None = None,
    ) -> JobResult | None:
        """Run the selected structural test against the immutable rebased tree."""
        if policy is None:
            return None
        argv = policy.structural_test_argv
        if not argv or any(not isinstance(part, str) or not part for part in argv):
            return self._annotate_rebase_policy_failure(
                JobResult(
                    ok=False,
                    value={"failure_kind": "validation_runner"},
                    error="rebase structural validation command is missing",
                ),
                policy,
                "structural validation",
            )
        relative_test = next(
            (part for part in argv if part.endswith(".py")),
            None,
        )
        if relative_test is None:
            return self._annotate_rebase_policy_failure(
                JobResult(
                    ok=False,
                    value={"failure_kind": "validation_runner"},
                    error="rebase structural validation test is missing",
                ),
                policy,
                "structural validation",
            )
        test_path = cwd / relative_test
        if not test_path.is_file():
            return self._annotate_rebase_policy_failure(
                JobResult(
                    ok=False,
                    value={"failure_kind": "validation_runner"},
                    error=f"rebase structural validation test is not in the tree: {relative_test}",
                ),
                policy,
                "structural validation",
            )
        source_sha = self._read_publish_head(cwd, timeout=timeout)
        if isinstance(source_sha, JobResult):
            return self._annotate_rebase_policy_failure(
                JobResult(
                    ok=False,
                    value={"failure_kind": "validation_runner"},
                    error="rebase structural validation could not bind the rebased head",
                ),
                policy,
                "structural validation",
            )
        result = self._run_immutable_build_test(
            BuildTestJob(
                repo="rebase-structural-validation",
                cwd=cwd,
                argv=argv,
                timeout_s=timeout,
                expected_head_sha=source_sha,
                immutable_source=True,
                descr="rebase_structural_validation",
            )
        )
        if result.ok:
            return None
        return self._annotate_rebase_policy_failure(result, policy, "structural validation")

    def _git_continue_rebase(self, job: GitJob) -> JobResult:
        """Validate edit-only conflict output, finish policy rebase, and lease-publish."""
        parsed = self._parse_rebase_continuation(job)
        if isinstance(parsed, JobResult):
            return parsed
        (
            cwd,
            branch,
            remote,
            base_sha,
            expected_remote_sha,
            paths,
            snapshot,
            index_snapshot,
            paused_head_sha,
        ) = parsed
        remote_head = self._read_remote_branch_head(
            cwd,
            remote=remote,
            branch=branch,
            expected_repo=job.transport_repository,
            timeout=job.timeout_s,
        )
        if isinstance(remote_head, JobResult):
            return remote_head
        if remote_head != expected_remote_sha:
            return JobResult(
                ok=False, error="remote writer head changed during conflict resolution"
            )
        edits = self._validate_rebase_conflict_edits(
            cwd,
            remote=remote,
            paths=paths,
            snapshot=snapshot,
            index_snapshot=index_snapshot,
            paused_head_sha=paused_head_sha,
            base_sha=base_sha,
            expected_remote_sha=expected_remote_sha,
            timeout=job.timeout_s,
        )
        if edits is not None:
            return edits
        continued = self._continue_rebase_process(
            cwd,
            remote=remote,
            base_sha=base_sha,
            expected_remote_sha=expected_remote_sha,
            paths=paths,
            timeout=job.timeout_s,
        )
        if continued is not None:
            return continued
        policy = self._select_rebase_policy(job.repo)
        structural = self._run_rebase_structural_validation(
            cwd,
            timeout=job.timeout_s,
            policy=policy,
        )
        if structural is not None:
            return structural
        semantic = self._validate_rebased_tree(cwd, policy=policy)
        if semantic is not None:
            return semantic
        metadata = self._verify_rebased_commit_metadata(
            cwd, base_sha=base_sha, timeout=job.timeout_s
        )
        if metadata is not None:
            return metadata
        source_sha = self._read_publish_head(cwd, timeout=job.timeout_s)
        if isinstance(source_sha, JobResult):
            return source_sha
        if source_sha == expected_remote_sha:
            return JobResult(ok=False, error="completed rebase did not rewrite the branch head")

        revalidate_remote = self._authenticated_remote_revalidator(
            cwd=cwd, expected_repo=job.transport_repository, timeout=job.timeout_s
        )
        remote_env, remote_config = revalidate_remote()
        git_utils.push_head_to_branch(
            branch,
            expected_remote_sha,
            cwd,
            source_sha=source_sha,
            timeout=job.timeout_s,
            env=remote_env,
            remote_config=remote_config,
            revalidate_remote=revalidate_remote,
        )
        return JobResult(
            ok=True,
            value={
                "rebased": True,
                "published": True,
                "head_sha": source_sha,
                "rebase_policy": policy.name if policy is not None else None,
            },
        )

    @staticmethod
    def _parse_rebase_continuation(
        job: GitJob,
    ) -> tuple[Path, str, str, str, str, tuple[str, ...], dict[str, object], str, str] | JobResult:
        """Validate and normalize coordinator-owned continuation arguments."""
        kwargs = job.kwargs
        cwd = Path(str(kwargs.get("cwd") or ""))
        branch = str(kwargs.get("branch") or "")
        remote = str(kwargs.get("remote") or "origin")
        base_sha = kwargs.get("base_sha")
        expected_remote_sha = kwargs.get("expected_remote_sha")
        raw_paths = kwargs.get("conflict_paths")
        raw_snapshot = kwargs.get("conflict_snapshot")
        index_snapshot = kwargs.get("conflict_index_snapshot")
        paused_head_sha = kwargs.get("paused_head_sha")
        if (
            not cwd.is_dir()
            or not branch
            or not _is_full_commit_sha(base_sha)
            or not _is_full_commit_sha(expected_remote_sha)
            or not isinstance(raw_paths, (list, tuple))
            or not raw_paths
            or not isinstance(raw_snapshot, dict)
            or not isinstance(index_snapshot, str)
            or re.fullmatch(r"[0-9a-f]{64}", index_snapshot) is None
            or not _is_full_commit_sha(paused_head_sha)
        ):
            return JobResult(ok=False, error="rebase continuation arguments invalid")
        paths = tuple(str(path) for path in raw_paths)
        if any(not is_safe_scope_retraction_path(path) for path in paths):
            return JobResult(ok=False, error="rebase continuation paths invalid")
        snapshot = {str(path): value for path, value in raw_snapshot.items()}
        return (
            cwd,
            branch,
            remote,
            base_sha,
            expected_remote_sha,
            paths,
            snapshot,
            index_snapshot,
            paused_head_sha,
        )

    def _validate_rebase_conflict_edits(
        self,
        cwd: Path,
        *,
        remote: str,
        paths: tuple[str, ...],
        snapshot: dict[str, object],
        index_snapshot: str,
        paused_head_sha: str,
        base_sha: str,
        expected_remote_sha: str,
        timeout: int,
    ) -> JobResult | None:
        """Reject out-of-band index edits, no-op agents, and residual markers."""
        current_receipt = self._conflict_receipt(
            cwd,
            remote=remote,
            base_branch="main",
            expected_remote_sha=expected_remote_sha,
            timeout=timeout,
        )
        if isinstance(current_receipt, JobResult):
            return current_receipt
        current_receipt["base_sha"] = base_sha
        raw_current_paths = current_receipt.get("conflict_paths")
        current_paths: tuple[str, ...] = (
            tuple(str(path) for path in raw_current_paths)
            if isinstance(raw_current_paths, (list, tuple))
            else ()
        )
        if set(current_paths) != set(paths):
            return JobResult(ok=False, error="conflict index was mutated outside host ownership")
        if current_receipt.get("conflict_index_snapshot") != index_snapshot:
            return JobResult(ok=False, error="conflict index was mutated outside host ownership")
        if current_receipt.get("paused_head_sha") != paused_head_sha:
            return JobResult(ok=False, error="paused rebase head changed outside host ownership")
        current_snapshot = current_receipt.get("conflict_snapshot")
        if not isinstance(current_snapshot, dict) or all(
            current_snapshot.get(path) == snapshot.get(path) for path in paths
        ):
            return JobResult(
                ok=False,
                value=current_receipt,
                error="rebase conflict resolution required: agent made no file changes",
            )
        marker = re.compile(rb"^(<<<<<<<|=======|>>>>>>>)", re.MULTILINE)
        if any(
            (cwd / path).is_file() and marker.search((cwd / path).read_bytes()) for path in paths
        ):
            return JobResult(
                ok=False,
                value=current_receipt,
                error="rebase conflict resolution required: conflict markers remain",
            )
        scope_error = self._rebase_conflict_edit_scope_error(
            cwd,
            conflict_paths=paths,
            timeout=timeout,
        )
        if scope_error is not None:
            return scope_error
        return None

    @staticmethod
    def _rebase_conflict_edit_scope_error(
        cwd: Path, *, conflict_paths: tuple[str, ...], timeout: int
    ) -> JobResult | None:
        """Reject unstaged tracked or untracked changes outside conflicts.

        The caller already proves that the complete index is byte-for-byte
        unchanged from the host-owned paused-rebase snapshot.  Inspecting the
        cached diff here would misclassify clean paths staged by Git before the
        agent turn as agent edits.
        """
        allowed = set(conflict_paths)
        probes = (
            ["git", "diff", "--name-only", "-z"],
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        )
        try:
            for argv in probes:
                result = git_utils.run(argv, cwd=cwd, timeout=timeout)
                changed = {path for path in result.stdout.split("\0") if path}
                if not changed.issubset(allowed):
                    return JobResult(
                        ok=False,
                        error="rebase conflict resolution changed paths outside host scope",
                    )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return JobResult(ok=False, error="cannot validate rebase conflict edit scope")
        return None

    def _continue_rebase_process(
        self,
        cwd: Path,
        *,
        remote: str,
        base_sha: str,
        expected_remote_sha: str,
        paths: tuple[str, ...],
        timeout: int,
    ) -> JobResult | None:
        """Stage only validated conflicts and let Git continue the policy rebase."""
        env = _controlled_git_signing_env(cwd, timeout=timeout)
        if isinstance(env, JobResult):
            return env
        env["GIT_EDITOR"] = "true"
        phase = "stage_conflicts"
        try:
            git_utils.run(["git", "add", "--", *paths], cwd=cwd, timeout=timeout)
            phase = "validate_index"
            git_utils.run(
                ["git", "diff", "--cached", "--check"],
                cwd=cwd,
                timeout=timeout,
            )
            phase = "rebase_continue"
            git_utils.run(
                ["git", "rebase", "--continue"],
                cwd=cwd,
                env=env,
                timeout=timeout,
            )
        except subprocess.CalledProcessError as exc:
            next_receipt: dict[str, object] | JobResult | None = None
            if phase == "rebase_continue":
                next_receipt = self._conflict_receipt(
                    cwd,
                    remote=remote,
                    base_branch="main",
                    expected_remote_sha=expected_remote_sha,
                    timeout=timeout,
                )
                if isinstance(next_receipt, dict):
                    next_receipt["base_sha"] = base_sha
                    return JobResult(
                        ok=False,
                        value=next_receipt,
                        error="rebase conflict resolution required: additional conflicts found",
                    )
            stdout_tail = bounded_git_diagnostic(exc.stdout, limit=_TAIL)
            stderr_tail = bounded_git_diagnostic(exc.stderr, limit=_TAIL)
            diagnostic = f"{stdout_tail}\n{stderr_tail}".lower()
            signing_failure = any(
                marker in diagnostic
                for marker in (
                    "cannot run gpg",
                    "failed to sign",
                    "gpg failed",
                    "failed to write commit object",
                )
            )
            failure_kind = "signing" if signing_failure else "continuation"
            receipt_error = next_receipt.error if isinstance(next_receipt, JobResult) else None
            return JobResult(
                ok=False,
                value={
                    "failure_kind": failure_kind,
                    "phase": phase,
                    "returncode": exc.returncode,
                    "receipt_error": receipt_error,
                },
                error=(
                    "host rebase continuation signing failed"
                    if signing_failure
                    else f"host rebase {phase} failed"
                ),
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
            )
        return None

    @staticmethod
    def _verify_rebased_commit_metadata(
        cwd: Path, *, base_sha: str, timeout: int
    ) -> JobResult | None:
        """Prove captured-base ancestry plus signature and DCO metadata."""
        ancestry = git_utils.run(
            ["git", "merge-base", "--is-ancestor", str(base_sha), "HEAD"],
            cwd=cwd,
            check=False,
            timeout=timeout,
        )
        if ancestry.returncode != 0:
            return JobResult(ok=False, error="completed rebase lacks captured base ancestry")
        commits = git_utils.run(
            ["git", "rev-list", "--reverse", f"{base_sha}..HEAD"],
            cwd=cwd,
            timeout=timeout,
        ).stdout.split()
        if not commits:
            return JobResult(ok=False, error="completed rebase produced no branch commits")
        for commit in commits:
            raw_commit = git_utils.run(
                ["git", "cat-file", "-p", commit],
                cwd=cwd,
                timeout=timeout,
            ).stdout
            if "\ngpgsig " not in f"\n{raw_commit}" or "Signed-off-by:" not in raw_commit:
                return JobResult(ok=False, error="completed rebase commit metadata invalid")
        return None

    def _git_sync_checkout(self, job: GitJob) -> JobResult:
        """Validate and fast-forward a clean reusable checkout.

        Tracked staged or unstaged changes block synchronization. Untracked
        files are left in place because issue work runs in isolated worktrees.
        """
        expected_repo = str(job.kwargs.get("repo") or "")
        dest = str(job.kwargs.get("dest") or "")
        if not expected_repo or not dest:
            return JobResult(
                ok=False,
                error="sync_checkout requires non-empty 'repo' and 'dest' kwargs",
            )

        checkout = Path(dest)
        if not checkout.is_dir():
            return JobResult(ok=False, error=f"checkout does not exist: {checkout}")
        # This read-only security preflight must run before acquiring a lock
        # below: creating a lock file can otherwise create ``.git`` in a
        # malformed directory and change how the preflight probes it.
        if preflight_error := _checkout_preflight_error(checkout, job.timeout_s):
            return JobResult(ok=False, error=preflight_error)

        metadata_lock = WorktreeManager.git_metadata_lock_path(checkout)
        with _interruptible_file_lock(
            metadata_lock,
            shutdown=self._shutdown,
            timeout_s=job.timeout_s,
        ):
            return self._sync_checkout_locked(
                checkout=checkout,
                expected_repo=expected_repo,
                timeout_s=job.timeout_s,
            )

    def _sync_checkout_locked(
        self,
        *,
        checkout: Path,
        expected_repo: str,
        timeout_s: int,
    ) -> JobResult:
        """Validate and synchronize one checkout while its metadata lock is held."""
        origin = git_utils.run(
            ["git", "remote", "get-url", "origin"],
            cwd=checkout,
            timeout=timeout_s,
            env=_controlled_git_env(),
        ).stdout.strip()
        normalized_origin = origin.rstrip("/").removesuffix(".git")
        expected_origins = {
            f"https://github.com/{expected_repo}",
            f"ssh://git@github.com/{expected_repo}",
            f"git@github.com:{expected_repo}",
        }
        if normalized_origin not in expected_origins:
            return JobResult(
                ok=False,
                error=f"checkout has unexpected origin; expected origin {expected_repo}",
            )

        status = git_utils.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain",
                "--untracked-files=no",
            ],
            cwd=checkout,
            timeout=timeout_s,
            env=_controlled_git_env(),
        )
        if status.stdout.strip():
            return JobResult(
                ok=False,
                error=f"checkout has uncommitted changes: {checkout}: {status.stdout.strip()}",
            )
        branch_result = git_utils.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=checkout,
            check=False,
            log_errors=False,
            timeout=timeout_s,
            env=_controlled_git_env(),
        )
        branch = branch_result.stdout.strip()
        if branch_result.returncode != 0 or not branch:
            return JobResult(ok=False, error=f"checkout is detached: {checkout}")
        gh_command = _trusted_gh_executable(self._gh_extra_path_root)
        if gh_command is None:
            return JobResult(
                ok=False,
                error=(
                    "required GitHub executable is unavailable; pass "
                    "--gh-extra-path-root ROOT when ROOT/bin/gh is the intended installation"
                ),
            )
        default_branch = git_utils.run(
            [gh_command, "api", f"repos/{expected_repo}", "--jq", ".default_branch"],
            cwd=checkout,
            timeout=timeout_s,
            env=_controlled_git_env(),
        ).stdout.strip()
        if not default_branch:
            return JobResult(ok=False, error=f"repository has no default branch: {expected_repo}")
        return self._fast_forward_checkout(
            checkout=checkout,
            default_branch=default_branch,
            gh_command=gh_command,
            timeout_s=timeout_s,
        )

    @staticmethod
    def _checkout_state_error(
        *, checkout: Path, timeout_s: int, default_branch: str | None = None
    ) -> str | None:
        """Return the clean, attached-checkout validation error, if any."""
        del default_branch
        status = git_utils.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain",
                "--untracked-files=no",
            ],
            cwd=checkout,
            timeout=timeout_s,
            env=_controlled_git_env(),
        )
        if status.stdout.strip():
            return f"checkout has uncommitted changes: {checkout}: {status.stdout.strip()}"
        branch_result = git_utils.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=checkout,
            check=False,
            log_errors=False,
            timeout=timeout_s,
            env=_controlled_git_env(),
        )
        branch = branch_result.stdout.strip()
        if branch_result.returncode != 0 or not branch:
            return f"checkout is detached: {checkout}"
        return None

    @staticmethod
    def _fast_forward_checkout(
        *,
        checkout: Path,
        default_branch: str,
        gh_command: str,
        timeout_s: int,
    ) -> JobResult:
        """Fetch and fast-forward a validated checkout while its metadata is locked."""
        hooks_disabled = f"core.hooksPath={os.devnull}"
        remote_config = _trusted_remote_git_config(gh_command)
        if remote_config is None:
            return JobResult(ok=False, error="required fetch executable is unavailable")
        fetch_config = (
            "-c",
            hooks_disabled,
            *remote_config,
        )
        git_utils.run(
            [
                "git",
                *fetch_config,
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                # The preflight validates origin before this point.  Fetch it
                # by name so a remote URL never reaches command debug logs.
                "origin",
                f"+refs/heads/{default_branch}:refs/remotes/origin/{default_branch}",
            ],
            cwd=checkout,
            timeout=timeout_s,
            env=_controlled_git_env(),
        )
        if validation_error := WorkerPool._checkout_state_error(
            checkout=checkout,
            timeout_s=timeout_s,
        ):
            return JobResult(ok=False, error=validation_error)
        relation = git_utils.run(
            [
                "git",
                "rev-list",
                "--left-right",
                "--count",
                f"HEAD...origin/{default_branch}",
            ],
            cwd=checkout,
            timeout=timeout_s,
            env=_controlled_git_env(),
        ).stdout.split()
        if len(relation) != 2:
            return JobResult(ok=False, error=f"could not compare checkout history: {checkout}")
        try:
            ahead, _behind = (int(count) for count in relation)
        except ValueError:
            return JobResult(ok=False, error=f"could not compare checkout history: {checkout}")
        if ahead:
            return JobResult(
                ok=False,
                error=f"checkout has local commits beyond origin/{default_branch}: {checkout}",
            )

        merge = git_utils.run(
            [
                "git",
                "-c",
                hooks_disabled,
                "-c",
                "core.fsmonitor=false",
                "merge",
                "--ff-only",
                f"origin/{default_branch}",
            ],
            cwd=checkout,
            check=False,
            log_errors=False,
            timeout=timeout_s,
            env=_controlled_git_env(),
        )
        if merge.returncode != 0:
            return JobResult(
                ok=False,
                error=(f"checkout cannot fast-forward {default_branch} to origin/{default_branch}"),
            )
        if validation_error := WorkerPool._checkout_state_error(
            checkout=checkout,
            timeout_s=timeout_s,
        ):
            return JobResult(ok=False, error=validation_error)
        synced_heads = git_utils.run(
            ["git", "rev-parse", "HEAD", f"origin/{default_branch}"],
            cwd=checkout,
            timeout=timeout_s,
            env=_controlled_git_env(),
        ).stdout.split()
        if len(synced_heads) != 2 or synced_heads[0] != synced_heads[1]:
            return JobResult(
                ok=False,
                error=f"checkout did not reach origin/{default_branch}: {checkout}",
            )
        synced_head = synced_heads[0]
        if not _is_full_commit_sha(synced_head):
            return JobResult(
                ok=False,
                error=f"checkout returned malformed default-branch SHA: {checkout}",
            )
        return JobResult(ok=True, value=synced_head)

    def _git_create_worktree(self, job: GitJob) -> JobResult:
        """Create a worktree, holding one implementation-writer handoff."""
        kwargs = dict(job.kwargs)
        if kwargs.get("source_lane") != "impl":
            return self._git_create_worktree_with_handoff(job, None, None)
        repo_root_kwarg = kwargs.get("repo_root")
        repo_root = Path(repo_root_kwarg) if repo_root_kwarg else get_repo_root()
        item_number = kwargs.get("issue_number")
        if isinstance(item_number, bool) or not isinstance(item_number, int):
            return JobResult(
                ok=False,
                error=(
                    "source_workspace_ownership_unavailable: "
                    "implementation writer item number is invalid"
                ),
            )
        source_manager = SourceWorkspaceManager(
            repo_root,
            repository=job.repo or job.transport_repository,
            base_dir=repo_root / "build" / ".worktrees",
        )
        try:
            with source_manager.implementation_writer_handoff(item_number) as handoff:
                recovered = self._recover_prepared_remediation_worktree(job, repo_root)
                if recovered is not None:
                    return recovered
                return self._git_create_worktree_with_handoff(job, source_manager, handoff)
        except SourceWorkspaceError as exc:
            return JobResult(
                ok=False,
                error=f"source_workspace_ownership_unavailable: {exc}",
            )

    def _recover_prepared_remediation_worktree(  # noqa: C901
        self,
        job: GitJob,
        repo_root: Path,
    ) -> JobResult | None:
        """Return one exact durable prepared child before adopted-branch sync."""
        if job.kwargs.get("recover_prepared_remediation") is not True:
            return None
        issue_number = job.kwargs.get("issue_number")
        pr_number = job.kwargs.get("remediation_pr_number")
        repository = job.kwargs.get("remediation_repository")
        branch = job.kwargs.get("branch_name")
        remote_head = job.kwargs.get("implementation_adoption_head")
        threads = job.kwargs.get("remediation_thread_snapshots")
        if (
            isinstance(issue_number, bool)
            or not isinstance(issue_number, int)
            or isinstance(pr_number, bool)
            or not isinstance(pr_number, int)
            or not isinstance(repository, str)
            or not isinstance(branch, str)
            or not _is_full_commit_sha(remote_head)
            or not isinstance(threads, list)
        ):
            return JobResult(ok=False, error="prepared remediation recovery identity is invalid")
        try:
            thread_json = RemediationReviewInput.canonical_thread_snapshot(threads)
            intent = load_prepublication_intent(
                repo_root=repo_root,
                repository=repository,
                issue_number=issue_number,
                pr_number=pr_number,
                branch=branch,
                expected_remote_sha=remote_head,
                thread_snapshot_json=thread_json,
            )
            if intent is not None:
                child = read_prepublication_private_head(
                    repo_root=repo_root,
                    pr_number=pr_number,
                )
                worktree = Path(intent.worktree_path)
                expected_path = (
                    repo_root / "build" / ".worktrees" / source_worktree_name(issue_number, "impl")
                ).resolve(strict=False)
                if worktree.resolve(strict=True) != expected_path:
                    raise ValueError("prepared remediation writer path is invalid")
                linked_env = _linked_worktree_git_env(repo_root.resolve(strict=True), worktree)
                binding = getattr(linked_env, "binding", None)
                if (
                    not isinstance(binding, _LinkedWorktreeBinding)
                    or binding.branch_ref != f"refs/heads/{intent.branch}"
                    or binding.branch_sha != intent.expected_remote_sha
                ):
                    raise ValueError("prepared remediation writer binding is invalid")
                if child == intent.expected_remote_sha:
                    (
                        current,
                        status_result,
                        candidate_tree,
                        diff_result,
                        selected_paths,
                    ) = _inspect_candidate_with_private_git(
                        worktree,
                        intent.expected_remote_sha,
                        timeout=job.timeout_s,
                        linked_env=linked_env,
                    )
                    if (
                        not status_result.text.strip()
                        or selected_paths is None
                        or current.snapshot != dict(intent.content_snapshot)
                        or candidate_tree != intent.candidate_tree_sha
                        or diff_result.text != intent.committed_diff
                        or diff_result.sha256 != intent.committed_diff_sha256
                        or selected_paths.add_paths != intent.add_paths
                        or selected_paths.update_paths != intent.update_paths
                    ):
                        raise ValueError("prepared remediation intent candidate changed")
                    inspection = {
                        "outcome": "dirty",
                        "branch": intent.branch,
                        "worktree_path": intent.worktree_path,
                        "head_sha": intent.expected_remote_sha,
                        "status": status_result.text,
                        "diff": diff_result.text,
                        "status_sha256": status_result.sha256,
                        "diff_sha256": diff_result.sha256,
                        "candidate_tree_sha": candidate_tree,
                        "content_snapshot": current.snapshot,
                        "changed_file_count": current.changed_file_count,
                        "candidate_add_paths": list(selected_paths.add_paths),
                        "candidate_update_paths": list(selected_paths.update_paths),
                    }
                    return JobResult(
                        ok=True,
                        value={
                            "path": str(worktree),
                            "impl_source_revision": intent.expected_remote_sha,
                            "branch": intent.branch,
                            "head_sha": intent.expected_remote_sha,
                            "dirty": True,
                            "status": status_result.text,
                            "diff": diff_result.text,
                            "content_snapshot": current.snapshot,
                            "incomplete_remediation_inspection": inspection,
                            "remediation_batch_nonce": intent.batch_nonce,
                        },
                    )
                with _private_linked_worktree_git_env(
                    linked_env,
                    detached_head=intent.expected_remote_sha,
                ) as parent_env:
                    current_snapshot = _dirty_worktree_content_snapshot(
                        worktree,
                        timeout=job.timeout_s,
                        git_env=parent_env,
                    )
                linked_env = _linked_worktree_git_env(repo_root.resolve(strict=True), worktree)
                index_is_original = current_snapshot == dict(intent.content_snapshot)
                index_tree = git_utils.run(
                    ["git", "write-tree"],
                    cwd=worktree,
                    timeout=job.timeout_s,
                    env=linked_env,
                ).stdout.strip()
                if not index_is_original and index_tree != intent.candidate_tree_sha:
                    raise ValueError("prepared remediation writer index changed")
                linked_env = _linked_worktree_git_env(repo_root.resolve(strict=True), worktree)
                durable_git_dir = prepublication_private_git_dir(
                    repo_root=repo_root,
                    pr_number=pr_number,
                    create=False,
                )
                with _private_linked_worktree_git_env(
                    linked_env,
                    detached_head=child,
                    durable_git_dir=durable_git_dir,
                ) as child_env:
                    if not self._is_exact_recovery_commit(
                        worktree,
                        child,
                        parent=intent.expected_remote_sha,
                        tree=intent.candidate_tree_sha,
                        timeout=job.timeout_s,
                        git_env=child_env,
                    ):
                        raise ValueError("prepared remediation private child is not exact")
                    with tempfile.TemporaryDirectory(
                        prefix="hephaestus-recovery-verify-index-"
                    ) as temporary:
                        verify_env = dict(child_env)
                        verify_env["GIT_INDEX_FILE"] = str(Path(temporary) / "index")
                        git_utils.run(
                            ["git", "read-tree", child],
                            cwd=worktree,
                            timeout=job.timeout_s,
                            env=verify_env,
                        )
                        status = _run_bounded_git_output(
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
                            timeout=job.timeout_s,
                            max_bytes=IMPLEMENTATION_INSPECTION_STATUS_MAX_BYTES,
                            retain_text=True,
                            env=verify_env,
                        ).text
                    if status:
                        raise ValueError("prepared remediation writer content changed")
                    _refresh_verified_recovery_index(
                        repo_root.resolve(strict=True),
                        worktree,
                        expected_git_env=child_env,
                        private_git_env=child_env,
                        source_sha=child,
                        expected_tree=intent.candidate_tree_sha,
                        timeout=job.timeout_s,
                    )
                recovered_receipt = intent.receipt(child)
                save_prepublication_receipt(
                    repo_root=repo_root,
                    receipt=recovered_receipt,
                    batch_nonce=intent.batch_nonce,
                )
            loaded = load_prepublication_receipt(
                repo_root=repo_root,
                repository=repository,
                issue_number=issue_number,
                pr_number=pr_number,
                branch=branch,
                expected_remote_sha=remote_head,
                thread_snapshot_json=thread_json,
            )
            if loaded is None:
                expected_path = (
                    repo_root / "build" / ".worktrees" / source_worktree_name(issue_number, "impl")
                ).resolve(strict=False)
                if expected_path.exists():
                    linked_env = _linked_worktree_git_env(
                        repo_root.resolve(strict=True),
                        expected_path,
                    )
                    binding = getattr(linked_env, "binding", None)
                    if not isinstance(binding, _LinkedWorktreeBinding):
                        raise ValueError("prepared remediation writer binding is invalid")
                    if (
                        binding.branch_ref == f"refs/heads/{branch}"
                        and binding.branch_sha != remote_head
                    ):
                        raise ValueError(
                            "prepared remediation receipt is absent for a preserved local child"
                        )
                return None
            receipt, batch_nonce, already_published = loaded
            review_input = receipt.review_input
            worktree = Path(review_input.worktree_path)
            expected_path = (
                repo_root / "build" / ".worktrees" / source_worktree_name(issue_number, "impl")
            ).resolve(strict=False)
            if worktree.resolve(strict=True) != expected_path:
                raise ValueError("prepared remediation writer path is invalid")
            linked_env = _linked_worktree_git_env(repo_root.resolve(strict=True), worktree)
            binding = getattr(linked_env, "binding", None)
            if (
                not isinstance(binding, _LinkedWorktreeBinding)
                or binding.branch_ref != f"refs/heads/{branch}"
                or binding.branch_sha
                not in {
                    review_input.reviewed_parent_sha,
                    review_input.recovery_commit_sha,
                }
                or not self._is_exact_recovery_commit(
                    worktree,
                    review_input.recovery_commit_sha,
                    parent=review_input.reviewed_parent_sha,
                    tree=review_input.candidate_tree_sha,
                    timeout=job.timeout_s,
                    git_env=linked_env,
                )
            ):
                raise ValueError("prepared remediation commit is not exact")
            if (
                git_utils.run(
                    ["git", "write-tree"],
                    cwd=worktree,
                    timeout=job.timeout_s,
                    env=linked_env,
                ).stdout.strip()
                != review_input.candidate_tree_sha
            ):
                raise ValueError("prepared remediation writer index changed")
            linked_env = _linked_worktree_git_env(repo_root.resolve(strict=True), worktree)
            with _private_linked_worktree_git_env(
                linked_env,
                detached_head=review_input.recovery_commit_sha,
            ) as child_env:
                with tempfile.TemporaryDirectory(
                    prefix="hephaestus-recovery-verify-index-"
                ) as temporary:
                    verify_env = dict(child_env)
                    verify_env["GIT_INDEX_FILE"] = str(Path(temporary) / "index")
                    git_utils.run(
                        ["git", "read-tree", review_input.recovery_commit_sha],
                        cwd=worktree,
                        timeout=job.timeout_s,
                        env=verify_env,
                    )
                    status = _run_bounded_git_output(
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
                        timeout=job.timeout_s,
                        max_bytes=IMPLEMENTATION_INSPECTION_STATUS_MAX_BYTES,
                        retain_text=True,
                        env=verify_env,
                    ).text
                    if status:
                        raise ValueError("prepared remediation writer content changed")
                committed = _run_bounded_git_output(
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
                        review_input.reviewed_parent_sha,
                        review_input.candidate_tree_sha,
                    ),
                    cwd=worktree,
                    timeout=job.timeout_s,
                    max_bytes=IMPLEMENTATION_INSPECTION_DIFF_MAX_BYTES,
                    retain_text=True,
                    env=child_env,
                )
                if (
                    committed.text != review_input.committed_diff
                    or committed.sha256 != review_input.committed_diff_sha256
                ):
                    raise ValueError("prepared remediation committed diff changed")
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
            return JobResult(ok=False, error=f"prepared remediation recovery failed: {exc}")
        return JobResult(
            ok=True,
            value={
                "path": str(worktree),
                "impl_source_revision": review_input.reviewed_parent_sha,
                "dirty": False,
                "status": "",
                "diff": "",
                "prepared_remediation_receipt": receipt.as_dict(),
                "remediation_batch_nonce": batch_nonce,
                "remediation_recovery_already_published": already_published,
            },
        )

    def _git_create_worktree_with_handoff(  # noqa: C901
        self,
        job: GitJob,
        source_manager: SourceWorkspaceManager | None,
        implementation_writer_handoff: ImplementationWriterHandoff | None,
    ) -> JobResult:
        """Create a worktree and optionally sync an adopted PR branch."""
        kwargs = dict(job.kwargs)
        sync_to_remote = bool(kwargs.pop("sync_to_remote", False))
        pr_number = kwargs.pop("pr_number", None)
        repo_root_kwarg = kwargs.pop("repo_root", None)
        repo_root = Path(repo_root_kwarg) if repo_root_kwarg else get_repo_root()
        try:
            direct_setup = self._prepare_direct_scope_worktree(
                kwargs=kwargs,
                sync_to_remote=sync_to_remote,
                repo_root=repo_root,
                expected_repo=job.transport_repository,
                timeout_s=job.timeout_s,
            )
        except git_utils.DirectBranchReservationCollisionError as exc:
            # The post-failure remote probe proved another branch now owns
            # this absent-only reservation.  Preserve that type across the
            # worker boundary so Implementation terminalizes it rather than
            # spending its generic transport retry budget (or an agent job).
            return JobResult(
                ok=False,
                error="direct_scope_reservation_collision",
                value={"direct_scope_reservation_collision": {"branch": exc.branch_name}},
            )
        if isinstance(direct_setup, JobResult):
            return direct_setup
        base_sha, branch_name = direct_setup
        base_dir = repo_root / "build" / ".worktrees"
        implementation_adoption_head = kwargs.get("implementation_adoption_head")
        adopting_implementation_writer = implementation_adoption_head is not None
        if adopting_implementation_writer and (
            kwargs.get("source_lane") != "impl"
            or not sync_to_remote
            or not _is_full_commit_sha(implementation_adoption_head)
        ):
            return JobResult(ok=False, error="implementation writer adoption head is invalid")

        implementation_source_lane = kwargs.get("source_lane") == "impl"
        if isinstance(base_sha, str):
            remote_env: dict[str, str] | None = None
            remote_config: tuple[str, ...] = ()
            if implementation_source_lane:
                remote_env, remote_config = self._authenticated_remote_git_configuration(
                    cwd=repo_root,
                    expected_repo=job.transport_repository,
                    timeout=job.timeout_s,
                )
            manager = WorktreeManager(
                base_dir=base_dir,
                base_branch=base_sha,
                repo_root=repo_root,
                remote_git_env=remote_env,
                remote_git_config=remote_config,
            )
        elif (
            bool(kwargs.get("refresh_base", False))
            or adopting_implementation_writer
            or implementation_source_lane
        ):
            remote_env, remote_config = self._authenticated_remote_git_configuration(
                cwd=repo_root,
                expected_repo=job.transport_repository,
                timeout=job.timeout_s,
            )
            manager = WorktreeManager(
                base_dir=base_dir,
                repo_root=repo_root,
                remote_git_env=remote_env,
                remote_git_config=remote_config,
            )
        else:
            manager = WorktreeManager(base_dir=base_dir, repo_root=repo_root)
        if base_sha is not None:
            kwargs["base_sha"] = base_sha
            kwargs["remote_branch_reserved"] = True
        if implementation_writer_handoff is not None:
            kwargs["implementation_writer_handoff"] = implementation_writer_handoff
        if (
            implementation_source_lane
            and source_manager is not None
            and implementation_writer_handoff is not None
            and (base_sha is not None or adopting_implementation_writer)
        ):
            try:
                item_number = cast(int, kwargs["issue_number"])
                if adopting_implementation_writer:
                    source_manager.authorize_adopted_implementation_writer_transition(
                        item_number,
                        branch=branch_name,
                        expected_head=cast(str, implementation_adoption_head),
                        handoff=implementation_writer_handoff,
                    )
                else:
                    source_manager.authorize_direct_implementation_writer_transition(
                        item_number,
                        branch=branch_name,
                        base_sha=cast(str, base_sha),
                        handoff=implementation_writer_handoff,
                    )
            except SourceWorkspaceError as exc:
                return self._creation_receipt_failure(
                    base_dir=base_dir,
                    item_number=kwargs.get("issue_number"),
                    exc=exc,
                    branch_name=branch_name,
                    base_sha=base_sha,
                )
        created_or_failure = self._create_managed_worktree(
            manager=manager,
            kwargs=kwargs,
            base_dir=base_dir,
            base_sha=base_sha,
            branch_name=branch_name,
            repo_root=repo_root,
            expected_repo=job.transport_repository,
            timeout_s=job.timeout_s,
        )
        if isinstance(created_or_failure, JobResult):
            return created_or_failure
        created = created_or_failure
        try:
            writer_authority = (
                manager.implementation_writer_authority(Path(created))
                if kwargs.get("source_lane") == "impl"
                and kwargs.get("implementation_adoption_head") is None
                and created is not None
                else None
            )
        except WorktreeCreationReceiptError as exc:
            return self._creation_receipt_failure(
                base_dir=base_dir,
                item_number=kwargs.get("issue_number"),
                exc=exc,
                branch_name=branch_name,
                base_sha=base_sha,
            )
        return self._finalize_created_worktree(
            created=created,
            base_sha=base_sha,
            branch_name=branch_name,
            repo_root=repo_root,
            repo=job.transport_repository,
            source_repository=job.repo,
            sync_to_remote=sync_to_remote,
            pr_number=pr_number,
            source_lane=kwargs.get("source_lane"),
            item_number=kwargs.get("issue_number"),
            writer_authority=writer_authority,
            worktree_manager=manager,
            implementation_adoption_head=kwargs.get("implementation_adoption_head"),
            source_manager=source_manager,
            implementation_writer_handoff=implementation_writer_handoff,
            timeout_s=job.timeout_s,
        )

    def _create_managed_worktree(
        self,
        *,
        manager: WorktreeManager,
        kwargs: dict[str, Any],
        base_dir: Path,
        base_sha: str | None,
        branch_name: str,
        repo_root: Path,
        expected_repo: str,
        timeout_s: int,
    ) -> Path | JobResult:
        """Create a worktree and preserve typed writer-receipt failures."""
        try:
            return manager.create_worktree(**kwargs, timeout=timeout_s)
        except (RemoteGitRefreshError, subprocess.CalledProcessError):
            if bool(kwargs.get("refresh_base", False)):
                return JobResult(
                    ok=False,
                    error="worktree remote refresh failed",
                    value={"failure_kind": "remote_git_transport"},
                )
            raise
        except WorktreeCreationReceiptError as exc:
            return self._creation_receipt_failure(
                base_dir=base_dir,
                item_number=kwargs.get("issue_number"),
                exc=exc,
                branch_name=branch_name,
                base_sha=base_sha,
            )
        except Exception as exc:
            if base_sha is not None:
                return self._rollback_direct_scope_reservation(
                    branch_name=branch_name,
                    base_sha=base_sha,
                    repo_root=repo_root,
                    expected_repo=expected_repo,
                    timeout_s=timeout_s,
                    error=f"worktree creation failed: {exc}",
                )
            raise

    @staticmethod
    def _creation_receipt_failure(
        *,
        base_dir: Path,
        item_number: object,
        exc: Exception,
        branch_name: str,
        base_sha: str | None,
    ) -> JobResult:
        """Preserve a materialized writer when its ownership proof fails."""
        error = f"source_workspace_ownership_unavailable: {exc}"
        if isinstance(item_number, bool) or not isinstance(item_number, int):
            return JobResult(ok=False, error=error)
        worktree_path = base_dir / source_worktree_name(item_number, "impl")
        value: dict[str, object] = {
            "path": str(worktree_path),
            WORKTREE_MATERIALIZED_KEY: worktree_path.exists(),
        }
        recovery = getattr(exc, "recovery", None)
        if isinstance(exc, (SourceWorkspaceError, WorktreeCreationReceiptError)) and isinstance(
            recovery, (dict, SourceWorkspaceRecovery)
        ):
            value["failure_kind"] = "source_workspace_ownership"
            value["source_workspace_recovery"] = (
                recovery.to_dict() if isinstance(recovery, SourceWorkspaceRecovery) else recovery
            )
        if base_sha is not None:
            value["direct_scope_reservation"] = {
                "branch": branch_name,
                "base_sha": base_sha,
            }
        return JobResult(
            ok=False,
            error=error,
            value=value,
        )

    def _release_direct_scope_reservation(
        self,
        branch_name: str,
        base_sha: str | None,
        repo_root: Path,
        *,
        expected_repo: str,
        timeout_s: int,
    ) -> bool:
        """Conditionally release a direct reservation, or no-op for normal worktrees."""
        if base_sha is None:
            return True

        revalidate_remote = self._authenticated_remote_revalidator(
            cwd=repo_root, expected_repo=expected_repo, timeout=timeout_s
        )
        remote_env, remote_config = revalidate_remote()
        return git_utils.delete_reserved_branch_if_unchanged(
            branch_name,
            base_sha,
            repo_root,
            timeout=timeout_s,
            env=remote_env,
            remote_config=remote_config,
            revalidate_remote=revalidate_remote,
        )

    def _rollback_direct_scope_reservation(
        self,
        *,
        branch_name: str,
        base_sha: str,
        repo_root: Path,
        expected_repo: str,
        timeout_s: int,
        error: str,
    ) -> JobResult:
        """Release an early reservation or preserve its receipt for Finished."""
        try:
            released = self._release_direct_scope_reservation(
                branch_name,
                base_sha,
                repo_root,
                expected_repo=expected_repo,
                timeout_s=timeout_s,
            )
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            return JobResult(
                ok=False,
                value={"direct_scope_reservation": {"branch": branch_name, "base_sha": base_sha}},
                error=f"{error}; reservation rollback failed: {exc}",
            )
        if not released:
            return JobResult(
                ok=False,
                error=f"{error}; direct scope reservation changed before it could be released",
            )
        return JobResult(ok=False, error=error)

    def _finalize_created_worktree(  # noqa: C901
        self,
        *,
        created: Path | None,
        base_sha: str | None,
        branch_name: str,
        repo_root: Path,
        repo: str,
        source_repository: str | None = None,
        sync_to_remote: bool,
        pr_number: object,
        timeout_s: int,
        source_lane: object = None,
        item_number: object = None,
        writer_authority: ImplementationWriterAuthority | None = None,
        worktree_manager: WorktreeManager | None = None,
        implementation_adoption_head: object = None,
        source_manager: SourceWorkspaceManager | None = None,
        implementation_writer_handoff: ImplementationWriterHandoff | None = None,
    ) -> JobResult:
        """Validate a created worktree and attach a direct reservation receipt."""
        if created is None:
            if source_lane == "impl":
                return self._creation_receipt_failure(
                    base_dir=repo_root / "build" / ".worktrees",
                    item_number=item_number,
                    exc=SourceWorkspaceError("implementation writer was not materialized"),
                    branch_name=branch_name,
                    base_sha=base_sha,
                )
            if base_sha is not None:
                return self._rollback_direct_scope_reservation(
                    branch_name=branch_name,
                    base_sha=base_sha,
                    repo_root=repo_root,
                    expected_repo=repo,
                    timeout_s=timeout_s,
                    error="worktree manager returned no worktree",
                )
            # Non-direct callers retain the legacy no-op success contract.
            return JobResult(ok=True)
        worktree_path = Path(created)
        if repo_root not in worktree_path.parents and worktree_path != repo_root:
            error = (
                f"worktree {worktree_path} escaped resolved repo root {repo_root} "
                f"for job.repo={repo!r}"
            )
            if base_sha is not None:
                return self._rollback_direct_scope_reservation(
                    branch_name=branch_name,
                    base_sha=base_sha,
                    repo_root=repo_root,
                    expected_repo=repo,
                    timeout_s=timeout_s,
                    error=error,
                )
            return JobResult(
                ok=False,
                error=error,
            )
        if pr_number is not None and not isinstance(pr_number, (int, str)):
            return JobResult(ok=False, error="worktree sync received an invalid PR number")
        if not worktree_path.exists() and not sync_to_remote and base_sha is None:
            if source_lane == "impl":
                return self._creation_receipt_failure(
                    base_dir=worktree_path.parent,
                    item_number=item_number,
                    exc=SourceWorkspaceError("implementation writer was not materialized"),
                    branch_name=branch_name,
                    base_sha=base_sha,
                )
            # Keep compatibility with test and alternate managers that return
            # a planned path. A materialized reusable checkout always exists
            # and must pass through the dirty snapshot below.
            return JobResult(ok=True, value=str(worktree_path))

        try:
            if source_lane == "impl" and (
                isinstance(item_number, bool) or not isinstance(item_number, int)
            ):
                raise SourceWorkspaceError("implementation writer item number is invalid")
            implementation_item_number = cast(int, item_number)
            dirty = not git_utils.is_clean_working_tree(worktree_path, timeout=timeout_s)
            status = ""
            diff = ""
            if dirty:
                status_result = git_utils.run(
                    ["git", "status", "--short"],
                    cwd=worktree_path,
                    capture_output=True,
                    check=False,
                    timeout=timeout_s,
                )
                diff_result = git_utils.run(
                    ["git", "diff"],
                    cwd=worktree_path,
                    capture_output=True,
                    check=False,
                    timeout=timeout_s,
                )
                status = status_result.stdout or ""
                diff = diff_result.stdout or ""
                content_snapshot = _dirty_worktree_content_snapshot(
                    worktree_path,
                    timeout=timeout_s,
                )
            elif sync_to_remote and branch_name:
                self._sync_worktree_to_remote_branch(
                    worktree_path,
                    branch_name,
                    expected_repo=repo,
                    pr_number=int(pr_number) if isinstance(pr_number, (int, str)) else None,
                    timeout=timeout_s,
                )
                if source_lane == "impl" and implementation_adoption_head is not None:
                    if not isinstance(implementation_adoption_head, str) or not _is_full_commit_sha(
                        implementation_adoption_head
                    ):
                        raise SourceWorkspaceError("implementation writer adoption head is invalid")
                    if worktree_manager is None:
                        raise SourceWorkspaceError(
                            "implementation writer authority manager is missing"
                        )
                    if implementation_writer_handoff is None:
                        writer_authority = (
                            worktree_manager.mint_adopted_implementation_writer_authority(
                                issue_number=implementation_item_number,
                                branch_name=branch_name,
                                worktree_path=worktree_path,
                                expected_head=implementation_adoption_head,
                                timeout=timeout_s,
                            )
                        )
                    else:
                        writer_authority = (
                            worktree_manager.mint_adopted_implementation_writer_authority(
                                issue_number=implementation_item_number,
                                branch_name=branch_name,
                                worktree_path=worktree_path,
                                expected_head=implementation_adoption_head,
                                timeout=timeout_s,
                                implementation_writer_handoff=implementation_writer_handoff,
                            )
                        )
            if source_lane == "impl" and not dirty:
                if writer_authority is None:
                    raise SourceWorkspaceError("implementation writer authority is missing")
                if source_manager is None or implementation_writer_handoff is None:
                    raise SourceWorkspaceError("implementation writer handoff is missing")
                binding = source_manager.claim_implementation_writer(
                    implementation_item_number,
                    branch=branch_name,
                    path=worktree_path,
                    authority=writer_authority,
                    handoff=implementation_writer_handoff,
                )
        except Exception as exc:
            if isinstance(exc, (SourceWorkspaceError, WorktreeCreationReceiptError)):
                return self._creation_receipt_failure(
                    base_dir=worktree_path.parent,
                    item_number=item_number,
                    exc=exc,
                    branch_name=branch_name,
                    base_sha=base_sha,
                )
            return JobResult(
                ok=False,
                error=f"worktree post-create preparation failed: {exc}",
                value={"path": str(worktree_path), WORKTREE_MATERIALIZED_KEY: True},
            )
        if not dirty and not sync_to_remote and base_sha is None:
            if source_lane == "impl":
                return JobResult(
                    ok=True,
                    value={
                        "path": str(worktree_path),
                        "impl_source_revision": binding.revision,
                    },
                )
            return JobResult(ok=True, value=str(worktree_path))
        value: dict[str, object] = {"path": str(worktree_path)}
        if source_lane == "impl" and not dirty:
            value["impl_source_revision"] = binding.revision
        if dirty or sync_to_remote:
            value.update(dirty=dirty, status=status, diff=diff)
        if dirty:
            observed_branch = git_utils.run(
                ["git", "branch", "--show-current"],
                cwd=worktree_path,
                timeout=timeout_s,
            ).stdout.strip()
            observed_head = git_utils.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree_path,
                timeout=timeout_s,
            ).stdout.strip()
            if observed_branch != branch_name or not _is_full_commit_sha(observed_head):
                return JobResult(
                    ok=False,
                    error="dirty worktree identity does not match its requested branch",
                    value={"path": str(worktree_path), WORKTREE_MATERIALIZED_KEY: True},
                )
            value.update(
                branch=observed_branch,
                head_sha=observed_head,
                content_snapshot=content_snapshot,
            )
        if base_sha is not None:
            value["direct_scope_reservation"] = {
                "branch": branch_name,
                "base_sha": base_sha,
            }
        return JobResult(ok=True, value=value)

    def _prepare_direct_scope_worktree(
        self,
        *,
        kwargs: dict[str, object],
        sync_to_remote: bool,
        repo_root: Path,
        expected_repo: str,
        timeout_s: int,
    ) -> tuple[str | None, str] | JobResult:
        """Validate and atomically reserve a direct-scope implementation branch."""
        base_sha = kwargs.pop("base_sha", None)
        branch_name = str(kwargs.get("branch_name") or "")
        if base_sha is None:
            return None, branch_name
        if sync_to_remote or bool(kwargs.get("refresh_base", False)):
            return JobResult(ok=False, error="direct scope base pin invalid")
        if not _is_full_commit_sha(base_sha):
            return JobResult(ok=False, error="direct scope base pin invalid")
        checkout_head = git_utils.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, timeout=timeout_s
        ).stdout.strip()
        if checkout_head != base_sha:
            return JobResult(ok=False, error="direct scope checkout pin mismatch")
        if not branch_name:
            return JobResult(ok=False, error="direct scope branch name is missing")
        remote_env, remote_config = self._authenticated_remote_git_configuration(
            cwd=repo_root,
            expected_repo=expected_repo,
            timeout=timeout_s,
        )
        git_utils.reserve_remote_branch_if_absent(
            branch_name,
            base_sha,
            repo_root,
            timeout=timeout_s,
            env=remote_env,
            remote_config=remote_config,
        )
        return base_sha, branch_name

    def _git_verify_pr_review_checkout(self, job: GitJob) -> JobResult:
        """Synchronize a clean review checkout and bind it to one PR head.

        The review snapshot comes from GitHub before this job.  A remote move
        while synchronizing is not an error to paper over: the stage refreshes
        a new snapshot and retries a bounded number of times without sending an
        agent job for the stale input.
        """
        worktree = Path(str(job.kwargs.get("worktree_path") or ""))
        branch = str(job.kwargs.get("branch") or "")
        expected_head = str(job.kwargs.get("expected_head_sha") or "")
        expected_base = str(job.kwargs.get("expected_base_sha") or "")
        base_branch = str(job.kwargs.get("base_branch") or "main")
        pr_number = job.kwargs.get("pr_number")
        if (
            not worktree.is_dir()
            or not branch
            or not _is_full_commit_sha(expected_head)
            or not _is_full_commit_sha(expected_base)
            or not base_branch
        ):
            return JobResult(
                ok=False,
                error="review checkout requires worktree, branch, exact base/head, and base branch",
            )
        if not git_utils.is_clean_working_tree(worktree, timeout=job.timeout_s):
            return JobResult(ok=True, value={"ready": False, "reason": "dirty"})
        self._sync_worktree_to_remote_branch(
            worktree,
            branch,
            expected_repo=job.transport_repository,
            pr_number=int(pr_number) if pr_number is not None else None,
            timeout=job.timeout_s,
        )
        head = git_utils.run(
            ["git", "rev-parse", "HEAD"], cwd=worktree, timeout=job.timeout_s
        ).stdout.strip()
        if head != expected_head:
            return JobResult(ok=True, value={"ready": False, "reason": "head_drift"})
        if not git_utils.is_clean_working_tree(worktree, timeout=job.timeout_s):
            return JobResult(ok=True, value={"ready": False, "reason": "dirty"})
        # Build the prompt diff from the checkout only after it is proven to
        # be the head captured above.  ``gh pr diff`` is mutable and cannot
        # distinguish an A -> B -> A head race from a stable A snapshot.
        remote_env, remote_config = self._authenticated_remote_git_configuration(
            cwd=worktree,
            expected_repo=job.transport_repository,
            timeout=job.timeout_s,
        )
        git_utils.run(
            ["git", *remote_config, "fetch", "origin", "--", base_branch],
            cwd=worktree,
            timeout=job.timeout_s,
            env=remote_env,
        )
        # The reviewer is bound to the branch point of the captured PR pair,
        # not to the base branch's current HEAD. Fetching only makes the
        # captured base object available; advancement of the branch is an
        # implementation concern after review.
        base = git_utils.run(
            ["git", "merge-base", expected_base, head],
            cwd=worktree,
            timeout=job.timeout_s,
        ).stdout.strip()
        if not _is_full_commit_sha(base):
            return JobResult(ok=False, error="review checkout branch point unavailable")
        diff = git_utils.run(
            ["git", "diff", "--no-ext-diff", "--binary", f"{base}...{head}"],
            cwd=worktree,
            timeout=job.timeout_s,
        ).stdout
        if not isinstance(diff, str):
            return JobResult(ok=False, error="review checkout diff unavailable")
        # Disable rename detection so a rename is represented by both its
        # deleted source and added destination.  The NUL-delimited manifest
        # preserves paths containing whitespace or newlines without parsing
        # the human-oriented ``diff --git`` header.
        changed_paths_output = git_utils.run(
            [
                "git",
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                f"{base}...{head}",
            ],
            cwd=worktree,
            timeout=job.timeout_s,
        ).stdout
        if not isinstance(changed_paths_output, str):
            return JobResult(ok=False, error="review checkout path manifest unavailable")
        changed_paths = [path for path in changed_paths_output.split("\0") if path]
        return JobResult(
            ok=True,
            value={
                "ready": True,
                "head": head,
                "base": base,
                "diff": diff,
                "changed_paths": changed_paths,
            },
        )

    def _git_remove_worktree(self, job: GitJob) -> JobResult:
        """Remove a worktree by known path, or fall back to manager state."""
        from .git_cleanup import run_cleanup_job

        return run_cleanup_job(job, worktree_manager_type=WorktreeManager)

    def _git_inspect_implementation_worktree(self, job: GitJob) -> JobResult:  # noqa: C901
        """Inspect an identity-bound writer without changing its contents."""
        raw_worktree = job.kwargs.get("worktree_path")
        raw_repo_root = job.kwargs.get("repo_root")
        branch = str(job.kwargs.get("branch") or "")
        expected_head = str(job.kwargs.get("expected_head") or "")

        def fail(kind: str, cause: str) -> JobResult:
            return JobResult(
                ok=False,
                error=f"implementation worktree inspection {kind}",
                value={
                    "outcome": "failed",
                    "failure_kind": kind,
                    "cause": bounded_git_diagnostic(cause, limit=_ERR_MAX),
                },
            )

        if (
            not branch
            or not _is_full_commit_sha(expected_head)
            or not isinstance(raw_worktree, str)
            or not raw_worktree
            or not Path(raw_worktree).is_absolute()
            or not isinstance(raw_repo_root, str)
            or not raw_repo_root
            or not Path(raw_repo_root).is_absolute()
        ):
            return fail(
                "invalid_request",
                "inspection requires absolute repository and writer paths, branch, and exact head",
            )
        worktree = Path(raw_worktree)
        repo_root = Path(raw_repo_root)
        try:
            if not _secure_dir_fd_supported():
                _portable_path_identity(repo_root, directory=True)
                _portable_path_identity(worktree, directory=True)
            confined_root = repo_root.resolve(strict=True)
            confined_worktree = worktree.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            return fail("worktree_unavailable", str(exc))
        if (
            repo_root.is_symlink()
            or worktree.is_symlink()
            or not confined_root.is_dir()
            or not (confined_root / ".git").exists()
            or not confined_worktree.is_dir()
            or not (confined_worktree / ".git").exists()
            or confined_worktree == confined_root
            or confined_root not in confined_worktree.parents
        ):
            return fail("worktree_unconfined", "worktree is outside the repository root")
        try:
            for checkout in (confined_root, confined_worktree):
                if preflight_error := _checkout_preflight_error(
                    checkout,
                    job.timeout_s,
                    max_config_bytes=IMPLEMENTATION_INSPECTION_METADATA_MAX_BYTES,
                ):
                    return fail("unsafe_git_configuration", preflight_error)
            linked_env = _linked_worktree_git_env(confined_root, confined_worktree)
            binding = getattr(linked_env, "binding", None)
            if not isinstance(binding, _LinkedWorktreeBinding):
                raise RuntimeError("linked worktree binding is unavailable")
            if binding.branch_ref != f"refs/heads/{branch}" or binding.branch_sha != expected_head:
                return fail("worktree_identity_drift", "worktree branch or head changed")
            after, status_result, candidate_tree, diff_result, selected_paths = (
                _inspect_candidate_with_private_git(
                    confined_worktree,
                    expected_head,
                    timeout=job.timeout_s,
                    linked_env=linked_env,
                )
            )
            rebound = _linked_worktree_git_env(confined_root, confined_worktree)
            rebound_binding = getattr(rebound, "binding", None)
            if (
                not _linked_binding_matches(linked_env, rebound, include_index=True)
                or not isinstance(rebound_binding, _LinkedWorktreeBinding)
                or rebound_binding.branch_sha != expected_head
            ):
                raise RuntimeError("linked worktree metadata changed during inspection")
            status = status_result.text
            diff = diff_result.text
            receipt: dict[str, object] = {
                "outcome": "dirty" if status.strip() else "clean",
                "branch": branch,
                "head_sha": expected_head,
                "status": status,
                "diff": diff,
                "status_sha256": status_result.sha256,
                "diff_sha256": diff_result.sha256,
                "worktree_path": str(confined_worktree),
            }
            if status.strip():
                if selected_paths is None:
                    raise RuntimeError("candidate path manifest is unavailable")
                receipt["candidate_tree_sha"] = candidate_tree
                receipt["content_snapshot"] = after.snapshot
                receipt["changed_file_count"] = after.changed_file_count
                receipt["candidate_add_paths"] = list(selected_paths.add_paths)
                receipt["candidate_update_paths"] = list(selected_paths.update_paths)
            return JobResult(ok=True, value=receipt)
        except _GitInspectionResourceLimitError as exc:
            return fail("resource_limit_exceeded", str(exc))
        except subprocess.TimeoutExpired as exc:
            return fail("timeout", str(exc))
        except subprocess.CalledProcessError as exc:
            return fail("git_error", str(exc))
        except (OSError, RuntimeError) as exc:
            return fail("inspection_unavailable", str(exc))

    def _git_recover_dirty_worktree(self, job: GitJob) -> JobResult:  # noqa: C901
        """Preserve one identity-bound dirty writer by commit or stash."""
        worktree = Path(str(job.kwargs.get("worktree_path") or ""))
        repo_root = Path(str(job.kwargs.get("repo_root") or ""))
        branch = str(job.kwargs.get("branch") or "")
        action = str(job.kwargs.get("action") or "")
        pre_action_head = str(job.kwargs.get("pre_action_head") or "")
        expected_remote_head = str(job.kwargs.get("expected_remote_head") or "")
        expected_status = str(job.kwargs.get("status") or "")
        expected_diff = str(job.kwargs.get("diff") or "")
        expected_content_snapshot = job.kwargs.get("content_snapshot")
        issue_number = job.kwargs.get("issue_number")
        receipt: dict[str, object] = {
            "outcome": "failed",
            "failure_kind": "invalid_request",
            "action": action,
            "branch": branch,
            "worktree_path": str(worktree),
            "pre_action_head": pre_action_head,
            "current_head": pre_action_head,
            "expected_remote_head": expected_remote_head,
            "remote_head": None,
            "published": False,
            "stash_object": None,
            "action_applied": False,
            "final_clean": False,
        }

        def fail(kind: str, cause: str) -> JobResult:
            receipt["failure_kind"] = kind
            receipt["cause"] = bounded_git_diagnostic(cause, limit=_ERR_MAX)
            return JobResult(ok=False, value=dict(receipt), error=f"dirty recovery {kind}")

        if (
            action not in {"COMMIT", "STASH"}
            or not branch
            or not _is_full_commit_sha(pre_action_head)
            or not _is_full_commit_sha(expected_remote_head)
            or not _valid_dirty_content_snapshot(expected_content_snapshot)
            or isinstance(issue_number, bool)
            or not isinstance(issue_number, int)
        ):
            return fail(
                "invalid_request",
                "recovery requires action, branch, issue, and exact heads",
            )
        try:
            confined_root = repo_root.resolve(strict=True)
            confined_worktree = worktree.resolve(strict=True)
        except OSError as exc:
            return fail("worktree_unavailable", str(exc))
        if (
            worktree.is_symlink()
            or not confined_worktree.is_dir()
            or not (confined_worktree / ".git").exists()
            or (
                confined_worktree != confined_root
                and confined_root not in confined_worktree.parents
            )
        ):
            return fail("worktree_unconfined", "worktree is outside the repository root")
        worktree = confined_worktree
        receipt["worktree_path"] = str(worktree)
        try:
            listing = git_utils.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=repo_root,
                timeout=job.timeout_s,
                env=_controlled_git_env(),
            ).stdout
            expected_block = (
                f"worktree {worktree}\nHEAD {pre_action_head}\nbranch refs/heads/{branch}\n"
            )
            if expected_block not in f"{listing.rstrip()}\n":
                return fail("worktree_identity_drift", "registered worktree identity changed")
            current_head = git_utils.run(
                ["git", "rev-parse", "HEAD"], cwd=worktree, timeout=job.timeout_s
            ).stdout.strip()
            current_branch = git_utils.run(
                ["git", "branch", "--show-current"], cwd=worktree, timeout=job.timeout_s
            ).stdout.strip()
            receipt["current_head"] = current_head
            if current_head != pre_action_head or current_branch != branch:
                return fail("worktree_identity_drift", "worktree branch or head changed")
            status = git_utils.run(
                ["git", "status", "--short"], cwd=worktree, timeout=job.timeout_s
            ).stdout
            diff = git_utils.run(["git", "diff"], cwd=worktree, timeout=job.timeout_s).stdout
            if not status.strip():
                return fail("worktree_clean", "captured dirty worktree is now clean")
            if status != expected_status or diff != expected_diff:
                return fail("worktree_content_drift", "dirty snapshot changed before recovery")
            content_snapshot = _dirty_worktree_content_snapshot(
                worktree,
                timeout=job.timeout_s,
            )
            if content_snapshot != expected_content_snapshot:
                return fail("worktree_content_drift", "dirty content changed before recovery")
            remote_head = self._read_remote_branch_head(
                worktree,
                remote="origin",
                branch=branch,
                expected_repo=job.transport_repository,
                timeout=job.timeout_s,
            )
            if isinstance(remote_head, JobResult):
                return fail("remote_probe_failed", remote_head.error or "remote probe failed")
            receipt["remote_head"] = remote_head
            if remote_head != expected_remote_head or remote_head != pre_action_head:
                return fail("remote_head_drift", "remote head does not match captured local head")

            if action == "COMMIT":
                if (
                    _dirty_worktree_content_snapshot(worktree, timeout=job.timeout_s)
                    != expected_content_snapshot
                ):
                    return fail(
                        "worktree_content_drift",
                        "dirty content changed at the commit boundary",
                    )
                changed = self._commit_if_changes_with_controlled_signing(
                    job,
                    (issue_number, worktree, str(job.kwargs.get("agent") or "claude")),
                    None,
                    job.kwargs.get("agent_model"),
                    int(job.kwargs.get("git_message_timeout", 1200)),
                )
                if isinstance(changed, JobResult):
                    return fail("commit_failed", changed.error or "commit failed")
                if not changed:
                    return fail("commit_failed", "dirty changes did not produce a commit")
                receipt["action_applied"] = True
                publish_head = self._read_publish_head(worktree, timeout=job.timeout_s)
                if isinstance(publish_head, JobResult):
                    return fail(
                        "commit_postflight_failed",
                        publish_head.error or "head unavailable",
                    )
                current_head = publish_head
                receipt["current_head"] = current_head
                parent = git_utils.run(
                    ["git", "rev-parse", "HEAD^"], cwd=worktree, timeout=job.timeout_s
                ).stdout.strip()
                raw_commit = git_utils.run(
                    ["git", "cat-file", "-p", current_head],
                    cwd=worktree,
                    timeout=job.timeout_s,
                ).stdout
                if (
                    parent != pre_action_head
                    or "\ngpgsig " not in f"\n{raw_commit}"
                    or "Signed-off-by:" not in raw_commit
                ):
                    return fail(
                        "commit_metadata_invalid",
                        "commit lacks exact parent, signature, or DCO",
                    )
                revalidate_remote = self._authenticated_remote_revalidator(
                    cwd=worktree,
                    expected_repo=job.transport_repository,
                    timeout=job.timeout_s,
                )
                remote_env, remote_config = revalidate_remote()
                git_utils.push_head_to_branch(
                    branch,
                    expected_remote_head,
                    worktree,
                    source_sha=current_head,
                    timeout=job.timeout_s,
                    env=remote_env,
                    remote_config=remote_config,
                    revalidate_remote=revalidate_remote,
                )
                receipt["published"] = True
            else:
                before_stash = git_utils.run(
                    ["git", "rev-parse", "--verify", "-q", "refs/stash"],
                    cwd=worktree,
                    check=False,
                    log_errors=False,
                    timeout=job.timeout_s,
                ).stdout.strip()
                if (
                    _dirty_worktree_content_snapshot(worktree, timeout=job.timeout_s)
                    != expected_content_snapshot
                ):
                    return fail(
                        "worktree_content_drift",
                        "dirty content changed at the stash boundary",
                    )
                message = f"hephaestus dirty recovery issue #{issue_number}"[:120]
                git_utils.run(
                    ["git", "stash", "push", "--include-untracked", "-m", message],
                    cwd=worktree,
                    timeout=job.timeout_s,
                    env=_controlled_git_env(),
                )
                receipt["action_applied"] = True
                stash_object = git_utils.run(
                    ["git", "rev-parse", "--verify", "refs/stash"],
                    cwd=worktree,
                    timeout=job.timeout_s,
                ).stdout.strip()
                if not _is_full_commit_sha(stash_object) or stash_object == before_stash:
                    return fail("stash_evidence_invalid", "stash reference did not advance")
                receipt["stash_object"] = stash_object

            current_head = git_utils.run(
                ["git", "rev-parse", "HEAD"], cwd=worktree, timeout=job.timeout_s
            ).stdout.strip()
            receipt["current_head"] = current_head
            final_clean = git_utils.is_clean_working_tree(worktree, timeout=job.timeout_s)
            receipt["final_clean"] = final_clean
            if not final_clean:
                return fail("postflight_dirty", "recovery left worktree dirty")
            final_remote = self._read_remote_branch_head(
                worktree,
                remote="origin",
                branch=branch,
                expected_repo=job.transport_repository,
                timeout=job.timeout_s,
            )
            if isinstance(final_remote, JobResult):
                return fail("remote_postflight_failed", final_remote.error or "remote probe failed")
            receipt["remote_head"] = final_remote
            expected_final = current_head if action == "COMMIT" else expected_remote_head
            if final_remote != expected_final:
                return fail("remote_postflight_drift", "remote head does not match recovery result")
            if action == "STASH" and current_head != pre_action_head:
                return fail("stash_head_drift", "stash changed the local branch head")
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return fail("operation_failed", str(exc))
        receipt["outcome"] = "recovered"
        receipt["failure_kind"] = None
        receipt["cause"] = ""
        return JobResult(ok=True, value=receipt)

    def _git_commit_push(self, job: GitJob) -> JobResult:
        """Commit and publish while private recovery metadata remains live."""
        with ExitStack() as recovery_stack:
            return self._git_commit_push_inner(job, recovery_stack)

    def _git_commit_push_inner(  # noqa: C901
        self,
        job: GitJob,
        recovery_stack: ExitStack,
    ) -> JobResult:
        """Commit pending changes in a worktree, then push its branch.

        Only the keys ``commit_if_changes`` actually accepts are forwarded —
        passing ``job.kwargs`` wholesale would crash on routing-only keys such
        as ``branch``. A missing ``worktree_path`` (or ``issue_number``) is a
        hard error result, never a silent skip: the coordinator submitted this
        op expecting a push to happen.
        """
        worktree_path = job.kwargs.get("worktree_path")
        issue_number = job.kwargs.get("issue_number")
        if not worktree_path or issue_number is None:
            return JobResult(
                ok=False,
                error="commit_push requires non-empty 'worktree_path' and 'issue_number' kwargs",
            )
        if "publish_detached_head" in job.kwargs:
            return JobResult(
                ok=False,
                error="detached reviewer commit publication is unsupported",
            )
        branch = str(job.kwargs.get("branch") or "")
        expected_recovery_head = job.kwargs.get("expected_recovery_head")
        expected_recovery_snapshot = job.kwargs.get("expected_recovery_content_snapshot")
        expected_recovery_tree = job.kwargs.get("expected_recovery_tree_sha")
        expected_recovery_diff = job.kwargs.get("expected_recovery_diff")
        expected_recovery_diff_sha256 = job.kwargs.get("expected_recovery_diff_sha256")
        expected_recovery_add_paths = job.kwargs.get("expected_recovery_add_paths")
        expected_recovery_update_paths = job.kwargs.get("expected_recovery_update_paths")
        expected_recovery_commit = job.kwargs.get("expected_recovery_commit_sha")
        prepare_only = job.op == "prepare_remediation_recovery"
        recovery_bound = (
            expected_recovery_head is not None
            or expected_recovery_snapshot is not None
            or expected_recovery_tree is not None
            or expected_recovery_diff is not None
            or expected_recovery_diff_sha256 is not None
            or expected_recovery_add_paths is not None
            or expected_recovery_update_paths is not None
            or expected_recovery_commit is not None
        )
        if recovery_bound and not branch:
            return JobResult(ok=False, error="commit publication branch is unavailable")
        worktree = Path(worktree_path)
        allowed_paths = cast(Collection[str] | None, job.kwargs.get("allowed_paths"))
        scope_check = self._verify_implementation_edit_scope(
            job,
            worktree,
            allowed_paths=allowed_paths,
        )
        if scope_check is not None:
            return scope_check
        if allowed_paths is not None and job.kwargs.get("scope_retraction_paths"):
            allowed_paths = tuple(
                sorted(set(allowed_paths).union(job.kwargs["scope_retraction_paths"]))
            )
        retry_recovery_commit = False
        selected_recovery_commit: str | None = None
        recovery_paths: CommitPaths | None = None
        recovery_git_env: dict[str, str] | None = None
        recovery_commit_git_env: dict[str, str] | None = None
        recovery_repo_root: Path | None = None
        remediation_parent_sha: str | None = None
        remediation_tree_sha: str | None = None
        remediation_diff: _BoundedGitOutput | None = None
        if recovery_bound:
            raw_repo_root = job.kwargs.get("repo_root")
            if (
                not isinstance(raw_repo_root, str)
                or not Path(raw_repo_root).is_absolute()
                or not worktree.is_absolute()
                or Path(raw_repo_root).is_symlink()
                or worktree.is_symlink()
            ):
                return JobResult(ok=False, error="remediation writer binding invalid")
            if not isinstance(expected_recovery_add_paths, tuple) or not isinstance(
                expected_recovery_update_paths, tuple
            ):
                return JobResult(ok=False, error="remediation writer binding invalid")
            recovery_paths = CommitPaths(
                expected_recovery_add_paths,
                expected_recovery_update_paths,
            )
            if (
                not _is_full_commit_sha(expected_recovery_head)
                or not (_valid_dirty_content_snapshot(expected_recovery_snapshot))
                or not _is_full_commit_sha(expected_recovery_tree)
                or not isinstance(expected_recovery_diff, str)
                or len(expected_recovery_diff.encode("utf-8", "surrogateescape"))
                > IMPLEMENTATION_INSPECTION_DIFF_MAX_BYTES
                or not isinstance(expected_recovery_diff_sha256, str)
                or expected_recovery_diff_sha256
                != hashlib.sha256(
                    expected_recovery_diff.encode("utf-8", "surrogateescape")
                ).hexdigest()
                or not is_bounded_commit_paths(
                    recovery_paths,
                    max_paths=DIRTY_SNAPSHOT_CHANGED_FILE_MAX,
                    max_bytes=IMPLEMENTATION_INSPECTION_STATUS_MAX_BYTES,
                )
                or (
                    expected_recovery_commit is not None
                    and not _is_full_commit_sha(expected_recovery_commit)
                )
            ):
                return JobResult(ok=False, error="remediation writer binding invalid")
            try:
                if not _secure_dir_fd_supported():
                    _portable_path_identity(Path(raw_repo_root), directory=True)
                    _portable_path_identity(worktree, directory=True)
                recovery_repo_root = Path(raw_repo_root).resolve(strict=True)
                linked_env = _linked_worktree_git_env(recovery_repo_root, worktree)
                linked_binding = getattr(linked_env, "binding", None)
                if isinstance(linked_binding, _LinkedWorktreeBinding):
                    expected_branch_ref = f"refs/heads/{branch}"
                    allowed_branch_heads = {expected_recovery_head}
                    if expected_recovery_commit is not None:
                        allowed_branch_heads.add(expected_recovery_commit)
                    if (
                        linked_binding.branch_ref != expected_branch_ref
                        or linked_binding.branch_sha not in allowed_branch_heads
                    ):
                        raise RuntimeError("remediation writer branch binding changed")
                private_head = expected_recovery_commit or expected_recovery_head
                durable_git_dir: Path | None = None
                if prepare_only:
                    private_pr_number = job.kwargs.get("remediation_pr_number")
                    if isinstance(private_pr_number, bool) or not isinstance(
                        private_pr_number, int
                    ):
                        raise RuntimeError("remediation PR identity is invalid")
                    durable_git_dir = prepublication_private_git_dir(
                        repo_root=recovery_repo_root,
                        pr_number=private_pr_number,
                        create=True,
                    )
                recovery_git_env = recovery_stack.enter_context(
                    _private_linked_worktree_git_env(
                        linked_env,
                        detached_head=private_head,
                        durable_git_dir=durable_git_dir,
                    )
                )
            except (OSError, RuntimeError):
                return JobResult(ok=False, error="remediation writer Git metadata binding invalid")
            current_head = self._read_publish_head(
                worktree,
                timeout=job.timeout_s,
                git_env=recovery_git_env,
            )
            if isinstance(current_head, JobResult):
                return current_head
            if expected_recovery_commit is not None:
                retry_recovery_commit = current_head == expected_recovery_commit and (
                    self._is_exact_recovery_commit(
                        worktree,
                        current_head,
                        parent=expected_recovery_head,
                        tree=expected_recovery_tree,
                        timeout=job.timeout_s,
                        git_env=recovery_git_env,
                    )
                )
                if not retry_recovery_commit:
                    return JobResult(
                        ok=False,
                        error="remediation writer retry commit is unavailable",
                    )
                selected_recovery_commit = current_head
            elif current_head != expected_recovery_head:
                return JobResult(
                    ok=False,
                    error="remediation writer head drift before commit",
                )
            else:
                try:
                    snapshot_parent = Path(
                        recovery_stack.enter_context(
                            tempfile.TemporaryDirectory(
                                prefix="hephaestus-recovery-snapshot-",
                            )
                        )
                    )
                    snapshot_root = snapshot_parent / "worktree"
                    private_index = snapshot_parent / "index"
                    current_snapshot = _dirty_worktree_content_snapshot(
                        worktree,
                        timeout=job.timeout_s,
                        git_env=recovery_git_env,
                    )
                    current_tree, current_diff = _candidate_commit_tree_evidence(
                        worktree,
                        current_head,
                        timeout=job.timeout_s,
                        selected=recovery_paths,
                        git_env=recovery_git_env,
                        snapshot_root=snapshot_root,
                    )
                    recovery_commit_git_env = _PrivateLinkedWorktreeGitEnvironment(
                        {
                            **recovery_git_env,
                            "GIT_INDEX_FILE": str(private_index),
                            "GIT_WORK_TREE": str(snapshot_root),
                        },
                        cast(
                            _PrivateLinkedWorktreeGitEnvironment,
                            recovery_git_env,
                        ).linked_env,
                    )
                except _GitInspectionResourceLimitError:
                    return JobResult(
                        ok=False,
                        value={"failure_kind": "resource_limit_exceeded"},
                        error="remediation writer resource limit exceeded before commit",
                    )
                except (
                    OSError,
                    RuntimeError,
                    subprocess.SubprocessError,
                ):
                    return JobResult(
                        ok=False,
                        error="remediation writer content binding unavailable before commit",
                    )
                same_content = all(
                    current_snapshot[key] == expected_recovery_snapshot[key]
                    for key in ("worktree_sha256", "untracked_sha256")
                )
                if not same_content or current_tree != expected_recovery_tree:
                    return JobResult(
                        ok=False,
                        error="remediation writer content drift before commit",
                    )
                if (
                    current_diff.text != expected_recovery_diff
                    or current_diff.sha256 != expected_recovery_diff_sha256
                ):
                    return JobResult(
                        ok=False,
                        error="remediation writer diff drift before commit",
                    )
            remediation_parent_sha = expected_recovery_head
            remediation_tree_sha = expected_recovery_tree
            remediation_diff = _BoundedGitOutput(
                text=expected_recovery_diff,
                sha256=expected_recovery_diff_sha256,
                byte_count=len(expected_recovery_diff.encode("utf-8", "surrogateescape")),
            )
        elif job.kwargs.get("remediation_repository") is not None:
            try:
                remediation_parent = self._read_publish_head(worktree, timeout=job.timeout_s)
                if isinstance(remediation_parent, JobResult):
                    return remediation_parent
                remediation_paths = _bounded_candidate_commit_paths(
                    worktree,
                    remediation_parent,
                    timeout=job.timeout_s,
                )
                remediation_tree_sha, remediation_diff = _candidate_commit_tree_evidence(
                    worktree,
                    remediation_parent,
                    timeout=job.timeout_s,
                    selected=remediation_paths,
                )
                recovery_paths = remediation_paths
                remediation_parent_sha = remediation_parent
            except RuntimeError:
                if not git_utils.is_clean_working_tree(worktree, timeout=job.timeout_s):
                    return JobResult(
                        ok=False, error="remediation candidate evidence is unavailable"
                    )
        # ``commit_if_changes`` returns False for a clean worktree.  An agent
        # is instructed to leave its edits uncommitted, but a defensive
        # recovery still recognizes a clean branch that is ahead of its
        # remote tracking ref: the coordinator, not the agent, publishes that
        # already-created commit so every subsequent review binds to the new
        # remote head.
        commit_args = (
            int(issue_number),
            worktree,
            str(job.kwargs.get("agent", "claude")),
        )
        agent_model = job.kwargs.get("agent_model")
        git_message_timeout = int(job.kwargs.get("git_message_timeout", 1200))
        changed: bool | str | JobResult = False
        if prepare_only and not retry_recovery_commit:
            try:
                intent_repository = job.kwargs.get("remediation_repository")
                intent_pr_number = job.kwargs.get("remediation_pr_number")
                intent_threads = job.kwargs.get("remediation_thread_snapshots")
                intent_batch = job.kwargs.get("remediation_batch_nonce")
                intent_diagnostic = job.kwargs.get("remediation_failure_diagnostic")
                if (
                    not isinstance(intent_repository, str)
                    or isinstance(intent_pr_number, bool)
                    or not isinstance(intent_pr_number, int)
                    or not isinstance(intent_threads, list)
                    or not isinstance(intent_batch, str)
                    or not isinstance(intent_diagnostic, str)
                ):
                    raise ValueError("remediation prepublication intent is incomplete")
                save_prepublication_intent(
                    repo_root=cast(Path, recovery_repo_root),
                    repository=intent_repository,
                    issue_number=cast(int, issue_number),
                    pr_number=intent_pr_number,
                    worktree_path=worktree,
                    branch=branch,
                    expected_remote_sha=cast(str, expected_recovery_head),
                    candidate_tree_sha=cast(str, expected_recovery_tree),
                    add_paths=cast(CommitPaths, recovery_paths).add_paths,
                    update_paths=cast(CommitPaths, recovery_paths).update_paths,
                    committed_diff_sha256=cast(str, expected_recovery_diff_sha256),
                    committed_diff=cast(str, expected_recovery_diff),
                    failure_diagnostic=intent_diagnostic,
                    thread_snapshot_json=RemediationReviewInput.canonical_thread_snapshot(
                        intent_threads
                    ),
                    content_snapshot=tuple(
                        sorted(cast(dict[str, str], expected_recovery_snapshot).items())
                    ),
                    batch_nonce=intent_batch,
                )
            except (OSError, TypeError, UnicodeError, ValueError) as exc:
                return JobResult(
                    ok=False,
                    error=f"remediation prepublication intent is unavailable: {exc}",
                )
        if not retry_recovery_commit:
            try:
                changed = self._commit_if_changes_with_controlled_signing(
                    job,
                    commit_args,
                    allowed_paths,
                    agent_model,
                    git_message_timeout,
                    recovery_paths=recovery_paths,
                    expected_tree_sha=remediation_tree_sha,
                    git_env=recovery_commit_git_env or recovery_git_env,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                if recovery_bound:
                    return self._classify_recovery_commit_failure(
                        worktree,
                        expected_head=cast(str, expected_recovery_head),
                        expected_tree=cast(str, expected_recovery_tree),
                        timeout=job.timeout_s,
                        cause=exc,
                        git_env=recovery_git_env,
                    )
                raise
            if isinstance(changed, JobResult):
                return changed
            if isinstance(changed, str):
                if not _is_full_commit_sha(changed):
                    return JobResult(ok=False, error="remediation commit receipt is invalid")
                selected_recovery_commit = changed
        if not changed and recovery_bound and not retry_recovery_commit:
            # The inspected writer was dirty immediately before this call.
            # ``commit_if_changes`` also returns False when its commit helper
            # catches RuntimeError, so False cannot prove a safe no-op here.
            return self._classify_recovery_commit_failure(
                worktree,
                expected_head=cast(str, expected_recovery_head),
                expected_tree=cast(str, expected_recovery_tree),
                timeout=job.timeout_s,
                git_env=recovery_git_env,
            )
        if not changed and not retry_recovery_commit:
            publish_state = self._commit_push_requires_publish(
                job=job,
                branch=branch,
                worktree_path=worktree,
            )
            if isinstance(publish_state, JobResult):
                return publish_state
            if not publish_state:
                clean_head = self._read_publish_head(worktree, timeout=job.timeout_s)
                if isinstance(clean_head, JobResult):
                    return clean_head
                return JobResult(
                    ok=True,
                    value={"pushed": False, "head_sha": clean_head},
                )
            status = git_utils.run(
                ["git", "status", "--porcelain"],
                cwd=worktree,
                capture_output=True,
                timeout=job.timeout_s,
            )
            if status.stdout.strip():
                return JobResult(ok=False, error="commit_push left uncommitted changes")
        scope_check = self._verify_implementation_edit_scope(
            job,
            worktree,
            allowed_paths=allowed_paths,
        )
        if scope_check is not None:
            return scope_check
        if not recovery_bound:
            scope_retraction = self._verify_scope_retraction(job, worktree)
            if scope_retraction is not None:
                return scope_retraction
            remediation_artifacts: tuple[dict[str, Any], tuple[str, str]] | None = None
            if (
                selected_recovery_commit is not None
                and remediation_parent_sha is not None
                and remediation_tree_sha is not None
                and remediation_diff is not None
                and recovery_paths is not None
            ):
                try:
                    raw_repo_root = job.kwargs.get("repo_root")
                    if not isinstance(raw_repo_root, str):
                        raise ValueError("remediation repository root is unavailable")
                    remediation_artifacts = _remediation_recovery_artifacts(
                        job,
                        repo_root=Path(raw_repo_root).resolve(strict=True),
                        worktree=worktree.resolve(strict=True),
                        branch=branch,
                        parent_sha=remediation_parent_sha,
                        candidate_tree_sha=remediation_tree_sha,
                        recovery_commit_sha=selected_recovery_commit,
                        paths=recovery_paths,
                        committed_diff=remediation_diff.text,
                        committed_diff_sha256=remediation_diff.sha256,
                    )
                except (OSError, TypeError, ValueError) as exc:
                    return JobResult(
                        ok=False,
                        value={"recovery_commit_sha": selected_recovery_commit},
                        error=f"remediation recovery journal is unavailable: {exc}",
                    )
            publication = self._publish_commit_push(job, branch, worktree)
            if not publication.ok or remediation_artifacts is None:
                return publication
            handoff, journal = remediation_artifacts
            value = dict(publication.value) if isinstance(publication.value, dict) else {}
            value["remediation_handoff"] = handoff
            value["remediation_journal"] = {"marker": journal[0], "body": journal[1]}
            return replace(publication, value=value)
        publication_head = self._read_publish_head(
            worktree,
            timeout=job.timeout_s,
            git_env=recovery_git_env,
        )
        if isinstance(publication_head, JobResult):
            if selected_recovery_commit is not None:
                value = (
                    dict(publication_head.value) if isinstance(publication_head.value, dict) else {}
                )
                value["recovery_commit_sha"] = selected_recovery_commit
                return replace(publication_head, value=value)
            return publication_head
        if selected_recovery_commit is not None and publication_head != selected_recovery_commit:
            return JobResult(
                ok=False,
                value={"recovery_commit_sha": selected_recovery_commit},
                error="remediation writer head changed after recovery selection",
            )
        selected_recovery_commit = publication_head

        def recovery_failure(result: JobResult) -> JobResult:
            """Attach the exact local child to each post-commit failure."""
            value = dict(result.value) if isinstance(result.value, dict) else {}
            value["recovery_commit_sha"] = selected_recovery_commit
            return replace(result, value=value)

        scope_retraction = self._verify_scope_retraction(
            job,
            worktree,
            git_env=recovery_git_env,
        )
        if scope_retraction is not None:
            return recovery_failure(scope_retraction)
        if not self._is_exact_recovery_commit(
            worktree,
            selected_recovery_commit,
            parent=cast(str, expected_recovery_head),
            tree=cast(str, expected_recovery_tree),
            timeout=job.timeout_s,
            git_env=recovery_git_env,
        ):
            return recovery_failure(
                JobResult(
                    ok=False,
                    error="remediation writer commit does not match the inspected tree",
                )
            )
        try:
            _refresh_verified_recovery_index(
                cast(Path, recovery_repo_root),
                worktree,
                expected_git_env=cast(dict[str, str], recovery_git_env),
                private_git_env=cast(
                    dict[str, str],
                    recovery_commit_git_env or recovery_git_env,
                ),
                source_sha=selected_recovery_commit,
                expected_tree=cast(str, expected_recovery_tree),
                timeout=job.timeout_s,
            )
            clean = _run_bounded_git_output(
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
                timeout=job.timeout_s,
                max_bytes=IMPLEMENTATION_INSPECTION_STATUS_MAX_BYTES,
                retain_text=True,
                env=recovery_git_env,
            ).text
            from hephaestus.automation.commit_paths import parse_porcelain_status

            clean_entries = parse_porcelain_status(clean)
            if len({path for _status, path in clean_entries}) > DIRTY_SNAPSHOT_CHANGED_FILE_MAX:
                raise _GitInspectionResourceLimitError("dirty snapshot file limit exceeded")
        except _GitInspectionResourceLimitError as exc:
            return recovery_failure(
                JobResult(
                    ok=False,
                    value={"failure_kind": "resource_limit_exceeded"},
                    error=f"remediation post-commit resource limit exceeded: {exc}",
                )
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return recovery_failure(
                JobResult(ok=False, error=f"remediation post-commit check failed: {exc}")
            )
        if clean.strip():
            return recovery_failure(
                JobResult(ok=False, error="remediation writer changed after commit")
            )
        if prepare_only:
            try:
                review_input = _remediation_review_input(
                    job,
                    repo_root=cast(Path, recovery_repo_root),
                    worktree=worktree,
                    branch=branch,
                    parent_sha=cast(str, expected_recovery_head),
                    candidate_tree_sha=cast(str, expected_recovery_tree),
                    recovery_commit_sha=selected_recovery_commit,
                    paths=cast(CommitPaths, recovery_paths),
                    committed_diff=cast(_BoundedGitOutput, remediation_diff).text,
                    committed_diff_sha256=cast(_BoundedGitOutput, remediation_diff).sha256,
                )
                journal_input_encoding, journal_input_data = encode_remediation_review_input(
                    review_input.canonical_bytes
                )
                receipt = RemediationRecoveryReceipt(
                    review_input_bytes=review_input.canonical_bytes.decode("utf-8"),
                    review_input_sha256=review_input.review_input_sha256,
                    journal_input_encoding=journal_input_encoding,
                    journal_input_data=journal_input_data,
                    expected_remote_sha=cast(str, expected_recovery_head),
                    content_snapshot=tuple(
                        sorted(cast(dict[str, str], expected_recovery_snapshot).items())
                    ),
                    add_paths=cast(CommitPaths, recovery_paths).add_paths,
                    update_paths=cast(CommitPaths, recovery_paths).update_paths,
                )
                batch_nonce = job.kwargs.get("remediation_batch_nonce")
                if not isinstance(batch_nonce, str):
                    raise ValueError("remediation recovery batch identity is unavailable")
                save_prepublication_receipt(
                    repo_root=cast(Path, recovery_repo_root),
                    receipt=receipt,
                    batch_nonce=batch_nonce,
                )
            except (OSError, TypeError, UnicodeError, ValueError) as exc:
                return recovery_failure(
                    JobResult(ok=False, error=f"remediation recovery receipt is unavailable: {exc}")
                )
            return JobResult(
                ok=True,
                value={
                    "pushed": False,
                    "head_sha": selected_recovery_commit,
                    "recovery_receipt": receipt.as_dict(),
                },
            )
        try:
            remediation_handoff, remediation_journal = _remediation_recovery_artifacts(
                job,
                repo_root=cast(Path, recovery_repo_root),
                worktree=worktree,
                branch=branch,
                parent_sha=cast(str, expected_recovery_head),
                candidate_tree_sha=cast(str, expected_recovery_tree),
                recovery_commit_sha=selected_recovery_commit,
                paths=cast(CommitPaths, recovery_paths),
                committed_diff=cast(_BoundedGitOutput, remediation_diff).text,
                committed_diff_sha256=cast(_BoundedGitOutput, remediation_diff).sha256,
            )
        except (TypeError, ValueError) as exc:
            return recovery_failure(
                JobResult(
                    ok=False,
                    error=f"remediation recovery journal is unavailable: {exc}",
                )
            )
        publication = self._publish_recovery_commit(
            job,
            branch,
            worktree,
            source_sha=selected_recovery_commit,
            expected_remote_sha=cast(str, expected_recovery_head),
            repo_root=cast(Path, recovery_repo_root),
            expected_git_env=cast(dict[str, str], recovery_git_env),
        )
        if not publication.ok:
            return publication
        value = dict(publication.value) if isinstance(publication.value, dict) else {}
        value["remediation_handoff"] = remediation_handoff
        value["remediation_journal"] = {
            "marker": remediation_journal[0],
            "body": remediation_journal[1],
        }
        return replace(publication, value=value)

    def _classify_recovery_commit_failure(
        self,
        worktree: Path,
        *,
        expected_head: str,
        expected_tree: str,
        timeout: int,
        cause: BaseException | None = None,
        git_env: dict[str, str] | None = None,
    ) -> JobResult:
        """Classify a failed commit without losing an exact completed child."""
        current_head = self._read_publish_head(worktree, timeout=timeout, git_env=git_env)
        if isinstance(current_head, str) and current_head != expected_head:
            if self._is_exact_recovery_commit(
                worktree,
                current_head,
                parent=expected_head,
                tree=expected_tree,
                timeout=timeout,
                git_env=git_env,
            ):
                return JobResult(
                    ok=False,
                    value={
                        "failure_kind": "commit_result_ambiguous",
                        "recovery_commit_sha": current_head,
                    },
                    error="remediation writer commit result is ambiguous",
                )
            return JobResult(ok=False, error="remediation writer head drift after commit")
        error = "remediation writer commit did not complete"
        if cause is not None:
            error = f"{error}: {cause}"
        return JobResult(
            ok=False,
            value={"failure_kind": "commit_failed"},
            error=error,
        )

    @staticmethod
    def _is_exact_recovery_commit(
        worktree: Path,
        commit: str,
        *,
        parent: str,
        tree: str,
        timeout: int,
        git_env: dict[str, str] | None = None,
    ) -> bool:
        """Return whether a signed DCO commit has the inspected parent and tree."""
        try:
            raw = git_utils.run(
                ["git", "cat-file", "-p", commit],
                cwd=worktree,
                timeout=timeout,
                env=git_env or _isolated_checkout_git_env(),
            ).stdout
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return False
        parents = [
            line.removeprefix("parent ") for line in raw.splitlines() if line.startswith("parent ")
        ]
        trees = [
            line.removeprefix("tree ") for line in raw.splitlines() if line.startswith("tree ")
        ]
        signature_header = "gpgsig-sha256 " if len(commit) == 64 else "gpgsig "
        return (
            parents == [parent]
            and trees == [tree]
            and any(line.startswith(signature_header) for line in raw.splitlines())
            and "Signed-off-by:" in raw
        )

    def _publish_recovery_commit(  # noqa: C901
        self,
        job: GitJob,
        branch: str,
        worktree: Path,
        *,
        source_sha: str,
        expected_remote_sha: str,
        repo_root: Path,
        expected_git_env: dict[str, str],
    ) -> JobResult:
        """Publish one inspected recovery commit with an exact remote lease."""
        if not branch:
            return JobResult(ok=False, error="commit publication branch is unavailable")
        try:
            expected_linked_env = getattr(expected_git_env, "linked_env", expected_git_env)
            rebound = _linked_worktree_git_env(repo_root, worktree)
            if not _linked_binding_matches(
                expected_linked_env,
                rebound,
                include_index=False,
            ):
                raise RuntimeError("remediation writer Git metadata identity changed")
            initial_binding = getattr(expected_linked_env, "binding", None)
            publication_binding = getattr(rebound, "binding", None)
            expected_branch_ref = f"refs/heads/{branch}"
            if isinstance(initial_binding, _LinkedWorktreeBinding) and (
                not isinstance(publication_binding, _LinkedWorktreeBinding)
                or initial_binding.branch_ref != expected_branch_ref
                or publication_binding.branch_ref != expected_branch_ref
                or initial_binding.branch_sha not in {expected_remote_sha, source_sha}
                or publication_binding.branch_sha != initial_binding.branch_sha
            ):
                raise RuntimeError("remediation writer branch binding changed")

            def revalidate_remote() -> tuple[dict[str, str], tuple[str, ...]]:
                """Rebind metadata and return one literal trusted remote."""
                current = _linked_worktree_git_env(repo_root, worktree)
                if not _linked_binding_matches(rebound, current, include_index=True):
                    raise RuntimeError("remediation writer Git metadata identity changed")
                current_binding = getattr(current, "binding", None)
                if isinstance(publication_binding, _LinkedWorktreeBinding) and (
                    not isinstance(current_binding, _LinkedWorktreeBinding)
                    or current_binding.branch_ref != expected_branch_ref
                    or current_binding.branch_sha != publication_binding.branch_sha
                ):
                    raise RuntimeError("remediation writer branch binding changed")
                remote_env, remote_config = self._authenticated_remote_git_configuration()
                remote_env.update(expected_git_env)
                return remote_env, remote_config

            remote_env, remote_config = revalidate_remote()
            trusted_url = f"https://github.com/{job.transport_repository}.git"
            git_utils.push_head_to_branch(
                branch,
                expected_remote_sha,
                worktree,
                source_sha=source_sha,
                timeout=job.timeout_s,
                env=remote_env,
                remote_config=remote_config,
                revalidate_remote=revalidate_remote,
                disable_hooks=True,
                remote=trusted_url,
            )
            post_push = _linked_worktree_git_env(repo_root, worktree)
            if not _linked_binding_matches(rebound, post_push, include_index=True):
                raise RuntimeError("remediation writer Git metadata identity changed")
            post_push_binding = getattr(post_push, "binding", None)
            if isinstance(publication_binding, _LinkedWorktreeBinding):
                if (
                    not isinstance(post_push_binding, _LinkedWorktreeBinding)
                    or post_push_binding.branch_ref != expected_branch_ref
                    or post_push_binding.branch_sha != publication_binding.branch_sha
                ):
                    raise RuntimeError("remediation writer branch binding changed")
                _compare_and_swap_linked_branch(
                    post_push_binding,
                    expected_sha=publication_binding.branch_sha,
                    new_sha=source_sha,
                )
                final_binding_env = _linked_worktree_git_env(repo_root, worktree)
                final_binding = getattr(final_binding_env, "binding", None)
                if (
                    not _linked_binding_matches(post_push, final_binding_env, include_index=True)
                    or not isinstance(final_binding, _LinkedWorktreeBinding)
                    or final_binding.branch_ref != expected_branch_ref
                    or final_binding.branch_sha != source_sha
                ):
                    raise RuntimeError("remediation writer local branch update is unavailable")
        except (
            git_utils.DetachedHeadPushRemoteHeadChangedError,
            git_utils.DetachedHeadPushRemoteHeadUnchangedError,
            git_utils.DetachedHeadPushRemoteProbeError,
        ) as exc:
            return JobResult(
                ok=False,
                error="recovery commit publication failed",
                value={
                    "failure_kind": exc.failure_kind,
                    "recovery_commit_sha": source_sha,
                },
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return JobResult(
                ok=False,
                error=f"recovery commit publication unavailable: {exc}",
                value={
                    "failure_kind": "publication_unavailable",
                    "recovery_commit_sha": source_sha,
                },
            )
        return JobResult(ok=True, value={"pushed": True, "head_sha": source_sha})

    @staticmethod
    def _verify_implementation_edit_scope(
        job: GitJob,
        worktree: Path,
        *,
        allowed_paths: Collection[str] | None,
    ) -> JobResult | None:
        """Reject dirty and committed edits outside the host-approved scope."""
        if allowed_paths is None:
            if agent_runtime.requires_codex_implementation_isolation(
                str(job.kwargs.get("agent", ""))
            ):
                return JobResult(ok=False, error="implementation approved scope is unavailable")
            return None
        if not allowed_paths or not all(
            is_safe_scope_retraction_path(path) for path in allowed_paths
        ):
            return JobResult(ok=False, error="implementation approved scope is unavailable")
        history_base_sha = job.kwargs.get("scope_history_base_sha")
        if not _is_full_commit_sha(history_base_sha):
            return JobResult(ok=False, error="cannot validate implementation edit scope")
        probes = (
            ["git", "diff", "--no-renames", "--name-only", "-z"],
            ["git", "diff", "--cached", "--no-renames", "--name-only", "-z"],
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            [
                "git",
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                f"{history_base_sha}..HEAD",
            ],
        )
        try:
            changed: set[str] = set()
            untracked: set[str] = set()
            for argv in probes:
                result = git_utils.run(
                    argv,
                    cwd=worktree,
                    capture_output=True,
                    timeout=job.timeout_s,
                )
                paths = {path for path in str(result.stdout or "").split("\0") if path}
                changed.update(paths)
                if "ls-files" in argv:
                    untracked = paths
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return JobResult(ok=False, error="cannot validate implementation edit scope")
        permitted = set(allowed_paths)
        retractions = job.kwargs.get("scope_retraction_paths")
        if retractions is not None:
            if (
                not isinstance(retractions, tuple)
                or not retractions
                or not all(is_safe_scope_retraction_path(path) for path in retractions)
                or untracked.intersection(retractions)
            ):
                return JobResult(ok=False, error="scope retraction verification unavailable")
            restoration = WorkerPool._verify_scope_retraction(job, worktree, include_worktree=True)
            if restoration is not None:
                return restoration
            permitted.update(retractions)
        if not changed.issubset(permitted):
            return JobResult(
                ok=False,
                error="implementation changed paths outside approved scope",
            )
        return None

    @staticmethod
    def _commit_if_changes_with_controlled_signing(
        job: GitJob,
        commit_args: tuple[int, Path, str],
        allowed_paths: Collection[str] | None,
        agent_model: object,
        git_message_timeout: int,
        *,
        recovery_paths: CommitPaths | None = None,
        expected_tree_sha: str | None = None,
        git_env: dict[str, str] | None = None,
    ) -> bool | str | JobResult:
        """Commit only dirty worktrees with the validated host signing identity."""
        operation_timeout = float(job.timeout_s)
        if job.deadline_s is not None:
            operation_timeout = cast(
                float,
                git_utils.remaining_operation_timeout(job.timeout_s),
            )
            if operation_timeout < 1:
                raise subprocess.TimeoutExpired("commit-message operation deadline", 0)
            git_message_timeout = min(git_message_timeout, int(operation_timeout))

        def signing_env_factory() -> dict[str, str]:
            signing_env = _controlled_git_signing_env(
                commit_args[1],
                timeout=cast(int, operation_timeout),
                private_metadata=isinstance(
                    git_env,
                    _PrivateLinkedWorktreeGitEnvironment,
                ),
            )
            if isinstance(signing_env, JobResult):
                raise git_utils.SigningEnvironmentUnavailableError(signing_env.error)
            if git_env is not None:
                signing_env.update(
                    {
                        key: value
                        for key, value in git_env.items()
                        if key
                        in {
                            "GIT_DIR",
                            "GIT_COMMON_DIR",
                            "GIT_INDEX_FILE",
                            "GIT_WORK_TREE",
                            "GIT_OBJECT_DIRECTORY",
                            "GIT_OPTIONAL_LOCKS",
                        }
                    }
                )
            return signing_env

        commit_kwargs: dict[str, Any] = {
            "allowed_paths": allowed_paths,
            "timeout": operation_timeout,
            "git_message_timeout": git_message_timeout,
            "signing_env_factory": signing_env_factory,
            "git_env": git_env or _isolated_checkout_git_env(),
            "issue_title": job.kwargs.get("issue_title"),
            "issue_body": job.kwargs.get("issue_body"),
            "claude_message_agent": _invoke_claude_commit_message,
        }
        if expected_tree_sha is not None:
            commit_kwargs["expected_tree_sha"] = expected_tree_sha
            commit_kwargs["return_commit_sha"] = True
            if recovery_paths is None:
                return JobResult(ok=False, error="remediation writer path manifest is unavailable")
            commit_kwargs["expected_add_paths"] = recovery_paths.add_paths
            commit_kwargs["expected_update_paths"] = recovery_paths.update_paths
            commit_kwargs["disable_hooks"] = True
        if agent_model is not None:
            commit_kwargs["agent_model"] = agent_model
        pi_dir = job.kwargs.get("pi_dir")
        if pi_dir is not None:
            commit_kwargs["pi_dir"] = Path(str(pi_dir))
        try:
            return git_utils.commit_if_changes(*commit_args, **commit_kwargs)
        except git_utils.SigningEnvironmentUnavailableError as exc:
            return JobResult(
                ok=False,
                value={"failure_kind": "signing_configuration"},
                error=str(exc),
            )

    @staticmethod
    def _verify_scope_retraction(
        job: GitJob,
        worktree_path: Path,
        *,
        git_env: dict[str, str] | None = None,
        include_worktree: bool = False,
    ) -> JobResult | None:
        """Reject publication unless host-designated paths match the reviewed base.

        The coordinator derives these paths only from validated scope-control
        review findings. Re-check their shape here before using them as Git
        pathspecs, then compare the exact post-commit ``HEAD`` with the base
        from the review checkout barrier. A failed check remains local-only.
        """
        paths = job.kwargs.get("scope_retraction_paths")
        if paths is None:
            return None
        base_sha = job.kwargs.get("scope_retraction_base_sha")
        if (
            not _is_full_commit_sha(base_sha)
            or not isinstance(paths, tuple)
            or not paths
            or not all(
                isinstance(path, str) and is_safe_scope_retraction_path(path) for path in paths
            )
        ):
            return JobResult(
                ok=False,
                value={"scope_retraction_failure": True},
                error="scope retraction verification unavailable",
            )
        try:
            run_kwargs: dict[str, Any] = {
                "capture_output": True,
                "timeout": job.timeout_s,
            }
            if git_env is not None:
                run_kwargs["env"] = git_env
            result = git_utils.run(
                [
                    "git",
                    "--literal-pathspecs",
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--name-only",
                    base_sha,
                    *(() if include_worktree else ("HEAD",)),
                    "--",
                    *paths,
                ],
                cwd=worktree_path,
                **run_kwargs,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return JobResult(
                ok=False,
                value={"scope_retraction_failure": True},
                error="scope retraction verification unavailable",
            )
        if str(result.stdout or "").strip():
            return JobResult(
                ok=False,
                value={"scope_retraction_failure": True},
                error="scope retraction incomplete",
            )
        return None

    def _publish_commit_push(
        self,
        job: GitJob,
        branch: str,
        worktree_path: Path,
        *,
        expected_head: str | None = None,
        expected_content_snapshot: dict[str, str] | None = None,
    ) -> JobResult:
        """Publish a newly created commit and return its exact immutable SHA."""
        if not branch:
            return JobResult(ok=False, error="commit publication branch is unavailable")
        expected_remote_sha = job.kwargs.get("expected_remote_sha")
        if expected_remote_sha is not None and not _is_full_commit_sha(expected_remote_sha):
            return JobResult(ok=False, error="direct scope base pin invalid")
        publication_bound = expected_head is not None or expected_content_snapshot is not None
        if publication_bound and (
            not _is_full_commit_sha(expected_head)
            or not _valid_dirty_content_snapshot(expected_content_snapshot)
        ):
            return JobResult(ok=False, error="remediation publication binding invalid")
        source_sha = self._read_publish_head(worktree_path, timeout=job.timeout_s)
        if isinstance(source_sha, JobResult):
            return source_sha
        if publication_bound and source_sha != expected_head:
            return JobResult(ok=False, error="remediation writer head drift before push")
        if publication_bound:
            try:
                current_snapshot = _dirty_worktree_content_snapshot(
                    worktree_path,
                    timeout=job.timeout_s,
                )
            except (
                _GitInspectionResourceLimitError,
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
            ):
                return JobResult(
                    ok=False,
                    error="remediation writer content binding unavailable before push",
                )
            if current_snapshot != expected_content_snapshot:
                return JobResult(
                    ok=False,
                    error="remediation writer content drift before push",
                )

        revalidate_remote = self._authenticated_remote_revalidator(
            cwd=worktree_path, expected_repo=job.transport_repository, timeout=job.timeout_s
        )
        remote_env, remote_config = revalidate_remote()
        if isinstance(expected_remote_sha, str):
            strict_push_kwargs: dict[str, Any] = {
                "timeout": job.timeout_s,
                "env": remote_env,
                "remote_config": remote_config,
                "source_sha": source_sha,
            }
            git_utils.push_branch_if_remote_matches(
                branch,
                expected_remote_sha,
                worktree_path,
                **strict_push_kwargs,
            )
        else:
            push_kwargs: dict[str, Any] = {
                "timeout": job.timeout_s,
                "env": remote_env,
                "remote_config": remote_config,
                "source_sha": source_sha,
            }
            git_utils.push_branch(branch, worktree_path, **push_kwargs)
        return JobResult(ok=True, value={"pushed": True, "head_sha": source_sha})

    @staticmethod
    def _read_publish_head(
        worktree_path: Path,
        *,
        timeout: int,
        git_env: dict[str, str] | None = None,
    ) -> str | JobResult:
        """Read the immutable commit the implementation writer will publish."""
        try:
            head = git_utils.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree_path,
                capture_output=True,
                timeout=timeout,
                env=git_env or _isolated_checkout_git_env(),
            ).stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return JobResult(ok=False, error="cannot bind implementation publish head")
        if not _is_full_commit_sha(head):
            return JobResult(ok=False, error="cannot bind implementation publish head")
        return head

    def _read_remote_branch_head(
        self,
        worktree_path: Path,
        *,
        remote: str,
        branch: str,
        expected_repo: str,
        timeout: int,
    ) -> str | JobResult:
        """Read one exact remote branch head without updating local refs."""
        expected_ref = f"refs/heads/{branch}"
        remote_env, remote_config = self._authenticated_remote_git_configuration(
            cwd=worktree_path,
            expected_repo=expected_repo,
            timeout=timeout,
        )
        try:
            fields = git_utils.run(
                ["git", *remote_config, "ls-remote", "--refs", remote, expected_ref],
                cwd=worktree_path,
                timeout=timeout,
                env=remote_env,
            ).stdout.split()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return JobResult(ok=False, error="cannot verify remote writer head")
        if len(fields) != 2 or not _is_full_commit_sha(fields[0]) or fields[1] != expected_ref:
            return JobResult(ok=False, error="cannot verify remote writer head")
        return fields[0]

    def _verify_noop_writer_rebase(
        self,
        worktree_path: Path,
        *,
        remote: str,
        branch: str,
        expected_repo: str,
        expected_remote_sha: str,
        timeout: int,
    ) -> JobResult:
        """Bind an already-current local writer to its unchanged remote head."""
        source_sha = self._read_publish_head(worktree_path, timeout=timeout)
        if isinstance(source_sha, JobResult):
            return source_sha
        if source_sha != expected_remote_sha:
            return JobResult(
                ok=False,
                error="current writer head does not match expected remote head",
            )
        remote_head = self._read_remote_branch_head(
            worktree_path,
            remote=remote,
            branch=branch,
            expected_repo=expected_repo,
            timeout=timeout,
        )
        if isinstance(remote_head, JobResult):
            return remote_head
        if remote_head != expected_remote_sha:
            return JobResult(
                ok=False,
                error="remote writer head changed during rebase preparation",
            )
        return JobResult(
            ok=True,
            value={
                "rebased": False,
                "published": False,
                "head_sha": source_sha,
            },
        )

    def _commit_push_requires_publish(
        self, *, job: GitJob, branch: str, worktree_path: Path
    ) -> bool | JobResult:
        """Return whether a clean worktree still needs coordinator-owned publication."""
        expected_remote_sha = job.kwargs.get("expected_remote_sha")
        if expected_remote_sha is None:
            publish_base_sha = job.kwargs.get("publish_base_sha")
            if publish_base_sha is not None and not _is_full_commit_sha(publish_base_sha):
                return JobResult(ok=False, error="publication base pin invalid")
            if publish_base_sha is None:
                return bool(
                    branch
                    and git_utils.has_unpushed_commits(
                        branch,
                        worktree_path,
                        timeout=job.timeout_s,
                    )
                )
            return bool(
                branch
                and git_utils.has_unpushed_commits(
                    branch,
                    worktree_path,
                    base_revision=publish_base_sha,
                    timeout=job.timeout_s,
                )
            )
        if not _is_full_commit_sha(expected_remote_sha):
            return JobResult(ok=False, error="direct scope base pin invalid")
        if not branch:
            return JobResult(ok=False, error="direct scope branch name is missing")
        ahead = git_utils.run(
            ["git", "rev-list", "--count", f"{expected_remote_sha}..HEAD"],
            cwd=worktree_path,
            capture_output=True,
            check=False,
            timeout=job.timeout_s,
        )
        if ahead.returncode != 0:
            return JobResult(ok=False, error="cannot verify direct scope branch ancestry")
        if ahead.stdout.strip() != "0":
            return True

        revalidate_remote = self._authenticated_remote_revalidator(
            cwd=worktree_path, expected_repo=job.transport_repository, timeout=job.timeout_s
        )
        remote_env, remote_config = revalidate_remote()
        released = git_utils.delete_reserved_branch_if_unchanged(
            branch,
            expected_remote_sha,
            worktree_path,
            timeout=job.timeout_s,
            env=remote_env,
            remote_config=remote_config,
            revalidate_remote=revalidate_remote,
        )
        if not released:
            return JobResult(
                ok=False,
                error="direct scope reservation changed before it could be released",
            )
        return False
