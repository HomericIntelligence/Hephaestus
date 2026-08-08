"""Worker pool: the only place agent, build/test, git, GitHub, and session work runs.

The coordinator submits frozen jobs and drains ``(handle, result)`` tuples from
the completion queue. Workers never touch WorkItems or stage queues and never
touch coordinator state. Closed GitHub jobs use a separately injected runner.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import queue as queue_mod
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Collection, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from contextvars import copy_context
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeGuard, cast

import hephaestus.automation.claude_invoke as claude_invoke
import hephaestus.automation.git_utils as git_utils
import hephaestus.automation.subprocess_registry as subprocess_registry
from hephaestus.agents.runtime import (
    AgentExecutionError,
    resolve_agent,
    resume_agent_session,
    run_agent_session,
)
from hephaestus.automation.learn import compact_agent_session
from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.automation.pipeline.github_jobs import GitHubJob, GitHubJobRunner
from hephaestus.automation.pipeline.jobs import (
    WORKTREE_MATERIALIZED_KEY,
    AgentJob,
    BuildTestJob,
    CompactJob,
    GitJob,
    JobHandle,
    JobResult,
)
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.scope_retraction import is_safe_scope_retraction_path
from hephaestus.automation.pipeline.tool_scopes import (
    DEFAULT_TOOL_SCOPE,
    ToolScope,
    tool_scope_for,
)
from hephaestus.automation.worktree_manager import (
    BRANCH_WORKTREE_OWNED,
    BranchWorktreeOwnedError,
    WorktreeManager,
)
from hephaestus.io.utils import write_secure
from hephaestus.resilience import (
    CircuitBreakerOpenError,
    resilient_call,
)
from hephaestus.utils.file_lock import LockUnavailableError, file_lock
from hephaestus.utils.helpers import get_repo_root

logger = logging.getLogger(__name__)

_TAIL = 4000  # chars of stdout/stderr retained in a JobResult
_ERR_MAX = 500  # chars of error detail retained in a JobResult
_GIT_LOCK_WAIT_POLL_S = 0.1
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
_HOST_RUNTIME_CACHE_DIRNAME = "hephaestus-host-validation-runtime"
_HOST_RUNTIME_CACHE_FORMAT = b"sealed-runtime-v6-shell-launchers"
_HOST_RUNTIME_MANIFEST_HEADER = "sealed-runtime-file-manifest-v1"

# The host verification handles code from an untrusted pull request.  Bound
# the Git archive before extraction and bound every child output/write path.
_HOST_VERIFICATION_ARCHIVE_MAX_BYTES = 64 * 1024 * 1024
_HOST_VERIFICATION_ARCHIVE_MAX_MEMBERS = 20_000
_HOST_VERIFICATION_SCRATCH_MAX_BYTES = 64 * 1024 * 1024
_HOST_VERIFICATION_OUTPUT_FILE_MAX_BLOCKS = 2_048  # POSIX ulimit -f units
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

    env = {
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "XDG_CACHE_HOME": str(cache),
        "UV_CACHE_DIR": str(cache / "uv"),
        # The coordinator's existing runtime environment is host-owned and
        # read-only inside the OS sandbox.  It has the locked dependencies
        # needed for an offline ``uv run`` without exposing a user home/cache.
        "UV_PROJECT_ENVIRONMENT": str(runtime_environment),
        "UV_OFFLINE": "1",
        "UV_NO_SYNC": "1",
        "RUFF_CACHE_DIR": str(cache / "ruff"),
        "COVERAGE_FILE": str(cache / ".coverage"),
        "PYTHONPYCACHEPREFIX": str(cache / "pycache"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
        "PATH": os.pathsep.join(
            (
                str(Path(executable).parent),
                *((str(Path(git_executable).parent),) if git_executable else ()),
                os.defpath,
            )
        ),
    }
    # Locale is harmless input-only process configuration and prevents tools
    # from producing platform-dependent decoding failures.
    for key in ("LANG", "LC_ALL", "TZ"):
        if value := os.environ.get(key):
            env[key] = value
    return env


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
            # Tests may stop only child processes they created; no signals to
            # unrelated host processes are allowed.
            "(allow signal (target children))",
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
        coverage_alias.symlink_to(scratch / "coverage.xml")
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


def _is_full_commit_sha(value: object) -> TypeGuard[str]:
    """Return whether ``value`` is a full SHA-1 or SHA-256 commit id."""
    return bool(
        isinstance(value, str)
        and len(value) in (40, 64)
        and all(character in "0123456789abcdef" for character in value)
    )


def _controlled_git_env() -> dict[str, str]:
    """Return an environment that cannot redirect or extend Git execution."""
    env = os.environ.copy()
    for key in _FETCH_ENV_BLOCKLIST:
        env.pop(key, None)
    for key in tuple(env):
        if key == "GIT_CONFIG_COUNT" or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["PATH"] = os.defpath
    env["GIT_PAGER"] = "cat"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _trusted_executable(name: str, *, path: str | None = None) -> str | None:
    """Resolve a command to an absolute path before entering a controlled env."""
    executable = shutil.which(name, path=path)
    return str(Path(executable).resolve()) if executable is not None else None


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
    for candidate in _TRUSTED_GIT_CANDIDATES:
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


def _trusted_gh_executable(extra_path_root: Path | None = None) -> str | None:
    """Return an allowed absolute ``gh`` binary without consulting ``PATH``.

    ``extra_path_root`` is an explicit operator authority passed only through
    the loop CLI.  It contributes exactly ``<root>/bin/gh`` and rejects a
    candidate whose resolved path escapes that root.
    """
    candidates: tuple[Path, ...] = _TRUSTED_GH_CANDIDATES
    if extra_path_root is not None:
        candidates = (*candidates, extra_path_root / "bin" / "gh")
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            continue
        if any(resolved.is_relative_to(root) for root in _TRUSTED_GH_ROOTS):
            return str(resolved)
        if extra_path_root is not None:
            try:
                resolved_root = extra_path_root.resolve(strict=True)
            except OSError:
                continue
            if resolved.is_relative_to(resolved_root):
                return str(resolved)
    return None


def _unsafe_local_git_config_key(config: str) -> str | None:
    """Return an unsafe repository/worktree config key, if *config* contains one."""
    for entry in config.split("\0"):
        if not entry:
            continue
        key, _separator, _value = entry.partition("\n")
        normalized = key.lower()
        if normalized in {
            "core.askpass",
            "core.attributesfile",
            "core.fsmonitor",
            "core.gitproxy",
            "core.sshcommand",
            "core.worktree",
        }:
            return key
        if normalized == "credential.helper" or (
            normalized.startswith("credential.") and normalized.endswith(".helper")
        ):
            return key
        if normalized.startswith("remote.") and normalized.rsplit(".", 1)[-1] in {
            "proxy",
            "proxyauthmethod",
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


def _checkout_preflight_error(checkout: Path, timeout_s: int) -> str | None:
    """Return a reusable-checkout metadata safety failure before synchronization."""
    if not (checkout / ".git").exists():
        return None
    unsafe_config = _unsafe_local_git_config_key(
        git_utils.run(
            ["git", "config", "--null", "--list"],
            cwd=checkout,
            timeout=timeout_s,
            env=_controlled_git_env(),
        ).stdout
    )
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

    @contextmanager
    def _repo_lock(self, repo: str) -> Iterator[None]:
        """Serialize in-process worker operations for one repository."""
        with self._repo_locks_guard:
            entry = self._repo_locks.get(repo)
            if entry is None:
                entry = _RepoLockEntry(threading.Lock())
                self._repo_locks[repo] = entry
            entry.users += 1

        try:
            with entry.lock:
                yield
        finally:
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
        job: AgentJob | BuildTestJob | GitJob | GitHubJob | CompactJob,
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
        job: AgentJob | BuildTestJob | GitJob | GitHubJob | CompactJob,
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
                if isinstance(job, AgentJob):
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

        return replace(
            result,
            duration_s=time.monotonic() - start,
            stdout_tail=result.stdout_tail[-_TAIL:] if result.stdout_tail else "",
            stderr_tail=result.stderr_tail[-_TAIL:] if result.stderr_tail else "",
            worker_id=worker_id,
        )

    def _run_github(self, job: GitHubJob) -> JobResult:
        """Execute one closed GitHub operation exactly once per submission."""
        if self._github_job_runner is None:
            raise RuntimeError("GitHubJob submitted without a GitHubJobRunner")
        with self._repo_lock(job.repo):
            receipt = self._github_job_runner.run(job)
        return JobResult(ok=True, value=receipt)

    def _run_agent(self, job: AgentJob) -> JobResult:
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
        try:
            agent = resolve_agent(job.agent, cwd=job.cwd)
            is_claude = agent == "claude"
            session_agent = job.session_agent or job.agent
            prompt = job.prompt_builder(**job.prompt_kwargs)

            def _invoke() -> tuple[str, str | None]:
                if is_claude:
                    # Scope priority: an explicit per-job grant (a stage that
                    # knows its exact needs, e.g. pr_review) wins; a read-only
                    # sandbox without one clamps to the fail-closed default;
                    # everything else resolves by session-agent role.
                    if job.allowed_tools:
                        scope = ToolScope(job.allowed_tools)
                    elif job.sandbox == "read-only":
                        scope = DEFAULT_TOOL_SCOPE
                    else:
                        scope = tool_scope_for(session_agent)
                    stdout, _ = claude_invoke.invoke_claude_with_session(
                        repo=job.repo,
                        issue=job.issue,
                        agent=session_agent,
                        prompt=prompt,
                        model=job.model,
                        cwd=job.cwd,
                        timeout=job.timeout_s,
                        output_format=job.output_format,
                        allowed_tools=scope.allowed_tools,
                        permission_mode=scope.permission_mode,
                        input_via_stdin=True,
                    )
                    return stdout, None
                if job.resume_session_id:
                    agent_result = resume_agent_session(
                        agent=agent,
                        session_id=job.resume_session_id,
                        prompt=prompt,
                        cwd=job.cwd,
                        timeout=job.timeout_s,
                        model=job.model,
                        sandbox=job.sandbox,
                        approval="never",
                        process_tracker=subprocess_registry.track_process_group,
                    )
                else:
                    agent_result = run_agent_session(
                        agent=agent,
                        prompt=prompt,
                        cwd=job.cwd,
                        timeout=job.timeout_s,
                        model=job.model,
                        sandbox=job.sandbox,
                        approval="never",
                        process_tracker=subprocess_registry.track_process_group,
                    )
                # A resumed command may not repeat the session-start event;
                # retain the known id in that case.
                return agent_result.stdout or "", agent_result.session_id or job.resume_session_id

            stdout, session_id = resilient_call(
                _invoke,
                circuit_breaker_name=f"agent:{agent}",
                retry_predicate=lambda _exc: not self._shutdown.is_set(),
            )

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
                    )

            return JobResult(
                ok=True,
                value=value if value is not None else stdout,
                stdout_tail=stdout[-_TAIL:],
                session_id=session_id,
            )

        except CircuitBreakerOpenError:
            return JobResult(ok=False, error="circuit_open")
        except subprocess.TimeoutExpired:
            return JobResult(ok=False, error="timeout")
        except subprocess.CalledProcessError as exc:
            return JobResult(
                ok=False,
                error=f"rc={exc.returncode}",
                stdout_tail=(exc.stdout or "")[-_TAIL:],
                stderr_tail=(exc.stderr or "")[-_TAIL:],
            )
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
        try:
            result = subprocess.run(
                job.argv,
                cwd=str(job.cwd),
                capture_output=True,
                text=True,
                timeout=job.timeout_s,
                check=False,  # we inspect rc below
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
                self._repo_lock(job.repo),
                _interruptible_file_lock(
                    lock_path,
                    shutdown=self._shutdown,
                    timeout_s=job.timeout_s,
                ),
            ):
                return self._dispatch_git_op(job)
        except _GitLockTimeoutError:
            return JobResult(ok=False, error="lock_timeout")
        except _GitLockInterruptedError:
            return JobResult(
                ok=False,
                interrupted=True,
                error="interrupted_waiting_for_git_lock",
            )
        except BranchWorktreeOwnedError as exc:
            return JobResult(
                ok=False,
                error=BRANCH_WORKTREE_OWNED,
                value={"branch": exc.branch, "owner_path": str(exc.owner_path)},
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

    def _dispatch_git_op(self, job: GitJob) -> JobResult:  # noqa: C901
        """Dispatch a git operation to its handler.

        ``job.timeout_s`` is threaded into every git helper call so network
        operations cannot outlive the job budget while holding repo locks.
        """
        if job.op == "create_worktree":
            return self._git_create_worktree(job)

        elif job.op == "verify_pr_review_checkout":
            return self._git_verify_pr_review_checkout(job)

        elif job.op == "remove_worktree":
            return self._git_remove_worktree(job)

        elif job.op == "rebase":
            return self._git_rebase(job)

        elif job.op == "continue_rebase":
            return self._git_continue_rebase(job)

        elif job.op == "push":
            git_utils.push_current_branch_with_lease_on_divergence(
                **job.kwargs,
                timeout=job.timeout_s,
            )
            return JobResult(ok=True)

        elif job.op == "commit_push":
            return self._git_commit_push(job)

        elif job.op == "release_branch_reservation":
            branch_name = str(job.kwargs.get("branch") or "")
            base_sha = job.kwargs.get("base_sha")
            repo_root_value = job.kwargs.get("repo_root")
            repo_root = Path(str(repo_root_value)) if repo_root_value else None
            if (
                not branch_name
                or not _is_full_commit_sha(base_sha)
                or repo_root is None
                or not repo_root.is_dir()
            ):
                return JobResult(
                    ok=False,
                    error="release_branch_reservation requires branch, base_sha, and repo_root",
                )
            released = git_utils.delete_reserved_branch_if_unchanged(
                branch_name,
                base_sha,
                repo_root,
                timeout=job.timeout_s,
            )
            return JobResult(ok=True, value=released)

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

    def _git_rebase(self, job: GitJob) -> JobResult:
        """Rebase an implementation writer and optionally lease-publish its head."""
        kwargs = dict(job.kwargs)
        if "publish_detached_head" in kwargs:
            return JobResult(
                ok=False,
                error="detached reviewer rebase publication is unsupported",
            )
        publish_rebased_head = bool(kwargs.pop("publish_rebased_head", False))
        sync_to_expected_remote_head = bool(kwargs.pop("sync_to_expected_remote_head", False))
        branch = str(kwargs.pop("branch", "") or "")
        expected_remote_sha = kwargs.pop("expected_remote_sha", None)
        pr_number = kwargs.pop("pr_number", None)
        cwd = Path(str(kwargs.get("cwd") or ""))
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
                pr_number=pr_number,
                expected_remote_sha=expected_remote_sha,
                timeout=job.timeout_s,
            )
            if synced is not None:
                return synced
            git_utils.run(
                ["git", "fetch", remote, base_branch],
                cwd=cwd,
                timeout=job.timeout_s,
            )
            ancestry = git_utils.run(
                ["git", "merge-base", "--is-ancestor", base_ref, "HEAD"],
                cwd=cwd,
                check=False,
                timeout=job.timeout_s,
            )
            if ancestry.returncode == 0:
                return self._verify_noop_writer_rebase(
                    cwd,
                    remote=remote,
                    branch=branch,
                    expected_remote_sha=expected_remote_sha,
                    timeout=job.timeout_s,
                )
            if ancestry.returncode != 1:
                return JobResult(ok=False, error="cannot determine writer base ancestry")
        result = git_utils.rebase_worktree_onto(
            **kwargs,
            preserve_conflicts=publish_rebased_head,
            timeout=job.timeout_s,
        )
        if not result:
            if not publish_rebased_head:
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
        source_sha = self._read_publish_head(cwd, timeout=job.timeout_s)
        if isinstance(source_sha, JobResult):
            return source_sha
        git_utils.push_head_to_branch(
            branch,
            expected_remote_sha,
            cwd,
            source_sha=source_sha,
            timeout=job.timeout_s,
        )
        return JobResult(
            ok=True,
            value={"rebased": True, "published": True, "head_sha": source_sha},
        )

    def _sync_writer_to_expected_remote_head(
        self,
        cwd: Path,
        *,
        enabled: bool,
        branch: str,
        remote: str,
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
            git_utils.sync_worktree_to_remote_branch(
                cwd,
                branch,
                remote=remote,
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
                ["git", "ls-files", "--unmerged", "-z", "--", *paths],
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
            cwd, remote=remote, branch=branch, timeout=job.timeout_s
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
        git_utils.push_head_to_branch(
            branch,
            expected_remote_sha,
            cwd,
            source_sha=source_sha,
            timeout=job.timeout_s,
        )
        return JobResult(
            ok=True,
            value={"rebased": True, "published": True, "head_sha": source_sha},
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
        """Reject tracked, staged, or untracked changes outside conflicts."""
        allowed = set(conflict_paths)
        probes = (
            ["git", "diff", "--name-only", "-z"],
            ["git", "diff", "--cached", "--name-only", "-z"],
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
        try:
            git_utils.run(["git", "add", "--", *paths], cwd=cwd, timeout=timeout)
            git_utils.run(
                ["git", "diff", "--cached", "--check"],
                cwd=cwd,
                timeout=timeout,
            )
            env = _controlled_git_env()
            env["GIT_EDITOR"] = "true"
            git_utils.run(
                ["git", "rebase", "--continue"],
                cwd=cwd,
                env=env,
                timeout=timeout,
            )
        except subprocess.CalledProcessError:
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
            return JobResult(ok=False, error="host could not continue rebase")
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

        Generated and intermediate files must be covered by the repository's
        ignore rules; any other uncommitted path blocks synchronization.
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
                "--untracked-files=all",
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
        if branch != default_branch:
            return JobResult(
                ok=False,
                error=(
                    f"checkout is not on its default branch {default_branch}: currently on {branch}"
                ),
            )
        return self._fast_forward_checkout(
            checkout=checkout,
            default_branch=default_branch,
            gh_command=gh_command,
            timeout_s=timeout_s,
        )

    @staticmethod
    def _checkout_state_error(*, checkout: Path, default_branch: str, timeout_s: int) -> str | None:
        """Return the clean-default-branch validation error, if any."""
        status = git_utils.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain",
                "--untracked-files=all",
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
        if branch != default_branch:
            return f"checkout is not on its default branch {default_branch}: currently on {branch}"
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
        ssh_command = _trusted_executable("ssh", path=os.defpath)
        if ssh_command is None:
            return JobResult(ok=False, error="required fetch executable is unavailable")
        ssh_config = " ".join(
            (
                shlex.quote(ssh_command),
                "-F",
                shlex.quote(os.devnull),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
            )
        )
        fetch_config = [
            "-c",
            hooks_disabled,
            "-c",
            f"core.sshCommand={ssh_config}",
            "-c",
            "credential.helper=",
            "-c",
            f"credential.helper=!{shlex.quote(gh_command)} auth git-credential",
            "-c",
            "core.askPass=",
            "-c",
            "http.sslVerify=true",
        ]
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
            default_branch=default_branch,
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
            default_branch=default_branch,
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
        if isinstance(base_sha, str):
            manager = WorktreeManager(
                base_dir=base_dir,
                base_branch=base_sha,
                repo_root=repo_root,
            )
        else:
            manager = WorktreeManager(base_dir=base_dir, repo_root=repo_root)
        if base_sha is not None:
            kwargs["base_sha"] = base_sha
            kwargs["remote_branch_reserved"] = True
        try:
            created = manager.create_worktree(**kwargs, timeout=job.timeout_s)
        except Exception as exc:
            if base_sha is not None:
                return self._rollback_direct_scope_reservation(
                    branch_name=branch_name,
                    base_sha=base_sha,
                    repo_root=repo_root,
                    timeout_s=job.timeout_s,
                    error=f"worktree creation failed: {exc}",
                )
            raise
        return self._finalize_created_worktree(
            created=created,
            base_sha=base_sha,
            branch_name=branch_name,
            repo_root=repo_root,
            repo=job.repo,
            sync_to_remote=sync_to_remote,
            pr_number=pr_number,
            timeout_s=job.timeout_s,
        )

    @staticmethod
    def _release_direct_scope_reservation(
        branch_name: str,
        base_sha: str | None,
        repo_root: Path,
        *,
        timeout_s: int,
    ) -> bool:
        """Conditionally release a direct reservation, or no-op for normal worktrees."""
        return base_sha is None or git_utils.delete_reserved_branch_if_unchanged(
            branch_name,
            base_sha,
            repo_root,
            timeout=timeout_s,
        )

    def _rollback_direct_scope_reservation(
        self,
        *,
        branch_name: str,
        base_sha: str,
        repo_root: Path,
        timeout_s: int,
        error: str,
    ) -> JobResult:
        """Release an early reservation or preserve its receipt for Finished."""
        try:
            released = self._release_direct_scope_reservation(
                branch_name, base_sha, repo_root, timeout_s=timeout_s
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

    def _finalize_created_worktree(
        self,
        *,
        created: Path | None,
        base_sha: str | None,
        branch_name: str,
        repo_root: Path,
        repo: str,
        sync_to_remote: bool,
        pr_number: object,
        timeout_s: int,
    ) -> JobResult:
        """Validate a created worktree and attach a direct reservation receipt."""
        if created is None:
            if base_sha is not None:
                return self._rollback_direct_scope_reservation(
                    branch_name=branch_name,
                    base_sha=base_sha,
                    repo_root=repo_root,
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
                    timeout_s=timeout_s,
                    error=error,
                )
            return JobResult(
                ok=False,
                error=error,
            )
        if not sync_to_remote:
            if base_sha is not None:
                return JobResult(
                    ok=True,
                    value={
                        "path": str(worktree_path),
                        "direct_scope_reservation": {
                            "branch": branch_name,
                            "base_sha": base_sha,
                        },
                    },
                )
            return JobResult(ok=True, value=str(worktree_path))

        if pr_number is not None and not isinstance(pr_number, (int, str)):
            return JobResult(ok=False, error="worktree sync received an invalid PR number")

        try:
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
            elif branch_name:
                git_utils.sync_worktree_to_remote_branch(
                    worktree_path,
                    branch_name,
                    pr_number=int(pr_number) if isinstance(pr_number, (int, str)) else None,
                    timeout=timeout_s,
                )
        except Exception as exc:
            return JobResult(
                ok=False,
                error=f"worktree post-create preparation failed: {exc}",
                value={"path": str(worktree_path), WORKTREE_MATERIALIZED_KEY: True},
            )
        value: dict[str, object] = {
            "path": str(worktree_path),
            "dirty": dirty,
            "status": status,
            "diff": diff,
        }
        return JobResult(ok=True, value=value)

    @staticmethod
    def _prepare_direct_scope_worktree(
        *,
        kwargs: dict[str, object],
        sync_to_remote: bool,
        repo_root: Path,
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
        git_utils.reserve_remote_branch_if_absent(
            branch_name,
            base_sha,
            repo_root,
            timeout=timeout_s,
        )
        return base_sha, branch_name

    @staticmethod
    def _git_verify_pr_review_checkout(job: GitJob) -> JobResult:
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
        git_utils.sync_worktree_to_remote_branch(
            worktree,
            branch,
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
        git_utils.run(
            ["git", "fetch", "origin", "--", base_branch],
            cwd=worktree,
            timeout=job.timeout_s,
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
        if job.kwargs.get("worktree_path"):
            worktree_path = Path(str(job.kwargs["worktree_path"]))
            repo_root = Path(str(job.kwargs.get("repo_root") or get_repo_root()))
            # This is the same lock create_worktree takes. Keep it across
            # removal, prune, and local cleanup so pipeline workers cannot
            # attach the branch between those operations. The final
            # ``git branch -d`` check also refuses an externally checked-out
            # branch.
            with file_lock(WorktreeManager.git_metadata_lock_path(repo_root)):
                cmd = ["git", "worktree", "remove", str(worktree_path)]
                if job.kwargs.get("force"):
                    cmd.append("--force")
                git_utils.run(cmd, cwd=repo_root, timeout=job.timeout_s)
                git_utils.run(
                    ["git", "worktree", "prune"],
                    cwd=repo_root,
                    check=False,
                    timeout=job.timeout_s,
                )
                local_cleanup = job.kwargs.get("local_branch_cleanup")
                if local_cleanup is not None:
                    if not isinstance(local_cleanup, dict):
                        return JobResult(ok=False, error="local branch cleanup receipt is invalid")
                    branch_name = local_cleanup.get("branch")
                    expected_sha = local_cleanup.get("base_sha")
                    if not isinstance(branch_name, str) or not _is_full_commit_sha(expected_sha):
                        return JobResult(ok=False, error="local branch cleanup receipt is invalid")
                    deleted = git_utils.delete_local_branch_if_unchanged(
                        branch_name,
                        expected_sha,
                        repo_root,
                        timeout=job.timeout_s,
                    )
                    return JobResult(ok=True, value={"local_branch_deleted": deleted})
            return JobResult(ok=True)
        fallback_root = Path(str(job.kwargs.get("repo_root") or get_repo_root()))
        manager = WorktreeManager(repo_root=fallback_root)
        manager.remove_worktree(**job.kwargs, timeout=job.timeout_s)
        return JobResult(ok=True)

    def _git_commit_push(self, job: GitJob) -> JobResult:
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
        # ``commit_if_changes`` returns False for a clean worktree.  An agent
        # is instructed to leave its edits uncommitted, but a defensive
        # recovery still recognizes a clean branch that is ahead of its
        # remote tracking ref: the coordinator, not the agent, publishes that
        # already-created commit so every subsequent review binds to the new
        # remote head.
        commit_args = (
            int(issue_number),
            Path(worktree_path),
            str(job.kwargs.get("agent", "claude")),
        )
        allowed_paths = cast(Collection[str] | None, job.kwargs.get("allowed_paths"))
        agent_model = job.kwargs.get("agent_model")
        if agent_model is None:
            changed = git_utils.commit_if_changes(
                *commit_args,
                allowed_paths=allowed_paths,
                timeout=job.timeout_s,
            )
        else:
            changed = git_utils.commit_if_changes(
                *commit_args,
                allowed_paths=allowed_paths,
                timeout=job.timeout_s,
                agent_model=str(agent_model),
            )
        branch = str(job.kwargs.get("branch") or "")
        if not changed:
            publish_state = self._commit_push_requires_publish(
                job=job,
                branch=branch,
                worktree_path=Path(worktree_path),
            )
            if isinstance(publish_state, JobResult):
                return publish_state
            if not publish_state:
                clean_head = self._read_publish_head(Path(worktree_path), timeout=job.timeout_s)
                if isinstance(clean_head, JobResult):
                    return clean_head
                return JobResult(
                    ok=True,
                    value={"pushed": False, "head_sha": clean_head},
                )
            status = git_utils.run(
                ["git", "status", "--porcelain"],
                cwd=Path(worktree_path),
                capture_output=True,
                timeout=job.timeout_s,
            )
            if status.stdout.strip():
                return JobResult(ok=False, error="commit_push left uncommitted changes")
        scope_retraction = self._verify_scope_retraction(job, Path(worktree_path))
        if scope_retraction is not None:
            return scope_retraction
        return self._publish_commit_push(job, branch, Path(worktree_path))

    @staticmethod
    def _verify_scope_retraction(job: GitJob, worktree_path: Path) -> JobResult | None:
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
            result = git_utils.run(
                [
                    "git",
                    "--literal-pathspecs",
                    "diff",
                    "--name-only",
                    base_sha,
                    "HEAD",
                    "--",
                    *paths,
                ],
                cwd=worktree_path,
                capture_output=True,
                timeout=job.timeout_s,
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

    def _publish_commit_push(self, job: GitJob, branch: str, worktree_path: Path) -> JobResult:
        """Publish a newly created commit and return its exact immutable SHA."""
        branch = branch or "HEAD"
        expected_remote_sha = job.kwargs.get("expected_remote_sha")
        if expected_remote_sha is not None and not _is_full_commit_sha(expected_remote_sha):
            return JobResult(ok=False, error="direct scope base pin invalid")
        source_sha = self._read_publish_head(worktree_path, timeout=job.timeout_s)
        if isinstance(source_sha, JobResult):
            return source_sha
        if isinstance(expected_remote_sha, str):
            git_utils.push_branch_if_remote_matches(
                branch,
                expected_remote_sha,
                worktree_path,
                timeout=job.timeout_s,
            )
        else:
            git_utils.push_branch(branch, worktree_path, timeout=job.timeout_s)
        return JobResult(ok=True, value={"pushed": True, "head_sha": source_sha})

    @staticmethod
    def _read_publish_head(worktree_path: Path, *, timeout: int) -> str | JobResult:
        """Read the immutable commit the implementation writer will publish."""
        try:
            head = git_utils.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree_path,
                capture_output=True,
                timeout=timeout,
            ).stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return JobResult(ok=False, error="cannot bind implementation publish head")
        if not _is_full_commit_sha(head):
            return JobResult(ok=False, error="cannot bind implementation publish head")
        return head

    @staticmethod
    def _read_remote_branch_head(
        worktree_path: Path,
        *,
        remote: str,
        branch: str,
        timeout: int,
    ) -> str | JobResult:
        """Read one exact remote branch head without updating local refs."""
        expected_ref = f"refs/heads/{branch}"
        try:
            fields = git_utils.run(
                ["git", "ls-remote", "--refs", remote, expected_ref],
                cwd=worktree_path,
                timeout=timeout,
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

    @staticmethod
    def _commit_push_requires_publish(
        *, job: GitJob, branch: str, worktree_path: Path
    ) -> bool | JobResult:
        """Return whether a clean worktree still needs coordinator-owned publication."""
        expected_remote_sha = job.kwargs.get("expected_remote_sha")
        if expected_remote_sha is None:
            return bool(
                branch
                and git_utils.has_unpushed_commits(
                    branch,
                    worktree_path,
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
        released = git_utils.delete_reserved_branch_if_unchanged(
            branch,
            expected_remote_sha,
            worktree_path,
            timeout=job.timeout_s,
        )
        if not released:
            return JobResult(
                ok=False,
                error="direct scope reservation changed before it could be released",
            )
        return False
