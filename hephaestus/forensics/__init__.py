"""Crash-forensics utilities for Hephaestus.

Two project-agnostic tools for capturing real core dumps from crashes that
are otherwise hard to observe:

* :mod:`hephaestus.forensics.coredump_handler` — a kernel pipe-mode
  ``core_pattern`` handler that captures cores from processes crashing inside
  containers, writing them to a host-side path that survives container
  teardown.
* :mod:`hephaestus.forensics.gdb_runner` — runs any command under
  ``gdb -batch`` so a real ELF core and backtrace are captured before a
  runtime's own in-process signal handler can swallow the fault.
"""

from .coredump_handler import (
    BUNDLE_NOT_RUN,
    BUNDLE_OK,
    BUNDLE_RAN_WITH_ERRORS,
    DEFAULT_MAX_BYTES,
    DEFAULT_TARGET_DIRS,
    VERIFY_SIGNAL_LOST_EXIT,
    resolve_target_dir,
    verify_crash_bundle,
    write_core,
)
from .gdb_runner import (
    build_gdb_script,
    resolve_command,
    run_under_gdb,
)

__all__ = [
    "BUNDLE_NOT_RUN",
    "BUNDLE_OK",
    "BUNDLE_RAN_WITH_ERRORS",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TARGET_DIRS",
    "VERIFY_SIGNAL_LOST_EXIT",
    "build_gdb_script",
    "resolve_command",
    "resolve_target_dir",
    "run_under_gdb",
    "verify_crash_bundle",
    "write_core",
]
