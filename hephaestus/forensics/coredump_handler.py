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
signal that the handler did not run at all. This contract is enforced by
:func:`verify_crash_bundle` (also exposed as ``hephaestus-coredump-handler
--verify``, which exits 3 on a lost signal); a CI artifact step SHOULD run it
and fail the job when it reports ``NOT_RUN``.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from hephaestus.cli.utils import add_json_arg, add_version_arg, emit_json_status

#: Default candidate output directories, tried in order. The first that
#: already exists wins; if none exist, the last is created. A CI job
#: typically overrides this to point at its workspace.
DEFAULT_TARGET_DIRS: tuple[str, ...] = ("/tmp/crash-bundle/cores",)  # nosec B108 - intentional default for CI environments; callers may override via --target-dir

#: Default cap on the core size written to disk (4 GiB). Prevents a runaway
#: process from filling the host disk.
DEFAULT_MAX_BYTES: int = 4 * 1024 * 1024 * 1024

#: Regex for parsing ``COREDUMP_MAX_BYTES``: integer with optional IEC unit
#: suffix (K/M/G/T, case-insensitive, optional trailing 'i' and/or 'B' for
#: ``KiB``/``MB``/etc.). Whitespace allowed at either end. Bare digits and
#: ``4G``/``500M``/``2GiB`` all parse; ``-1``/``0``/``4X``/``4G500M`` do not.
_MAX_BYTES_RE = re.compile(r"^\s*(\d+)\s*([KMGT]?)i?B?\s*$", re.IGNORECASE)
_UNIT_MULTIPLIERS: dict[str, int] = {
    "": 1,
    "K": 1024,
    "M": 1024 * 1024,
    "G": 1024 * 1024 * 1024,
    "T": 1024 * 1024 * 1024 * 1024,
}


def _parse_max_bytes(raw: str) -> int | None:
    """Parse a ``COREDUMP_MAX_BYTES`` value, returning ``None`` on any failure.

    Accepts bare digits (``"4096"``) and IEC suffixes (``"4G"``, ``"500M"``,
    ``"2GiB"``, case-insensitive). All suffixes use power-of-two (IEC)
    semantics. Rejects empty/whitespace input, negatives, zero, unknown
    suffixes, and any value that ``int()`` cannot consume.

    Args:
        raw: The raw env-var string (caller has already established it is set).

    Returns:
        The parsed positive byte count, or ``None`` if the input is malformed.
        Never raises.

    """
    match = _MAX_BYTES_RE.match(raw)
    if match is None:
        return None
    digits, suffix = match.group(1), match.group(2).upper()
    try:
        value = int(digits) * _UNIT_MULTIPLIERS[suffix]
    except (ValueError, KeyError):
        return None
    return value if value > 0 else None


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
    # No safe way to surface a logging failure here — see the function docstring.
    with contextlib.suppress(OSError):
        with open(log_dir / "handler.log", "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")


#: Verdicts returned by :func:`verify_crash_bundle`. ``NOT_RUN`` is the
#: silent-loss case the handler contract guards against: a missing
#: ``handler.log`` means the kernel never invoked the handler at all.
BUNDLE_OK = "OK"
BUNDLE_RAN_WITH_ERRORS = "RAN_WITH_ERRORS"
BUNDLE_NOT_RUN = "NOT_RUN"

#: Exit code returned by ``--verify`` when the failure signal was lost
#: (verdict ``NOT_RUN``). Deliberately NOT 1 or 2: argparse exits 2 on usage
#: errors and gdb_runner already returns 2, so a distinct code lets a CI gate
#: tell "signal lost" apart from "bad command line".
VERIFY_SIGNAL_LOST_EXIT = 3


