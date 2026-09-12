"""Helper functions for Hephaestus.

General utility functions that don't fit in other specific modules.
"""

import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement

from hephaestus.logging.utils import get_logger

logger = get_logger(__name__)

# Subprocess timeouts for different operation types.
# METADATA_TIMEOUT: local, non-network queries (git status, git config, uv tree)
# NETWORK_TIMEOUT: operations touching the network (gh calls, git clone/fetch/push)
# Callers can override these defaults through explicit timeout parameters.
METADATA_TIMEOUT: int = 10
NETWORK_TIMEOUT: int = 120


class SubprocessOutputLimitExceeded(subprocess.SubprocessError):
    """Report that a subprocess exceeded its combined output limit."""

    def __init__(
        self,
        cmd: list[str],
        limit: int,
        *,
        output: str,
        stderr: str,
    ) -> None:
        """Keep bounded partial output with the specific failure cause."""
        super().__init__(f"subprocess output limit exceeded ({limit} bytes)")
        self.cmd = cmd
        self.limit = limit
        self.output = output
        self.stdout = output
        self.stderr = stderr


def slugify(text: str) -> str:
    """Convert text to a URL-friendly slug.

    Args:
        text: Text to convert to slug

    Returns:
        URL-friendly slug string

    """
    # Normalize unicode characters
    text = unicodedata.normalize("NFKD", text)
    # Convert to ASCII
    text = text.encode("ascii", "ignore").decode("ascii")
    # Convert to lowercase and replace spaces/underscores/dots with hyphens
    text = re.sub(r"[\s_.]+", "-", text.lower())
    # Remove non-alphanumeric characters (except hyphens)
    text = re.sub(r"[^a-z0-9-]", "", text)
    # Remove leading/trailing hyphens
    text = text.strip("-")
    # Replace multiple consecutive hyphens with single hyphen
    text = re.sub(r"-+", "-", text)
    return text


def strip_null_bytes(text: str) -> str:
    r"""Remove NUL (``\x00``) bytes from external plain text.

    An argv element cannot contain a NUL. Agent output and malformed GitHub issue
    bodies can carry stray NULs, which can permanently strand an affected work
    item in the automation loop. Remove them at the input and data boundary.

    Args:
        text: Text that may contain embedded NUL bytes.

    Returns:
        ``text`` with every NUL byte removed; the same object when none are
        present (clean text is byte-identical).

    """
    return text.replace("\x00", "") if "\x00" in text else text


def human_readable_size(size_bytes: int | float) -> str:
    """Convert byte size to human readable format.

    Args:
        size_bytes: Size in bytes

    Returns:
        Human readable size string with appropriate unit

    """
    if size_bytes == 0:
        return "0 B"

    size_names = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)

    while size >= 1024.0 and i < len(size_names) - 1:
        size /= 1024.0
        i += 1

    return f"{size:.1f} {size_names[i]}"


def flatten_dict(d: dict[str, Any], parent_key: str = "", sep: str = ".") -> dict[str, Any]:
    """Flatten nested dictionary using dot notation for keys.

    Args:
        d: Dictionary to flatten
        parent_key: Parent key prefix
        sep: Separator for nested keys

    Returns:
        Flattened dictionary

    """
    items: list[Any] = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def get_repo_root(start_path: str | Path | None = None) -> Path:
    """Find repository root by walking up to a ``.git`` or ``pyproject.toml`` marker.

    This is the single canonical repository-root resolver for the codebase. It
    accepts an optional starting path so callers anchored to a file (e.g.
    ``Path(__file__)``) and callers relying on the current working directory can
    share one implementation. A directory is treated as the repository root if it
    contains either a ``.git`` entry (git checkout) or a ``pyproject.toml`` file
    (project marker), covering both git-based and packaging-based callers.

    Args:
        start_path: Starting path to search from. Defaults to current directory.

    Returns:
        Path to repository root if found, otherwise the resolved start path as a
        fallback.

    """
    start_path = Path.cwd().resolve() if start_path is None else Path(start_path).resolve()

    path = start_path
    while path != path.parent:  # Stop at filesystem root
        if (path / ".git").exists() or (path / "pyproject.toml").exists():
            return path
        path = path.parent

    # No marker found anywhere on the path to the filesystem root; fall back to
    # the original start path.
    return start_path


