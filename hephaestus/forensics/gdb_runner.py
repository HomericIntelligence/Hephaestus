#!/usr/bin/env python3
r"""Run any command under ``gdb`` to capture crashes a runtime would swallow.

Some runtimes — JIT compilers, sanitizer runtimes, language VMs — install
their own ``SIGABRT``/``SIGSEGV``/``SIGILL`` handlers that catch a fatal
signal, print a terse post-handler trace, and exit cleanly. The kernel never
produces a core and the *real* faulting frame is lost.

Running the command under ``gdb -batch`` stops the inferior in the debugger
*before* its own handler runs, so a real ELF core and a full backtrace can be
captured at the moment of fault. This module is the Python core component
behind the ``hephaestus-run-under-gdb`` console script.

Usage::

    hephaestus-run-under-gdb <core-dir> <command> [args...]

Execution is bounded to 7,200 seconds by default. ``--timeout`` must appear
before ``<core-dir>`` and accepts values from 1 through 86,400 seconds. A
timeout terminates the dedicated POSIX process group, or the direct child when
process groups are unavailable, and returns exit code 124 after bounded cleanup.
If the bounded reap also times out, the runner makes one final nonblocking reap
attempt and surfaces a cleanup failure unless termination is confirmed.

Environment variables:

* ``RUN_UNDER_GDB=0`` — skip gdb entirely and exec the command directly
  (local-dev escape hatch; gdb adds overhead).
* ``GDB_CMD_PREFIX`` — optional command prefix inserted before ``gdb``, e.g.
  ``"uv run --"``. Parsed with ``shlex.split`` so values containing
  shell-quoted whitespace (e.g. ``"'/path with space/uv' run --"``) are
  tokenized correctly.

Security:

* ``GDB_CMD_PREFIX`` is an intentional escape hatch and its tokens are spliced
  into the ``gdb`` argv, so unvalidated input would allow argv injection (e.g.
  ``--init-eval-command``). The value is tokenized with ``shlex.split`` (so
  shell-quoted whitespace is honored) and each resulting token MUST either be
  the bare argv terminator ``--`` or fully match ``[A-Za-z0-9_./:=,@+~ \\-]+``
  (a literal space is allowed only because it can arise solely from a quoted
  token, which is spliced as a single argv element) AND NOT start with ``-``.
  Anything else — including unbalanced quotes — is rejected: the library entry point
  raises ``ValueError`` and the CLI prints ``[run-under-gdb] ERROR: …`` to
  stderr and exits with code ``2``. The supported shape is a sub-runner like
  ``"uv run --"`` that ends in its own argument terminator.

Exit code:

* ``0`` / ``N`` — normal exit with the inferior's own exit code ``N``.
* ``128 + signo`` — the inferior was stopped by a caught signal
  (134 = SIGABRT, 139 = SIGSEGV, 132 = SIGILL, ...).
* ``124`` — gdb or direct command execution exceeded its timeout.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from hephaestus.cli.utils import add_json_arg, add_version_arg, emit_json_status

# A literal space is permitted because tokens are produced by ``shlex.split``
# and spliced into ``subprocess.run`` WITHOUT a shell: an embedded space can
# only originate from shell-quoting (e.g. a quoted path), so the token is a
# single, safe argv element rather than a word boundary an attacker controls.
_GDB_PREFIX_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:=,@+~ \-]+")

_EXECUTION_TIMEOUT_SECONDS = 7_200
_EXECUTION_TIMEOUT_MAX_SECONDS = 86_400
_PROCESS_REAP_TIMEOUT_SECONDS = 5
_TIMEOUT_EXIT_CODE = 124
_TIMEOUT_ERROR = "[run-under-gdb] ERROR: command timed out"
_TIMEOUT_JSON_MESSAGE = "command timed out"
_PROCESS_GROUPS_SUPPORTED = (
    os.name == "posix"
    and hasattr(os, "setpgid")
    and hasattr(os, "killpg")
    and hasattr(signal, "SIGKILL")
)


def _validate_execution_timeout(timeout: int) -> int:
    """Validate a gdb or direct-command execution timeout."""
    if not 1 <= timeout <= _EXECUTION_TIMEOUT_MAX_SECONDS:
        raise ValueError(f"timeout must be between 1 and {_EXECUTION_TIMEOUT_MAX_SECONDS} seconds")
    return timeout


def _parse_execution_timeout(raw: str) -> int:
    """Parse and validate the CLI execution timeout."""
    try:
        timeout = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be an integer") from exc
    try:
        return _validate_execution_timeout(timeout)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _read_process_group(path: Path | None) -> int | None:
    """Return a positive process-group ID recorded by the child, if present."""
    if path is None:
        return None
    try:
        process_group = int(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return None
    return process_group if process_group > 0 else None


def _unlink_best_effort(*paths: Path) -> None:
    """Remove temporary artifacts without masking the execution outcome."""
    for path in paths:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def _terminate_process(
    process: subprocess.Popen[bytes],
    *,
    additional_process_group_file: Path | None = None,
) -> None:
    """Kill a POSIX process group or fall back to the direct child."""
    if _PROCESS_GROUPS_SUPPORTED:
        additional_process_group = _read_process_group(additional_process_group_file)
        if additional_process_group is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(additional_process_group, signal.SIGKILL)
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            pass
    with contextlib.suppress(ProcessLookupError):
        process.kill()


def _terminate_and_reap(
    process: subprocess.Popen[bytes],
    *,
    additional_process_group_file: Path | None = None,
) -> None:
    """Terminate owned process groups and confirm the direct child was reaped."""
    _terminate_process(
        process,
        additional_process_group_file=additional_process_group_file,
    )
    try:
        process.wait(timeout=_PROCESS_REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _terminate_process(
            process,
            additional_process_group_file=additional_process_group_file,
        )
        # A final zero-wait reap records an exit which raced the bounded
        # cleanup without extending timeout handling.
        try:
            process.wait(timeout=0)
        except subprocess.TimeoutExpired as final_timeout:
            raise RuntimeError(
                "process termination was not confirmed within the cleanup timeout"
            ) from final_timeout


def _run_bounded(
    command: list[str],
    timeout: int,
    *,
    additional_process_group_file: Path | None = None,
) -> int:
    """Run a command with inherited streams and bounded timeout cleanup."""
    process = subprocess.Popen(
        command,
        # A new process group gives cleanup an owned signal target while
        # preserving the caller's session and controlling terminal.
        start_new_session=False,
        process_group=0 if _PROCESS_GROUPS_SUPPORTED else None,
    )
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_and_reap(
            process,
            additional_process_group_file=additional_process_group_file,
        )
        raise exc
    except KeyboardInterrupt:
        _terminate_and_reap(
            process,
            additional_process_group_file=additional_process_group_file,
        )
        raise


def _validate_gdb_cmd_prefix(raw: str | None) -> list[str]:
    """Whitelist-validate ``GDB_CMD_PREFIX`` into argv tokens.

    Returns ``[]`` for ``None``/empty/whitespace-only input. Otherwise
    tokenizes with :func:`shlex.split` so values containing shell-quoted
    whitespace (e.g. ``"'/path with space/uv' run --"``) are split
    correctly; each token must either be the bare argv terminator ``--``
    or fully match the safe character class AND not begin with ``-`` (which
    would let a caller inject gdb flags such as ``--init-eval-command``).

    Args:
        raw: The raw env-var / kwarg value.

    Returns:
        The validated argv tokens, ready to splice before ``gdb``.

    Raises:
        ValueError: If the value cannot be tokenized (e.g. an unbalanced
            quote), if any token contains disallowed characters, or if a token
            is a ``-``-leading flag-like token other than the bare ``--``. The
            message is prefixed with ``GDB_CMD_PREFIX``.

    """
    if not raw or not raw.strip():
        return []
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        raise ValueError(
            f"GDB_CMD_PREFIX value {raw!r} is not valid shell syntax "
            f"({exc}); refusing to splice into gdb argv"
        ) from exc
    for tok in tokens:
        if tok == "--":
            continue
        if tok.startswith("-"):
            raise ValueError(
                f"GDB_CMD_PREFIX token {tok!r} is a flag-like token; only the "
                "bare argv terminator '--' is permitted among '-'-leading "
                "tokens, to prevent gdb argv injection"
            )
        if not _GDB_PREFIX_TOKEN_RE.fullmatch(tok):
            raise ValueError(
                f"GDB_CMD_PREFIX token {tok!r} contains characters outside the "
                r"allowed set [A-Za-z0-9_./:=,@+~ \-]; refusing to splice into "
                "gdb argv"
            )
    return tokens


#: gdb batch script template. Uses Python event hooks rather than a plain
#: gdb-script ``if`` because ``handle SIG* stop`` + a ``hook-stop`` block
#: fires on *every* stop event (including normal exit, where
#: ``generate-core-file`` then fails), and ``--return-child-result`` is
#: unreliable in batch mode for processes killed by *handled* signals.
#: The Python hooks cleanly distinguish a real crash (``gdb.SignalEvent``)
#: from a clean exit (``gdb.ExitedEvent``) and record the intended exit code
#: to ``{exit_file}`` for the wrapper to read back.
_GDB_SCRIPT_TEMPLATE = """\
set pagination off
set confirm off
set logging file {gdb_log}
set logging overwrite on
set logging on

