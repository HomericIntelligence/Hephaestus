#!/usr/bin/env bash
#
# run-under-gdb.sh — run any command under gdb to catch crash signals that
# the program's own in-process signal handler would otherwise swallow.
#
# Some runtimes (JIT compilers, sanitizer runtimes, language VMs) install
# their own SIGABRT/SIGSEGV/SIGILL handlers that catch a fatal signal, print
# a terse post-handler trace, and exit cleanly — so the kernel never produces
# a core and the *real* faulting frame is lost. Running the command under gdb
# stops the inferior in the debugger BEFORE its own handler runs, so a real
# ELF core and a full backtrace can be captured at the moment of fault.
#
# Usage:
#   run-under-gdb.sh <core-dir> <command> [args...]
#
#   <core-dir>   Directory for cores and gdb logs (created if absent).
#   <command>    The program to run. Resolved via PATH if not an absolute
#                path. All remaining arguments are passed to it verbatim.
#
# Environment variables:
#   RUN_UNDER_GDB=0       Skip gdb entirely and exec the command directly
#                         (local-dev escape hatch — gdb adds overhead).
#   GDB_CMD_PREFIX        Optional command prefix inserted before `gdb`, e.g.
#                         "uv run --", so gdb and
#                         its inferior inherit an activated environment.
#                         Default: empty (gdb is run directly).
#
# Output files (written on crash):
#   <core-dir>/core.gdb.<timestamp>  — ELF core (multi-MB)
#   <core-dir>/gdb-<timestamp>.log   — backtrace + threads + shared libraries
#
# Exit code:
#   - 0 / N            normal exit with the inferior's own exit code N
#   - 128 + signo      the inferior was stopped by a caught signal
#                      (134 = SIGABRT, 139 = SIGSEGV, 132 = SIGILL, ...)

set -euo pipefail

