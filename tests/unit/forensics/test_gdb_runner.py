#!/usr/bin/env python3
"""Tests for the run-under-gdb command wrapper."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hephaestus.forensics.gdb_runner import (
    build_gdb_script,
    main,
    resolve_command,
    run_under_gdb,
)

#: A command name guaranteed not to resolve on PATH — exercises the
#: no-gdb fallback path of run_under_gdb without needing gdb installed.
_UNRESOLVABLE_CMD = "definitely_not_a_real_command_xyz"

#: Skip marker for tests that genuinely invoke gdb (an integration concern;
#: gdb is not guaranteed to be present in the unit-test environment).
_requires_gdb = pytest.mark.skipif(
    shutil.which("gdb") is None, reason="gdb is not installed in this environment"
)


class TestResolveCommand:
    """Tests for resolve_command."""

    def test_resolves_bare_name_via_path(self) -> None:
        """A bare command name is resolved through PATH."""
        result = resolve_command("sh")
        assert result is not None
        assert result == shutil.which("sh")

    def test_resolves_explicit_executable_path(self, tmp_path: Path) -> None:
        """An explicit path to an executable file is returned as-is."""
        script = tmp_path / "tool"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        assert resolve_command(str(script)) == str(script)

    def test_returns_none_for_unresolvable_command(self) -> None:
        """An unknown command resolves to None."""
        assert resolve_command("definitely_not_a_real_command_xyz") is None

    def test_returns_none_for_non_executable_path(self, tmp_path: Path) -> None:
        """A path that exists but is not executable resolves to None."""
        not_exec = tmp_path / "data.txt"
        not_exec.write_text("not a program")
        assert resolve_command(str(not_exec)) is None


class TestBuildGdbScript:
    """Tests for build_gdb_script."""

    def test_embeds_all_three_paths(self) -> None:
        """The rendered script references the log, core, and exit-code paths."""
        script = build_gdb_script(
            gdb_log="/cores/gdb.log",
            core_file="/cores/core.gdb.1",
            exit_file="/cores/exit.code",
        )
        assert "/cores/gdb.log" in script
        assert "/cores/core.gdb.1" in script
        assert "/cores/exit.code" in script

    def test_intercepts_crash_signals(self) -> None:
        """The script installs handlers for the expected fatal signals."""
        script = build_gdb_script("a", "b", "c")
        for signal in ("SIGABRT", "SIGSEGV", "SIGBUS", "SIGILL", "SIGFPE"):
            # The template aligns the signal column with padding spaces, so
            # match the tokens individually rather than a fixed-spacing string.
            assert f"handle {signal}" in script
            handle_line = next(
                line for line in script.splitlines() if line.startswith(f"handle {signal}")
            )
            assert "stop" in handle_line
            assert "nopass" in handle_line

    def test_uses_python_event_hooks(self) -> None:
        """The script wires gdb.events rather than a plain hook-stop block."""
        script = build_gdb_script("a", "b", "c")
        assert "gdb.events.stop.connect" in script
        assert "gdb.events.exited.connect" in script


class TestRunUnderGdb:
    """Tests for run_under_gdb."""

    def test_creates_core_dir(self, tmp_path: Path) -> None:
        """The core directory is created before the command is resolved."""
        # An unresolvable command exercises the early-return path: the core
        # dir is created first, so this works without gdb installed.
        core_dir = tmp_path / "deep" / "cores"
        run_under_gdb(str(core_dir), _UNRESOLVABLE_CMD, [])
        assert core_dir.is_dir()

    def test_unresolvable_command_returns_127(self, tmp_path: Path) -> None:
        """An unresolvable command returns 127 (POSIX 'command not found')."""
        rc = run_under_gdb(str(tmp_path / "cores"), _UNRESOLVABLE_CMD, [])
        assert rc == 127

    @_requires_gdb
    def test_clean_exit_under_gdb(self, tmp_path: Path) -> None:
        """A command that exits 0 under gdb yields exit code 0."""
        rc = run_under_gdb(str(tmp_path / "cores"), "true", [])
        assert rc == 0

    @_requires_gdb
    def test_nonzero_exit_under_gdb(self, tmp_path: Path) -> None:
        """A non-zero exit is propagated through the gdb wrapper."""
        rc = run_under_gdb(str(tmp_path / "cores"), "sh", ["-c", "exit 5"])
        assert rc == 5


class TestMain:
    """Tests for the CLI entry point."""

    def test_run_under_gdb_0_bypasses_gdb(self, monkeypatch) -> None:
        """RUN_UNDER_GDB=0 execs the command directly and returns its code."""
        monkeypatch.setenv("RUN_UNDER_GDB", "0")
        rc = main(["/tmp/unused-core-dir", "sh", "-c", "exit 0"])
        assert rc == 0

    def test_run_under_gdb_0_propagates_nonzero(self, monkeypatch) -> None:
        """RUN_UNDER_GDB=0 propagates the command's non-zero exit code."""
        monkeypatch.setenv("RUN_UNDER_GDB", "0")
        rc = main(["/tmp/unused-core-dir", "sh", "-c", "exit 3"])
        assert rc == 3