python
import gdb
import os
EXIT_FILE = {exit_file!r}
CORE_FILE = {core_file!r}
PROCESS_GROUP_FILE = {process_group_file!r}
# POSIX shell convention for signal-terminated processes (128 + signo).
SIG_MAP = {{"SIGABRT": 6, "SIGSEGV": 11, "SIGBUS": 7, "SIGFPE": 8, "SIGILL": 4}}
state = {{"signaled": False}}

def write_exit(code):
    with open(EXIT_FILE, "w") as f:
        f.write(str(code))

def record_process_group(event):
    # GDB normally puts the inferior in a process group distinct from its own.
    # Persist that group for the wrapper so timeout cleanup owns both trees.
    if PROCESS_GROUP_FILE is None:
        return
    pid = gdb.selected_inferior().pid
    if pid:
        temporary_file = PROCESS_GROUP_FILE + ".tmp"
        with open(temporary_file, "w") as f:
            f.write(str(os.getpgid(pid)))
        os.replace(temporary_file, PROCESS_GROUP_FILE)

# Default = 1 covers the case where neither handler fires (e.g. gdb dies).
write_exit(1)

def on_stop(event):
    # We never set breakpoints, so the only stops we expect are signals.
    if isinstance(event, gdb.SignalEvent):
        signo = event.stop_signal
        print("[run-under-gdb] caught " + signo + "; dumping " + CORE_FILE)
        gdb.execute("generate-core-file " + CORE_FILE)
        gdb.execute("bt full")
        gdb.execute("info threads")
        gdb.execute("info sharedlibrary")
        state["signaled"] = True
        write_exit(128 + SIG_MAP.get(signo, 1))