_LOG_ARG_MAX = 200
_LOG_STREAM_TAIL_MAX = 2000
_PROCESS_GROUP_TERMINATION_GRACE_SECONDS = 1.0


def _process_group_exists(pgid: int) -> bool:
    """Return true while a POSIX process group has a live member."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - owned children use the same identity
        return True
    return True


def _timeout_stream_text(value: str | bytes | None) -> str:
    """Convert partial timeout output to text."""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _stop_process_group(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Stop an owned process group and reap its direct child."""
    pgid = process.pid
    with suppress(ProcessLookupError, OSError):
        os.killpg(pgid, signal.SIGTERM)
    grace_deadline = time.monotonic() + _PROCESS_GROUP_TERMINATION_GRACE_SECONDS
    while time.monotonic() < grace_deadline:
        process.poll()
        if not _process_group_exists(pgid):
            break
        time.sleep(0.01)
    if _process_group_exists(pgid):
        with suppress(ProcessLookupError, OSError):
            os.killpg(pgid, signal.SIGKILL)
    try:
        return process.communicate(timeout=_PROCESS_GROUP_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired as error:
        stdout = _timeout_stream_text(error.output)
        stderr = _timeout_stream_text(error.stderr)
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                with suppress(OSError):
                    pipe.close()
        with suppress(ProcessLookupError, OSError):
            process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_PROCESS_GROUP_TERMINATION_GRACE_SECONDS)
        return stdout, stderr


def _format_cmd_for_log(cmd: list[str]) -> str:
    """Render *cmd* for a log line, truncating any argument longer than 200 chars.

    Defense-in-depth: large argv values (e.g. a forgotten ``--body`` with a
    multi-KB string) would otherwise dump straight into ERROR logs on
    subprocess failure. Truncation keeps each log line bounded while still
    leaving enough of each argument to identify the command.
    """
    parts: list[str] = []
    for arg in cmd:
        if len(arg) > _LOG_ARG_MAX:
            parts.append(f"{arg[:_LOG_ARG_MAX]}…({len(arg) - _LOG_ARG_MAX} more chars)")
        else:
            parts.append(arg)
    return " ".join(parts)


def _tail_for_log(value: str, limit: int = _LOG_STREAM_TAIL_MAX) -> str:
    """Return a bounded tail for command output included in error logs."""
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    return f"...({omitted} earlier chars){value[-limit:]}"


def _communicate_until_deadline(
    process: subprocess.Popen[str],
    *,
    cmd: list[str],
    input_text: str | None,
    timeout: float | None,
    deadline: float | None,
    shutdown: threading.Event | None,
) -> tuple[str, str]:
    """Wait for child output within one deadline and cancellation request."""
    pending_input = input_text
    while True:
        if shutdown is not None and shutdown.is_set():
            raise InterruptedError("subprocess cancelled")
        remaining = deadline - time.monotonic() if deadline is not None else None
        if remaining is not None and remaining <= 0:
            raise subprocess.TimeoutExpired(cmd, float(timeout or 0))
        wait_s = remaining
        if shutdown is not None:
            wait_s = min(0.1, remaining) if remaining is not None else 0.1
        try:
            stdout, stderr = process.communicate(input=pending_input, timeout=wait_s)
            if shutdown is not None and shutdown.is_set():
                raise InterruptedError("subprocess cancelled")
            return stdout, stderr
        except subprocess.TimeoutExpired:
            pending_input = None
            if shutdown is None:
                raise


def _subprocess_run_input(input_text: str | None) -> dict[str, Any]:
    """Return one valid standard-input configuration for subprocess.run."""
    if input_text is None:
        return {"stdin": subprocess.DEVNULL}
    return {"input": input_text}