if [ $# -lt 2 ]; then
    echo "[run-under-gdb] usage: $0 <core-dir> <command> [args...]" >&2
    exit 1
fi

CORE_DIR="$1"
shift
CMD="$1"
shift

# Escape hatch: RUN_UNDER_GDB=0 bypasses gdb for local dev where overhead matters.
if [ "${RUN_UNDER_GDB:-1}" = "0" ]; then
    exec "$CMD" "$@"
fi

mkdir -p "$CORE_DIR"
TS=$(date +%s)
GDB_LOG="${CORE_DIR}/gdb-${TS}.log"
CORE_FILE="${CORE_DIR}/core.gdb.${TS}"

# Resolve the command to a concrete executable path so gdb has a real ELF
# file argument. An absolute/relative path is used as-is; a bare name is
# resolved via PATH. If resolution fails, fall back to running it directly.
if [ -x "$CMD" ]; then
    CMD_BIN="$CMD"
else
    CMD_BIN=$(command -v "$CMD" 2>/dev/null || echo "")
fi
if [ -z "$CMD_BIN" ] || [ ! -x "$CMD_BIN" ]; then
    echo "[run-under-gdb] WARNING: could not resolve '$CMD'; running directly without gdb" >&2
    exec "$CMD" "$@"
fi

hephaestus_tmp_subdir() {
    local component="$1"
    local base
    base="${TMPDIR:-/tmp}/hephaestus-$(id -u)"
    if [[ -e "$base" && ( -L "$base" || ! -d "$base" || ! -O "$base" ) ]]; then
        echo "[run-under-gdb] ERROR: unsafe temp root: $base" >&2
        return 1
    fi
    mkdir -p "$base/$component" || return 1
    if [[ -L "$base/$component" || ! -O "$base/$component" ]]; then
        echo "[run-under-gdb] ERROR: unsafe temp directory: $base/$component" >&2
        return 1
    fi
    chmod 700 "$base" "$base/$component" || return 1
    printf '%s\n' "$base/$component"
}

# Write the gdb command script to a temp file. Multi-line gdb scripts passed
# via repeated -ex flags are not portable across gdb versions; -x <file> is.
GDB_TMP_DIR="$(hephaestus_tmp_subdir run-under-gdb)" || exit 1
GDB_SCRIPT=$(mktemp "$GDB_TMP_DIR/run-under-gdb-XXXXXX.gdb")
EXIT_CODE_FILE="${CORE_DIR}/exit-${TS}.code"
trap 'rm -f -- "$GDB_SCRIPT"' EXIT
export RUN_UNDER_GDB_LOG="$GDB_LOG"
export RUN_UNDER_GDB_CORE="$CORE_FILE"
export RUN_UNDER_GDB_EXIT="$EXIT_CODE_FILE"

# The gdb script uses Python event hooks rather than a plain gdb-script `if`
# because:
#  1. `handle SIG* stop` + a `hook-stop` block fires on EVERY stop event,
#     including normal exit, where `generate-core-file` then fails with
#     "You can't do that without a process to debug".
#  2. `--return-child-result` is unreliable in batch mode for processes
#     killed by *handled* signals: gdb often exits 0 even when the inferior
#     died on a signal, masking the failure entirely.
# Python events cleanly distinguish gdb.SignalEvent (real crash) from
# gdb.ExitedEvent (clean exit) and record the intended exit code to a file
# that this wrapper reads back afterwards.
cat > "$GDB_SCRIPT" <<GDBEOF
set pagination off
set confirm off

python
import gdb
import os
import shlex

EXIT_FILE = os.environ["RUN_UNDER_GDB_EXIT"]
CORE_FILE = os.environ["RUN_UNDER_GDB_CORE"]
LOG_FILE = os.environ["RUN_UNDER_GDB_LOG"]
gdb.execute("set logging file " + shlex.quote(LOG_FILE))
gdb.execute("set logging overwrite on")
gdb.execute("set logging enabled on")
# POSIX shell convention for signal-terminated processes (128 + signo).
SIG_MAP = {"SIGABRT": 6, "SIGSEGV": 11, "SIGBUS": 7, "SIGFPE": 8, "SIGILL": 4}
state = {"signaled": False}

def write_exit(code):
    with open(EXIT_FILE, "w") as f:
        f.write(str(code))

# Default = 1 covers the case where neither handler fires (e.g. gdb itself dies).
write_exit(1)

def on_stop(event):
    # We never set breakpoints, so the only stop events we expect are signals.
    if isinstance(event, gdb.SignalEvent):
        signo = event.stop_signal
        print("[run-under-gdb] caught " + signo + "; dumping " + CORE_FILE)
        gdb.execute("generate-core-file " + shlex.quote(CORE_FILE))
        gdb.execute("bt full")
        gdb.execute("info threads")
        gdb.execute("info sharedlibrary")
        state["signaled"] = True
        write_exit(128 + SIG_MAP.get(signo, 1))

def on_exit(event):
    # gdb fires Exited after the post-signal kill too. If we already captured
    # a signal, do not overwrite that exit code with the kill's exit code.
    if state["signaled"]:
        return
    code = getattr(event, "exit_code", None)
    write_exit(code if code is not None else 0)

gdb.events.stop.connect(on_stop)
gdb.events.exited.connect(on_exit)
end

# Intercept crash signals before the inferior's own handlers run.
# "nopass" prevents the signal from being delivered to the inferior.
handle SIGABRT stop nopass print
handle SIGSEGV stop nopass print
handle SIGBUS  stop nopass print
handle SIGILL  stop nopass print
handle SIGFPE  stop nopass print

run

set logging enabled off
quit
GDBEOF

echo "[run-under-gdb] gdb log  : ${GDB_LOG}" >&2
echo "[run-under-gdb] core file: ${CORE_FILE} (written on crash)" >&2
echo "[run-under-gdb] binary   : ${CMD_BIN}" >&2
echo "[run-under-gdb] args     : $*" >&2

# GDB_CMD_PREFIX lets the caller run gdb inside an environment activator
# (e.g. "uv run --") so gdb and its inferior inherit the activated env.
# Unquoted on purpose: the prefix is a word list, not a single argument.
# `set -e` would abort here if gdb exits non-zero before we read the exit
# file; disable it just for this invocation.
set +e
${GDB_CMD_PREFIX:-} gdb -batch -nx -x "$GDB_SCRIPT" --args "$CMD_BIN" "$@"
gdb_status=$?
set -e

# Prefer the Python-recorded exit code; fall back to gdb's own status if the
# file is missing (gdb crashed before the python hook fired).
if [ -r "$EXIT_CODE_FILE" ]; then
    inferior_exit=$(cat "$EXIT_CODE_FILE")
    rm -f "$EXIT_CODE_FILE"
    exit "$inferior_exit"
fi
exit "$gdb_status"