def on_exit(event):
    # gdb fires Exited after the post-signal kill too. If we already
    # captured a signal, do not overwrite that exit code.
    if state["signaled"]:
        return
    code = getattr(event, "exit_code", None)
    write_exit(code if code is not None else 0)

gdb.events.stop.connect(on_stop)
gdb.events.exited.connect(on_exit)
gdb.events.new_objfile.connect(record_process_group)
gdb.events.cont.connect(record_process_group)
end

# Intercept crash signals before the inferior's own handlers run.
# "nopass" prevents the signal from being delivered to the inferior.
handle SIGABRT stop nopass print
handle SIGSEGV stop nopass print
handle SIGBUS  stop nopass print
handle SIGILL  stop nopass print
handle SIGFPE  stop nopass print

run

set logging off
quit
"""


def resolve_command(command: str) -> str | None:
    """Resolve a command to a concrete executable path.

    An absolute or relative path that is executable is returned as-is; a bare
    name is resolved via ``PATH``.

    Args:
        command: The command to resolve.

    Returns:
        The resolved executable path, or ``None`` if it cannot be resolved.

    """
    candidate = Path(command)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which(command)


def build_gdb_script(
    gdb_log: str,
    core_file: str,
    exit_file: str,
    process_group_file: str | None = None,
) -> str:
    """Render the gdb batch script.

    Args:
        gdb_log: Path gdb writes its logging output to.
        core_file: Path ``generate-core-file`` writes the ELF core to on crash.
        exit_file: Path the Python hook records the intended exit code to.
        process_group_file: Optional path where the hook records the inferior's
            process-group ID for bounded wrapper cleanup.

    Returns:
        The rendered gdb script text.

    """
    return _GDB_SCRIPT_TEMPLATE.format(
        gdb_log=gdb_log,
        core_file=core_file,
        exit_file=exit_file,
        process_group_file=process_group_file,
    )


def run_under_gdb(
    core_dir: str,
    command: str,
    command_args: list[str],
    gdb_cmd_prefix: str | None = None,
    *,
    timeout: int = _EXECUTION_TIMEOUT_SECONDS,
) -> int:
    """Run ``command`` under ``gdb -batch`` and return the inferior's exit code.

    Args:
        core_dir: Directory for cores and gdb logs (created if absent).
        command: The program to run. Resolved via ``PATH`` if not a path.
        command_args: Arguments passed to ``command`` verbatim.
        gdb_cmd_prefix: Optional command prefix inserted before ``gdb`` (e.g.
            ``"uv run --"``) so gdb and its inferior inherit an activated
            environment. Tokenized with :func:`shlex.split` to honor shell
            quoting rules, so paths with embedded whitespace can be quoted.
            Each resulting token must either be the bare ``--`` terminator or
            match ``[A-Za-z0-9_./:=,@+~ -]+`` and not start with ``-``;
            otherwise a ``ValueError`` is raised (see module ``Security:``
            note).
        timeout: Maximum execution time in seconds, from 1 through 86,400.

    Returns:
        ``0``/``N`` for a normal exit with code ``N``; ``128 + signo`` if the
        inferior was stopped by a caught signal; ``127`` if ``command`` cannot
        be resolved on ``PATH`` (the POSIX "command not found" convention).

    Raises:
        ValueError: If ``gdb_cmd_prefix`` (or the ``GDB_CMD_PREFIX`` env var
            that feeds it) contains an unsafe token. See the module
            ``Security:`` note for the whitelist.

    """
    timeout = _validate_execution_timeout(timeout)
    prefix = _validate_gdb_cmd_prefix(gdb_cmd_prefix)  # fail fast

    core_path = Path(core_dir)
    core_path.mkdir(parents=True, exist_ok=True)

    command_bin = resolve_command(command)
    if command_bin is None:
        print(
            f"[run-under-gdb] ERROR: could not resolve command '{command}' on PATH",
            file=sys.stderr,
        )
        return 127

    timestamp = str(int(time.time()))
    gdb_log = str(core_path / f"gdb-{timestamp}.log")
    core_file = str(core_path / f"core.gdb.{timestamp}")
    exit_file = core_path / f"exit-{timestamp}.code"
    process_group_file = core_path / f"inferior-pgid-{timestamp}.code"

    script_text = build_gdb_script(
        gdb_log,
        core_file,
        str(exit_file),
        str(process_group_file),
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".gdb", prefix="run-under-gdb-", delete=False, encoding="utf-8"
    ) as script_handle:
        script_handle.write(script_text)
        gdb_script = script_handle.name

    try:
        gdb_cmd = [
            *prefix,
            "gdb",
            "-batch",
            "-nx",
            "-x",
            gdb_script,
            "--args",
            command_bin,
            *command_args,
        ]
        gdb_status = _run_bounded(
            gdb_cmd,
            timeout,
            additional_process_group_file=process_group_file,
        )

        print(f"[run-under-gdb] gdb log  : {gdb_log}", file=sys.stderr)
        print(f"[run-under-gdb] core file: {core_file} (written on crash)", file=sys.stderr)
        print(f"[run-under-gdb] binary   : {command_bin}", file=sys.stderr)
        print(f"[run-under-gdb] args     : {' '.join(command_args)}", file=sys.stderr)

        # Prefer the Python-recorded exit code; fall back to gdb's own status
        # if the file is missing (gdb died before the hook fired).
        if exit_file.is_file():
            recorded = exit_file.read_text(encoding="utf-8").strip()
            try:
                return int(recorded)
            except ValueError:
                return gdb_status
        return gdb_status
    finally:
        _unlink_best_effort(
            exit_file,
            process_group_file,
            Path(f"{process_group_file}.tmp"),
            Path(gdb_script),
        )


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``hephaestus-run-under-gdb`` CLI."""
    parser = argparse.ArgumentParser(
        prog="hephaestus-run-under-gdb",
        description=(
            "Run a command under gdb -batch so a real ELF core and backtrace "
            "are captured before the inferior's own signal handler runs."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=_parse_execution_timeout,
        default=_EXECUTION_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=(
            "Execution timeout in seconds, from 1 through 86400 "
            f"(default: {_EXECUTION_TIMEOUT_SECONDS}); place before <core-dir>"
        ),
    )
    parser.add_argument(
        "core_dir",
        help="directory for cores and gdb logs (created if absent)",
    )
    parser.add_argument(
        "command",
        help="the program to run (resolved via PATH if not an explicit path)",
    )
    parser.add_argument(
        "command_args",
        nargs=argparse.REMAINDER,
        help="arguments passed to the command verbatim",
    )
    add_json_arg(parser)
    add_version_arg(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``hephaestus-run-under-gdb`` console script.

    Reads two environment variables:

    * ``RUN_UNDER_GDB=0`` — bypass gdb and exec the command directly.
    * ``GDB_CMD_PREFIX`` — optional prefix inserted before ``gdb``; parsed with
      ``shlex.split`` so shell-quoted whitespace in paths is preserved.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        The inferior's exit code (see :func:`run_under_gdb`).

    """
    args = _build_parser().parse_args(argv)

    try:
        # Escape hatch: RUN_UNDER_GDB=0 bypasses gdb for local dev.
        if os.environ.get("RUN_UNDER_GDB") == "0":
            rc = _run_bounded([args.command, *args.command_args], args.timeout)
            json_message = "ran command directly (RUN_UNDER_GDB=0)"
        else:
            try:
                rc = run_under_gdb(
                    core_dir=args.core_dir,
                    command=args.command,
                    command_args=args.command_args,
                    gdb_cmd_prefix=os.environ.get("GDB_CMD_PREFIX"),
                    timeout=args.timeout,
                )
            except ValueError as exc:
                print(f"[run-under-gdb] ERROR: {exc}", file=sys.stderr)
                if args.json:
                    emit_json_status(2, message=f"invalid GDB_CMD_PREFIX: {exc}")
                return 2
            json_message = "ran command under gdb"
    except subprocess.TimeoutExpired:
        print(_TIMEOUT_ERROR, file=sys.stderr)
        if args.json:
            emit_json_status(_TIMEOUT_EXIT_CODE, message=_TIMEOUT_JSON_MESSAGE)
        return _TIMEOUT_EXIT_CODE
    if args.json:
        emit_json_status(rc, message=json_message)
    return rc


if __name__ == "__main__":
    sys.exit(main())