def verify_crash_bundle(log_dir: Path) -> tuple[str, str]:
    """Enforce the handler's failure-signal contract on a crash bundle.

    The kernel ignores a pipe handler's exit code, so every failure path logs
    to ``handler.log`` instead (see module docstring). This makes that
    convention executable: it inspects the bundle directory and classifies
    whether the handler ran.

    Classification keys off the lines :func:`_log` writes. A ``wrote `` line
    means the core was captured (verdict :data:`BUNDLE_OK`) even if a WARNING
    is also present, because a successful capture can still log a chmod or
    ``COREDUMP_MAX_BYTES`` warning (see :func:`write_core` / :func:`main`). Any
    other non-empty log means the handler ran but recorded no successful
    capture (:data:`BUNDLE_RAN_WITH_ERRORS`). A missing/empty/unreadable log
    means the handler never ran (:data:`BUNDLE_NOT_RUN`) — the silent-loss case.

    Args:
        log_dir: The directory ``handler.log`` is expected in (the parent of
            the core ``target_dir``; see :func:`write_core`).

    Returns:
        A ``(verdict, detail)`` pair. ``verdict`` is one of the ``BUNDLE_*``
        constants; ``detail`` is a human-readable explanation.

    """
    log_path = log_dir / "handler.log"
    if not log_path.is_file():
        return BUNDLE_NOT_RUN, (
            f"{log_path} is missing — the handler never ran "
            "(the kernel cannot report a pipe handler's exit code)"
        )
    try:
        contents = log_path.read_text(encoding="utf-8")
    except OSError as exc:
        return BUNDLE_NOT_RUN, f"{log_path} is unreadable ({exc})"

    lines = [ln for ln in contents.splitlines() if ln.strip()]
    if not lines:
        return BUNDLE_NOT_RUN, f"{log_path} is empty — no handler activity recorded"

    # A `wrote ` line is the authoritative success signal; a successful capture
    # may also carry a chmod/max-bytes WARNING, so `wrote ` wins over WARNING.
    # Each kept line is "<iso-timestamp> <message>"; split off the timestamp and
    # check that the message portion starts with "wrote " so that an ERROR: line
    # whose path happens to contain the substring " wrote " is not misclassified.
    def _message_is_wrote(ln: str) -> bool:
        parts = ln.split(maxsplit=1)
        return len(parts) == 2 and parts[1].startswith("wrote ")

    if any(_message_is_wrote(ln) for ln in lines):
        return BUNDLE_OK, f"{log_path} records a successful capture"
    return BUNDLE_RAN_WITH_ERRORS, (
        f"{log_path} records handler activity but no successful capture "
        "(handler ran; inspect the log for the failure)"
    )


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
    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "verification mode: do not read stdin. Inspect the resolved bundle "
            "directory and assert the handler-ran contract (a missing "
            "handler.log means the handler never ran). Exit 0 if the handler "
            "ran, 3 if its failure signal was lost. For CI artifact steps."
        ),
    )
    parser.add_argument("pid", nargs="?", help="PID of the crashing process (%%p)")
    parser.add_argument("exe", nargs="?", help="executable basename (%%e)")
    parser.add_argument("crash_time", nargs="?", help="crash time, seconds since epoch (%%t)")
    parser.add_argument("signal", nargs="?", help="signal number (%%s)")
    parser.add_argument(
        "global_pid",
        nargs="?",
        default="",
        help="global (host-namespace) PID (%%P) — captured, unused in the filename",
    )
    add_json_arg(parser)
    add_version_arg(parser)
    return parser


def _resolve_candidates(target_dir: str | None) -> list[str]:
    """Resolve ordered output-directory candidates from CLI/env/default.

    Mirrors the precedence documented on :func:`main`: an explicit
    ``--target-dir`` wins over ``COREDUMP_TARGET_DIRS`` (colon-separated),
    which wins over :data:`DEFAULT_TARGET_DIRS`.

    Args:
        target_dir: The ``--target-dir`` CLI value, or ``None`` if unset.

    Returns:
        The ordered list of candidate directory paths (may contain empties).

    """
    if target_dir:
        return [target_dir]
    target_dirs = os.environ.get("COREDUMP_TARGET_DIRS")
    return target_dirs.split(":") if target_dirs else list(DEFAULT_TARGET_DIRS)


