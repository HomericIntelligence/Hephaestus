#!/usr/bin/env python3
r"""Kernel pipe-mode ``core_pattern`` handler for capturing core dumps.

When a process that runs inside a container (Podman/Docker) crashes, a plain
file-path ``core_pattern`` is resolved against the *container's* mount
namespace, so the kernel silently writes the core somewhere the host cannot
see. A pipe handler runs in the *host* PID/mount namespace: the kernel pipes
the full ELF core to the handler's stdin and the handler writes it to a
host-side directory that survives container teardown.

This module is the Python core component behind the
``hephaestus-coredump-handler`` console script. Install it as the kernel's
core pattern handler::

    HANDLER=$(command -v hephaestus-coredump-handler)
    echo "|${HANDLER} %p %e %t %s %P" | sudo tee /proc/sys/kernel/core_pattern

The kernel invokes a pipe handler with a *minimal environment*, so the
``COREDUMP_TARGET_DIRS`` env var cannot reach it. When a specific output
directory is required (e.g. a CI workspace path that a container bind-mount
maps to), pass it as a literal ``--target-dir`` argument in the
``core_pattern`` line — the kernel forwards literal args verbatim::

    echo "|${HANDLER} --target-dir /workspace/crash-bundle/cores %p %e %t %s %P" \\
      | sudo tee /proc/sys/kernel/core_pattern

``core_pattern`` format tokens (order matters, must match the install line):

============  ===================================================
``%p``        PID of the crashing process (in its own PID namespace)
``%e``        executable basename
``%t``        time of the crash (seconds since epoch)
``%s``        signal number
``%P``        global PID (host namespace) — captured, unused in the filename
============  ===================================================

Exit-code note: the kernel ignores this handler's exit code entirely. Every
failure path therefore logs to ``handler.log`` next to the core directory
instead of relying on exit status — a missing ``handler.log`` is the only
signal that the handler did not run at all.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

#: Default candidate output directories, tried in order. The first that
#: already exists wins; if none exist, the last is created. A CI job
#: typically overrides this to point at its workspace.
DEFAULT_TARGET_DIRS: tuple[str, ...] = ("/tmp/crash-bundle/cores",)

#: Default cap on the core size written to disk (4 GiB). Prevents a runaway
#: process from filling the host disk.
DEFAULT_MAX_BYTES: int = 4 * 1024 * 1024 * 1024


def resolve_target_dir(candidates: list[str]) -> Path:
    """Resolve the directory the core ELF will be written to.

    Walks ``candidates`` in order; the first that already exists wins. If
    none exist, the last candidate is selected and created. Running as host
    PID 1 means well-known host paths (e.g. a CI workspace) are reachable.

    Args:
        candidates: Ordered list of candidate directory paths. Empty entries
            are skipped.

    Returns:
        The resolved target directory, created if it did not exist.

    Raises:
        ValueError: If ``candidates`` contains no non-empty entries.

    """
    cleaned = [c for c in candidates if c]
    if not cleaned:
        raise ValueError("no candidate target directories provided")

    target = next((Path(c) for c in cleaned if Path(c).is_dir()), Path(cleaned[-1]))
    target.mkdir(parents=True, exist_ok=True)
    return target


def _log(log_dir: Path, message: str) -> None:
    """Append a timestamped line to ``handler.log``, swallowing all errors.

    The kernel gives a pipe handler no channel to surface an exit code, so a
    logging failure here cannot be propagated. It is silent by design; a
    downstream artifact-upload step should treat a missing ``handler.log`` as
    "the handler never ran".

    Args:
        log_dir: Directory the ``handler.log`` file lives in.
        message: The line to append (a timestamp is prepended).

    """
    stamp = datetime.now(timezone.utc).isoformat()
    try:
        with open(log_dir / "handler.log", "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        # No safe way to surface this — see the function docstring.
        pass


def write_core(
    stream: object,
    pid: str,
    exe: str,
    crash_time: str,
    signal: str,
    target_dir: Path,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Path:
    """Write the core ELF piped on ``stream`` to a file under ``target_dir``.

    Args:
        stream: A binary file-like object the kernel pipes the core ELF on
            (typically ``sys.stdin.buffer``).
        pid: PID of the crashing process (``%p``).
        exe: Executable basename (``%e``).
        crash_time: Crash time, seconds since epoch (``%t``).
        signal: Signal number (``%s``).
        target_dir: Directory to write the core file into.
        max_bytes: Maximum number of bytes to write. Excess input is
            discarded so a runaway process cannot fill the disk.

    Returns:
        The path the core file was written to.

    """
    out = target_dir / f"core.{pid}.{exe}.{crash_time}.sig{signal}"
    log_dir = target_dir.parent

    written = 0
    chunk_size = 1024 * 1024
    try:
        with open(out, "wb") as core_file:
            while written < max_bytes:
                chunk = stream.read(min(chunk_size, max_bytes - written))  # type: ignore[attr-defined]
                if not chunk:
                    break
                core_file.write(chunk)
                written += len(chunk)
    except OSError as exc:
        _log(log_dir, f"ERROR: failed to write core to {out}: {exc}")
        return out

    try:
        out.chmod(0o644)
    except OSError as exc:
        _log(log_dir, f"WARNING: chmod 644 {out} failed ({exc}); file may be unreadable")

    _log(log_dir, f"wrote {out} ({written} bytes) signal={signal} exe={exe}")
    return out


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``hephaestus-coredump-handler`` CLI."""
    parser = argparse.ArgumentParser(
        prog="hephaestus-coredump-handler",
        description=(
            "Kernel pipe-mode core_pattern handler. Invoked by the Linux kernel "
            "with the core ELF on stdin; not meant to be run interactively."
        ),
    )
    parser.add_argument(
        "--target-dir",
        default=None,
        help=(
            "explicit output directory for the core file. Takes precedence over "
            "COREDUMP_TARGET_DIRS and the built-in default. The kernel invokes a "
            "core_pattern pipe handler with a minimal environment, so an env var "
            "cannot reach it — pass this literal flag in the core_pattern line "
            "when a specific directory (e.g. a CI workspace path) is required."
        ),
    )
    parser.add_argument("pid", help="PID of the crashing process (%%p)")
    parser.add_argument("exe", help="executable basename (%%e)")
    parser.add_argument("crash_time", help="crash time, seconds since epoch (%%t)")
    parser.add_argument("signal", help="signal number (%%s)")
    parser.add_argument(
        "global_pid",
        nargs="?",
        default="",
        help="global (host-namespace) PID (%%P) — captured, unused in the filename",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``hephaestus-coredump-handler`` console script.

    Configuration precedence for the output directory:

    1. ``--target-dir`` CLI option (highest — survives the kernel's minimal
       handler environment).
    2. ``COREDUMP_TARGET_DIRS`` env var — colon-separated candidate dirs.
    3. :data:`DEFAULT_TARGET_DIRS` — the built-in fallback.

    ``COREDUMP_MAX_BYTES`` (env var) caps the core size in bytes.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code. Note the kernel ignores this when the handler is
        invoked via ``core_pattern``; it is meaningful only for the TTY-guard
        rejection path and for tests.

    """
    args = _build_parser().parse_args(argv)

    # TTY guard: when invoked by the kernel, stdin is a pipe carrying the core
    # ELF. When invoked manually on a terminal with no piped input, reading
    # stdin would hang forever — refuse early.
    if sys.stdin.isatty():
        print(
            "hephaestus-coredump-handler: stdin is a TTY — refusing to run "
            "(would block). This is a kernel core_pattern handler; test it with "
            "`printf 'fake' | hephaestus-coredump-handler <pid> <exe> <time> <sig>`.",
            file=sys.stderr,
        )
        return 1

    # --target-dir wins over the env var, which wins over the built-in default.
    if args.target_dir:
        candidates = [args.target_dir]
    else:
        target_dirs = os.environ.get("COREDUMP_TARGET_DIRS")
        candidates = target_dirs.split(":") if target_dirs else list(DEFAULT_TARGET_DIRS)

    max_bytes_env = os.environ.get("COREDUMP_MAX_BYTES")
    max_bytes = int(max_bytes_env) if max_bytes_env else DEFAULT_MAX_BYTES

    target_dir = resolve_target_dir(candidates)
    write_core(
        sys.stdin.buffer,
        pid=args.pid,
        exe=args.exe,
        crash_time=args.crash_time,
        signal=args.signal,
        target_dir=target_dir,
        max_bytes=max_bytes,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
