#!/usr/bin/env bash
#
# coredump-host-handler.sh — pipe-mode core_pattern handler
#
# Invoked by the Linux kernel (as PID 1 of the HOST namespace) when any
# process with `ulimit -c > 0` crashes. The kernel pipes the full ELF core
# on stdin and passes positional args set via core_pattern format tokens.
#
# Why a pipe handler: when the crashing process runs inside a container
# (Podman/Docker), a plain `core_pattern` file path is resolved against the
# *container's* mount namespace, so the kernel silently writes nothing the
# host can see. A pipe handler runs in the HOST namespace and can write to
# a host-side directory that survives container teardown.
#
# Install:
#   sudo cp scripts/shell/coredump-host-handler.sh \
#     /usr/local/bin/coredump-host-handler.sh
#   sudo chmod +x /usr/local/bin/coredump-host-handler.sh
#   echo "|/usr/local/bin/coredump-host-handler.sh %p %e %t %s %P" \
#     | sudo tee /proc/sys/kernel/core_pattern
#
# core_pattern tokens (must match the install line above, in order):
#   %p  PID of crashing process (in its own PID namespace)
#   %e  executable basename
#   %t  time of crash (seconds since epoch)
#   %s  signal number
#   %P  global PID (host namespace) — captured, not used in the filename
#
# Environment variables:
#   COREDUMP_TARGET_DIRS   Colon-separated list of candidate output dirs,
#                          tried in order; the first that already exists
#                          wins. If none exist, the LAST entry is created.
#                          Default: "/tmp/crash-bundle/cores".
#                          A CI job typically points this at its workspace,
#                          e.g. "/home/runner/work/<repo>/<repo>/crash-bundle/cores".
#   COREDUMP_MAX_BYTES     Cap on core size written to disk, in bytes.
#                          Default: 4 GiB. Prevents a runaway process from
#                          filling the host disk.
#
# Manual test (does NOT hang — the TTY guard prevents blocking on a terminal):
#   echo "fake core data" | \
#     ./scripts/shell/coredump-host-handler.sh 1234 myproc 1700000000 11 5678
#
# Exit code note: the kernel ignores this handler's exit code entirely. Every
# failure path therefore logs to handler.log instead of relying on exit
# status — a missing handler.log is the only signal that the handler did not
# run at all.

set -euo pipefail

# ---------------------------------------------------------------------------
# Arguments (from core_pattern tokens)
# ---------------------------------------------------------------------------
PID="${1:-unknown}"
EXE="${2:-unknown}"
TIME="${3:-0}"
SIGNAL="${4:-0}"
# $5 = global (host) PID — captured by the format string but unused here.

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_TARGET_DIRS="/tmp/crash-bundle/cores"
TARGET_DIRS="${COREDUMP_TARGET_DIRS:-$DEFAULT_TARGET_DIRS}"
MAX_BYTES="${COREDUMP_MAX_BYTES:-$((4 * 1024 * 1024 * 1024))}"

# ---------------------------------------------------------------------------
# TTY guard
# ---------------------------------------------------------------------------
# When invoked by the kernel, stdin is a pipe carrying the core ELF — not a
# terminal. When invoked manually for testing with no piped input, stdin IS a
# terminal and reading from it would hang forever. Bail out early in that case.
if [ -t 0 ]; then
    echo "coredump-host-handler: stdin is a TTY — refusing to run (would block)." >&2
    echo "  This script is meant to be invoked by the kernel via core_pattern." >&2
    echo "  Manual test: echo somecore | $0 <pid> <exe> <time> <signal> [<gpid>]" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Resolve the destination directory
# ---------------------------------------------------------------------------
# Walk the candidate list in order; the first existing directory wins. If none
# exist, fall back to the LAST candidate and create it. Running as host PID 1
# means well-known host paths (e.g. a GitHub Actions workspace) are reachable.
TARGET=""
LAST_CANDIDATE=""
IFS=':' read -r -a _candidates <<< "$TARGET_DIRS"
for candidate in "${_candidates[@]}"; do
    [ -n "$candidate" ] || continue
    LAST_CANDIDATE="$candidate"
    if [ -d "$candidate" ]; then
        TARGET="$candidate"
        break
    fi
done
if [ -z "$TARGET" ]; then
    TARGET="${LAST_CANDIDATE:-/tmp/crash-bundle/cores}"
fi

mkdir -p "$TARGET"

# LOG_DIR must be resolved BEFORE the first log write below.
LOG_DIR="$(dirname "$TARGET")"

# ---------------------------------------------------------------------------
# Write the core ELF
# ---------------------------------------------------------------------------
OUT="$TARGET/core.${PID}.${EXE}.${TIME}.sig${SIGNAL}"
if ! head -c "$MAX_BYTES" > "$OUT"; then
    echo "$(date -Iseconds) ERROR: failed to write core to $OUT" \
        >> "$LOG_DIR/handler.log" 2>/dev/null
fi
if ! chmod 644 "$OUT" 2>/dev/null; then
    echo "$(date -Iseconds) WARNING: chmod 644 $OUT failed (file may be unreadable)" \
        >> "$LOG_DIR/handler.log" 2>/dev/null
fi

# ---------------------------------------------------------------------------
# Log the capture
# ---------------------------------------------------------------------------
SIZE=$(stat -c %s "$OUT" 2>/dev/null || echo "?")
{
    printf '%s wrote %s (%s bytes) signal=%s exe=%s\n' \
        "$(date -Iseconds)" "$OUT" "$SIZE" "$SIGNAL" "$EXE"
} >> "$LOG_DIR/handler.log" 2>/dev/null

# Kernel-invoked: if the log append failed (e.g. LOG_DIR is unwritable), there
# is no safe way to surface the error — the kernel has no channel for our exit
# code. Failure here is silent by design; a downstream artifact-upload step
# should surface a missing handler.log as "handler was not invoked".
true