def _terminate_bounded_process(
    process: subprocess.Popen[bytes],
    *,
    process_group: bool,
) -> None:
    """Stop a bounded-output child and reap its direct process."""
    if process_group:
        pgid = process.pid
        with suppress(ProcessLookupError, OSError):
            os.killpg(pgid, signal.SIGTERM)
        grace_deadline = time.monotonic() + _PROCESS_GROUP_TERMINATION_GRACE_SECONDS
        while time.monotonic() < grace_deadline:
            process.poll()
            if not _process_group_exists(pgid):
                break
            time.sleep(0.01)
        if _process_group_exists(pgid):
            with suppress(ProcessLookupError, OSError):
                os.killpg(pgid, signal.SIGKILL)
    else:
        with suppress(ProcessLookupError, OSError):
            process.terminate()
    try:
        process.wait(timeout=_PROCESS_GROUP_TERMINATION_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    if not process_group:
        with suppress(ProcessLookupError, OSError):
            process.kill()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_PROCESS_GROUP_TERMINATION_GRACE_SECONDS)


def _read_bounded_process_output(  # noqa: C901
    process: subprocess.Popen[bytes],
    *,
    cmd: list[str],
    input_text: str | None,
    timeout: float | None,
    deadline: float | None,
    shutdown: threading.Event | None,
    max_output_bytes: int,
    process_group: bool,
) -> tuple[str, str]:
    """Read both process streams without more than the configured byte count."""
    if process.stdout is None or process.stderr is None:  # pragma: no cover
        raise RuntimeError("subprocess output pipes are unavailable")
    events: queue.Queue[tuple[str, bytes | BaseException | None]] = queue.Queue(maxsize=4)
    stop = threading.Event()
    streams = {"stdout": process.stdout, "stderr": process.stderr}

    def put_event(name: str, value: bytes | BaseException | None) -> None:
        while not stop.is_set():
            try:
                events.put((name, value), timeout=0.05)
                return
            except queue.Full:
                continue

    def read_pipe(name: str) -> None:
        stream = streams[name]
        try:
            while not stop.is_set():
                chunk = os.read(stream.fileno(), min(64 * 1024, max_output_bytes + 1))
                if not chunk:
                    break
                put_event(name, chunk)
        except BaseException as exc:
            put_event(name, exc)
        finally:
            put_event(name, None)

    def write_input() -> None:
        if process.stdin is None or input_text is None:
            return
        try:
            process.stdin.write(input_text.encode())
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            with suppress(OSError):
                process.stdin.close()

    readers = tuple(
        threading.Thread(
            target=read_pipe,
            args=(name,),
            name=f"hephaestus-subprocess-{process.pid}-{name}",
            daemon=True,
        )
        for name in streams
    )
    input_writer = threading.Thread(
        target=write_input,
        name=f"hephaestus-subprocess-{process.pid}-stdin",
        daemon=True,
    )
    output = {"stdout": bytearray(), "stderr": bytearray()}
    byte_count = 0
    ended: set[str] = set()
    completed = False
    try:
        for reader in readers:
            reader.start()
        if input_text is not None:
            input_writer.start()
        while len(ended) != len(readers):
            if shutdown is not None and shutdown.is_set():
                raise InterruptedError("subprocess cancelled")
            remaining = deadline - time.monotonic() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, float(timeout or 0))
            wait_s = min(0.1, remaining) if remaining is not None else 0.1
            try:
                name, value = events.get(timeout=wait_s)
            except queue.Empty:
                continue
            if value is None:
                ended.add(name)
                continue
            if isinstance(value, BaseException):
                raise RuntimeError(f"subprocess {name} pipe read failed") from value
            remaining_bytes = max_output_bytes - byte_count
            if len(value) > remaining_bytes:
                output[name].extend(value[:remaining_bytes])
                byte_count += remaining_bytes
                raise SubprocessOutputLimitExceeded(
                    cmd,
                    max_output_bytes,
                    output=output["stdout"].decode(errors="replace"),
                    stderr=output["stderr"].decode(errors="replace"),
                )
            output[name].extend(value)
            byte_count += len(value)
        while process.poll() is None:
            if shutdown is not None and shutdown.is_set():
                raise InterruptedError("subprocess cancelled")
            remaining = deadline - time.monotonic() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, float(timeout or 0))
            time.sleep(min(0.05, remaining) if remaining is not None else 0.05)
        completed = True
    finally:
        stop.set()
        if not completed:
            _terminate_bounded_process(process, process_group=process_group)
        for stream in streams.values():
            with suppress(OSError):
                stream.close()
        for reader in readers:
            reader.join(timeout=1.0)
        if input_writer.is_alive():
            input_writer.join(timeout=1.0)
    return (
        output["stdout"].decode(errors="replace"),
        output["stderr"].decode(errors="replace"),
    )