def _run_verify(target_dir: str | None, as_json: bool) -> int:
    """Run ``--verify`` mode: assert the handler-ran contract, never read stdin.

    Resolves the bundle directory *read-only* — it deliberately does NOT call
    :func:`resolve_target_dir`, which would ``mkdir()`` the directory and so
    fabricate the very directory whose absence is the lost-signal indicator.

    Args:
        target_dir: The ``--target-dir`` CLI value, or ``None`` if unset.
        as_json: Whether to emit a JSON status envelope instead of plain text.

    Returns:
        ``0`` if the handler provably ran (``OK``/``RAN_WITH_ERRORS``), else
        :data:`VERIFY_SIGNAL_LOST_EXIT` (verdict ``NOT_RUN``).

    """
    cleaned = [c for c in _resolve_candidates(target_dir) if c]
    if not cleaned:
        raise ValueError("no candidate target directories provided")
    # log_dir is the parent of target_dir (see write_core).
    target = next((Path(c) for c in cleaned if Path(c).is_dir()), Path(cleaned[-1]))
    verdict, detail = verify_crash_bundle(target.parent)
    exit_code = 0 if verdict in (BUNDLE_OK, BUNDLE_RAN_WITH_ERRORS) else VERIFY_SIGNAL_LOST_EXIT
    if as_json:
        emit_json_status(exit_code, message=detail, verdict=verdict)
    else:
        print(f"{verdict}: {detail}", file=sys.stdout if exit_code == 0 else sys.stderr)
    return exit_code


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

    if args.verify:
        return _run_verify(args.target_dir, args.json)

    # Capture path: positionals are kernel-supplied. They are declared optional
    # only so --verify can omit them; a real capture invocation missing them is
    # a malformed core_pattern line and must fail loudly, not silently no-op.
    missing = [
        name for name in ("pid", "exe", "crash_time", "signal") if getattr(args, name) is None
    ]
    if missing:
        msg = f"missing required argument(s) for capture: {', '.join(missing)}"
        if args.json:
            emit_json_status(1, message=msg)
        else:
            print(f"hephaestus-coredump-handler: {msg}", file=sys.stderr)
        return 1

    # TTY guard: when invoked by the kernel, stdin is a pipe carrying the core
    # ELF. When invoked manually on a terminal with no piped input, reading
    # stdin would hang forever — refuse early.
    if sys.stdin.isatty():
        if args.json:
            emit_json_status(1, message="stdin is a TTY — refusing to run")
        else:
            print(
                "hephaestus-coredump-handler: stdin is a TTY — refusing to run "
                "(would block). This is a kernel core_pattern handler; test it with "
                "`printf 'fake' | hephaestus-coredump-handler <pid> <exe> <time> <sig>`.",
                file=sys.stderr,
            )
        return 1

    # --target-dir wins over the env var, which wins over the built-in default.
    target_dir = resolve_target_dir(_resolve_candidates(args.target_dir))

    max_bytes_env = os.environ.get("COREDUMP_MAX_BYTES")
    if max_bytes_env and max_bytes_env.strip():
        parsed = _parse_max_bytes(max_bytes_env)
        if parsed is None:
            _log(
                target_dir.parent,
                f"WARNING: COREDUMP_MAX_BYTES={max_bytes_env!r} is not a valid "
                f"byte count (expected digits with optional K/M/G/T suffix); "
                f"falling back to default {DEFAULT_MAX_BYTES}",
            )
            max_bytes = DEFAULT_MAX_BYTES
        else:
            max_bytes = parsed
    else:
        max_bytes = DEFAULT_MAX_BYTES

    out_path = write_core(
        sys.stdin.buffer,
        pid=args.pid,
        exe=args.exe,
        crash_time=args.crash_time,
        signal=args.signal,
        target_dir=target_dir,
        max_bytes=max_bytes,
    )
    if args.json:
        emit_json_status(0, message="core written", path=str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