def _run_output_bounded_process(
    cmd: list[str],
    *,
    cwd: str | Path | None,
    timeout: float | None,
    check: bool,
    env: dict[str, str],
    input_text: str | None,
    shutdown: threading.Event | None,
    remaining_timeout: Callable[[], int | float] | None,
    max_output_bytes: int,
    process_group: bool,
) -> subprocess.CompletedProcess[str]:
    """Run one process with a combined standard-output byte limit."""
    from hephaestus.utils import subprocess_registry

    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL if input_text is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=process_group,
    )
    with subprocess_registry.track_process_group(process.pid):
        if remaining_timeout is not None:
            try:
                operation_timeout = remaining_timeout()
            except BaseException:
                _terminate_bounded_process(process, process_group=process_group)
                raise
            timeout = operation_timeout if timeout is None else min(timeout, operation_timeout)
        deadline = time.monotonic() + timeout if timeout is not None else None
        stdout, stderr = _read_bounded_process_output(
            process,
            cmd=cmd,
            input_text=input_text,
            timeout=timeout,
            deadline=deadline,
            shutdown=shutdown,
            max_output_bytes=max_output_bytes,
            process_group=process_group,
        )
    result = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode,
            cmd,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result


def _run_tracked_process_group(
    cmd: list[str],
    *,
    cwd: str | Path | None,
    timeout: float | None,
    check: bool,
    env: dict[str, str],
    input_text: str | None = None,
    shutdown: threading.Event | None = None,
    remaining_timeout: Callable[[], int | float] | None = None,
    max_output_bytes: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one command and stop its process group on timeout or cancellation."""
    from hephaestus.utils import subprocess_registry

    group_supported = subprocess_registry.supported()
    if (
        not group_supported
        and shutdown is None
        and remaining_timeout is None
        and max_output_bytes is None
    ):
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
            env=env,
            **_subprocess_run_input(input_text),
        )

    if shutdown is not None and shutdown.is_set():
        raise InterruptedError("subprocess cancelled before start")
    if remaining_timeout is not None:
        operation_timeout = remaining_timeout()
        timeout = operation_timeout if timeout is None else min(timeout, operation_timeout)
    if max_output_bytes is not None:
        return _run_output_bounded_process(
            cmd,
            cwd=cwd,
            timeout=timeout,
            check=check,
            env=env,
            input_text=input_text,
            shutdown=shutdown,
            remaining_timeout=remaining_timeout,
            max_output_bytes=max_output_bytes,
            process_group=group_supported,
        )
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL if input_text is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=group_supported,
    )
    with subprocess_registry.track_process_group(process.pid):
        try:
            if remaining_timeout is not None:
                operation_timeout = remaining_timeout()
                timeout = operation_timeout if timeout is None else min(timeout, operation_timeout)
            deadline = time.monotonic() + timeout if timeout is not None else None
            stdout, stderr = _communicate_until_deadline(
                process,
                cmd=cmd,
                input_text=input_text,
                timeout=timeout,
                deadline=deadline,
                shutdown=shutdown,
            )
        except subprocess.TimeoutExpired:
            if group_supported:
                stdout, stderr = _stop_process_group(process)
            else:
                process.kill()
                stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(
                cmd,
                float(timeout or 0),
                output=stdout,
                stderr=stderr,
            ) from None
        except BaseException:
            if group_supported:
                _stop_process_group(process)
            else:
                process.kill()
                process.communicate()
            raise
    result = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode,
            cmd,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result


def run_subprocess(
    cmd: list[str],
    *,
    env: dict[str, str],
    cwd: str | Path | None = None,
    timeout: float | None = None,
    check: bool = True,
    dry_run: bool = False,
    log_on_error: bool = True,
    track_process_group: bool = False,
    shutdown: threading.Event | None = None,
    input_text: str | None = None,
    remaining_timeout: Callable[[], int | float] | None = None,
    max_output_bytes: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run subprocess command with proper error handling.

    Args:
        cmd: Command and arguments as list
        cwd: Working directory for command execution
        timeout: Optional timeout in seconds
        check: Whether to raise on non-zero exit code
        dry_run: If True, log the command but do not execute it
        log_on_error: If False, suppress ERROR logging when the command fails.
            Use when failure is expected and already handled by the caller.
        env: Exact environment dict replacing the current process environment.
        track_process_group: Run the child in a tracked POSIX process group so
            an owning host can stop active work during forced shutdown.
        shutdown: Optional cancellation event. Stop the child when it is set.
        input_text: Optional text to send through the child's standard input.
        remaining_timeout: Optional operation budget callback. It is checked
            directly before and after tracked process creation.
        max_output_bytes: Optional combined standard-output byte limit.

    Returns:
        Completed process object

    Raises:
        subprocess.CalledProcessError: If command fails and check=True

    """
    if max_output_bytes is not None and (
        type(max_output_bytes) is not int or max_output_bytes <= 0
    ):
        raise ValueError("max_output_bytes must be a positive integer")
    if dry_run:
        logger.info("[DRY-RUN] $ %s", " ".join(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    # Inject correlation ID into subprocess environment if set.
    # Function-local import to keep module import graph clean.
    effective_env = env.copy()
    from hephaestus.logging.utils import get_current_correlation_id

    cid = get_current_correlation_id()
    if cid:
        effective_env["GH_TRACE_ID"] = cid

    try:
        if (
            track_process_group
            or shutdown is not None
            or remaining_timeout is not None
            or max_output_bytes is not None
        ):
            result = _run_tracked_process_group(
                cmd,
                cwd=cwd,
                timeout=timeout,
                check=check,
                env=effective_env,
                input_text=input_text,
                shutdown=shutdown,
                remaining_timeout=remaining_timeout,
                max_output_bytes=max_output_bytes,
            )
        else:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=check,
                timeout=timeout,
                env=effective_env,
                **_subprocess_run_input(input_text),
            )
        return result
    except subprocess.TimeoutExpired:
        if log_on_error:
            logger.error(
                "Command timed out after %ds: %s",
                timeout,
                _format_cmd_for_log(cmd),
            )
        raise
    except SubprocessOutputLimitExceeded:
        if log_on_error:
            logger.error(
                "Command output exceeded %d bytes: %s",
                max_output_bytes,
                _format_cmd_for_log(cmd),
            )
        raise
    except subprocess.CalledProcessError as e:
        if log_on_error:
            logger.error("Command failed: %s", _format_cmd_for_log(cmd))
            stdout = e.stdout or ""
            if stdout:
                logger.error("stdout: %s", _tail_for_log(stdout))
            stderr = e.stderr or ""
            logger.error("stderr: %s", _tail_for_log(stderr))
        raise


def install_package(package_name: str, upgrade: bool = False) -> bool:
    """Install a single Python package with pip.

    Validates the package name using the PEP 508 requirement parser from
    the ``packaging`` library. Supports extras (e.g. ``pkg[extra1,extra2]``)
    and version specifiers (e.g. ``pkg>=1.0,<2``), but rejects URL-based
    requirements for security.

    Args:
        package_name: A single PEP 508 requirement string
            (e.g. ``"requests"``, ``"pkg[extra]>=1.0"``).
        upgrade: Whether to upgrade if already installed.

    Returns:
        True if installation successful, False otherwise.

    Raises:
        ValueError: If package_name is not a valid PEP 508 requirement
            or uses a URL-based requirement.

    """
    if not package_name or not package_name.strip():
        raise ValueError(f"Invalid package requirement: {package_name!r}")

    # Validate using the canonical PEP 508 requirement parser
    try:
        req = Requirement(package_name)
    except InvalidRequirement as e:
        raise ValueError(f"Invalid package requirement: {package_name!r}") from e

    # Reject URL-based requirements for security
    if req.url is not None:
        raise ValueError(f"URL-based requirements are not supported: {package_name!r}")

    cmd = [sys.executable, "-m", "pip", "install"]
    if upgrade:
        cmd.append("--upgrade")
    cmd.append(package_name)

    try:
        from hephaestus.config.child_environments import build_python_phase_env

        run_subprocess(cmd, env=build_python_phase_env(Path.cwd()))
        logger.info("Successfully installed %s", package_name)
        return True
    except subprocess.CalledProcessError as e:
        logger.error("Failed to install %s: %s", package_name, e)
        return False
