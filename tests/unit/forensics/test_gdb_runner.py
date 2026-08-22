#!/usr/bin/env python3
"""Tests for the run-under-gdb command wrapper."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock, call

import pytest

from hephaestus.forensics import gdb_runner
from hephaestus.forensics.gdb_runner import (
    _EXECUTION_TIMEOUT_SECONDS,
    _build_parser,
    _run_bounded,
    _validate_gdb_cmd_prefix,
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
        for signal_name in ("SIGABRT", "SIGSEGV", "SIGBUS", "SIGILL", "SIGFPE"):
            # The template aligns the signal column with padding spaces, so
            # match the tokens individually rather than a fixed-spacing string.
            assert f"handle {signal_name}" in script
            handle_line = next(
                line for line in script.splitlines() if line.startswith(f"handle {signal_name}")
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


class TestGdbCmdPrefixParsing:
    """Regression tests for GDB_CMD_PREFIX shell-quote parsing (issue #756)."""

    @staticmethod
    def _capture_argv(monkeypatch) -> list[list[str]]:
        captured: list[list[str]] = []
        process = MagicMock()
        process.pid = 4242
        process.wait.return_value = 0

        def fake_popen(argv, *, start_new_session, process_group):
            captured.append(list(argv))
            return process

        monkeypatch.setattr("hephaestus.forensics.gdb_runner.subprocess.Popen", fake_popen)
        return captured

    def test_prefix_none_yields_no_prefix_tokens(self, monkeypatch, tmp_path) -> None:
        captured = self._capture_argv(monkeypatch)
        run_under_gdb(str(tmp_path / "cores"), "sh", ["-c", "true"], gdb_cmd_prefix=None)
        assert captured, "subprocess.Popen was not invoked"
        assert captured[0][0] == "gdb"

    def test_prefix_empty_string_yields_no_prefix_tokens(self, monkeypatch, tmp_path) -> None:
        captured = self._capture_argv(monkeypatch)
        run_under_gdb(str(tmp_path / "cores"), "sh", ["-c", "true"], gdb_cmd_prefix="")
        assert captured[0][0] == "gdb"

    def test_unquoted_prefix_splits_on_whitespace(self, monkeypatch, tmp_path) -> None:
        captured = self._capture_argv(monkeypatch)
        run_under_gdb(
            str(tmp_path / "cores"),
            "sh",
            ["-c", "true"],
            gdb_cmd_prefix="uv run --",
        )
        argv = captured[0]
        assert argv[:3] == ["uv", "run", "--"]
        assert argv[3] == "gdb"

    def test_single_quoted_path_with_spaces_stays_one_token(self, monkeypatch, tmp_path) -> None:
        """Regression for issue #756: '/path with space/uv' must be ONE token."""
        captured = self._capture_argv(monkeypatch)
        run_under_gdb(
            str(tmp_path / "cores"),
            "sh",
            ["-c", "true"],
            gdb_cmd_prefix="'/path with space/uv' run --",
        )
        argv = captured[0]
        assert argv[:3] == ["/path with space/uv", "run", "--"]
        assert argv[3] == "gdb"

    def test_double_quoted_path_with_spaces_stays_one_token(self, monkeypatch, tmp_path) -> None:
        captured = self._capture_argv(monkeypatch)
        run_under_gdb(
            str(tmp_path / "cores"),
            "sh",
            ["-c", "true"],
            gdb_cmd_prefix='"/abs path/to/uv" run --',
        )
        argv = captured[0]
        assert argv[:3] == ["/abs path/to/uv", "run", "--"]

    def test_malformed_quoting_raises_valueerror(self, monkeypatch, tmp_path) -> None:
        """Unclosed quotes surface as ValueError, not as silently broken argv."""
        self._capture_argv(monkeypatch)
        with pytest.raises(ValueError):
            run_under_gdb(
                str(tmp_path / "cores"),
                "sh",
                ["-c", "true"],
                gdb_cmd_prefix="'unterminated",
            )


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

    def test_run_under_gdb_0_json_envelope(self, monkeypatch, capsys) -> None:
        """RUN_UNDER_GDB=0 with --json emits a status envelope."""
        import json

        monkeypatch.setenv("RUN_UNDER_GDB", "0")
        rc = main(["--json", "/tmp/unused-core-dir", "sh", "-c", "exit 0"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert "directly" in payload["message"]

    def test_gdb_branch_json_envelope(self, monkeypatch, capsys, tmp_path: Path) -> None:
        """The gdb-wrapped branch emits a JSON envelope when --json is set."""
        import json

        from hephaestus.forensics import gdb_runner

        monkeypatch.delenv("RUN_UNDER_GDB", raising=False)
        monkeypatch.setattr(gdb_runner, "run_under_gdb", lambda **kw: 0)
        rc = main(["--json", str(tmp_path), "sh"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert "gdb" in payload["message"]

    def test_gdb_branch_no_json(self, monkeypatch, tmp_path: Path) -> None:
        """The gdb-wrapped branch returns the inferior's exit code without --json."""
        from hephaestus.forensics import gdb_runner

        monkeypatch.delenv("RUN_UNDER_GDB", raising=False)
        monkeypatch.setattr(gdb_runner, "run_under_gdb", lambda **kw: 42)
        rc = main([str(tmp_path), "sh"])
        assert rc == 42

    @pytest.mark.parametrize("direct", [True, False])
    def test_timeout_returns_124_at_each_execution_path(
        self,
        direct: bool,
        monkeypatch: pytest.MonkeyPatch,
        capsys,
        tmp_path: Path,
    ) -> None:
        """Both CLI execution paths return fixed timeout status output."""
        hostile = "secret-" * 20_000
        error = subprocess.TimeoutExpired(
            [hostile],
            7,
            output=hostile,
            stderr=hostile,
        )
        if direct:
            monkeypatch.setenv("RUN_UNDER_GDB", "0")
            monkeypatch.setattr(gdb_runner, "_run_bounded", Mock(side_effect=error))
        else:
            monkeypatch.delenv("RUN_UNDER_GDB", raising=False)
            monkeypatch.setattr(gdb_runner, "run_under_gdb", Mock(side_effect=error))

        rc = main(["--json", "--timeout", "7", str(tmp_path / "cores"), "tool", hostile])

        captured = capsys.readouterr()
        assert rc == 124
        assert captured.err == "[run-under-gdb] ERROR: command timed out\n"
        assert json.loads(captured.out) == {
            "status": "error",
            "exit_code": 124,
            "message": "command timed out",
        }
        assert hostile not in captured.out
        assert hostile not in captured.err

    def test_direct_bypass_succeeds_without_process_groups(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Direct execution preserves success when process groups are unavailable."""
        process = MagicMock(pid=4242)
        process.wait.return_value = 0
        popen = Mock(return_value=process)
        monkeypatch.setenv("RUN_UNDER_GDB", "0")
        monkeypatch.setattr(gdb_runner, "_PROCESS_GROUPS_SUPPORTED", False)
        monkeypatch.setattr(subprocess, "Popen", popen)

        assert main(["--timeout", "7", "unused-cores", "tool"]) == 0
        assert popen.call_args.kwargs["start_new_session"] is False
        assert popen.call_args.kwargs["process_group"] is None
        process.kill.assert_not_called()

    def test_gdb_success_without_process_groups(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Gdb execution preserves success when process groups are unavailable."""
        process = MagicMock(pid=4242)
        process.wait.return_value = 0
        monkeypatch.setattr(gdb_runner, "_PROCESS_GROUPS_SUPPORTED", False)
        monkeypatch.setattr(gdb_runner, "resolve_command", lambda _command: "/tool")
        monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))

        assert run_under_gdb(str(tmp_path / "cores"), "tool", [], timeout=7) == 0
        process.kill.assert_not_called()

    @pytest.mark.skipif(not hasattr(os, "forkpty"), reason="requires a controlling TTY")
    def test_direct_bypass_preserves_controlling_tty(self) -> None:
        """The direct path keeps /dev/tty usable in a PTY-backed session."""
        pid, master_fd = os.forkpty()
        if pid == 0:
            os.environ["RUN_UNDER_GDB"] = "0"
            rc = main(
                [
                    "--timeout",
                    "5",
                    "unused-cores",
                    sys.executable,
                    "-c",
                    "with open('/dev/tty', 'rb') as tty: assert tty.isatty()",
                ]
            )
            os._exit(rc)

        try:
            _, status = os.waitpid(pid, 0)
        finally:
            os.close(master_fd)
        assert os.waitstatus_to_exitcode(status) == 0

    def test_artifact_cleanup_cannot_mask_timeout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Best-effort unlink failures preserve the execution timeout."""
        timeout = subprocess.TimeoutExpired(["gdb"], 7)
        monkeypatch.setattr(gdb_runner, "resolve_command", lambda _command: "/tool")
        monkeypatch.setattr(gdb_runner, "_run_bounded", Mock(side_effect=timeout))
        monkeypatch.setattr(Path, "unlink", Mock(side_effect=PermissionError("denied")))

        with pytest.raises(subprocess.TimeoutExpired):
            run_under_gdb(str(tmp_path / "cores"), "tool", [], timeout=7)


class TestBoundedProcess:
    """Tests for process-group and direct-child timeout cleanup."""

    def test_timeout_kills_and_reaps_process_group(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POSIX timeout cleanup kills the process group and reaps the child."""
        process = MagicMock(pid=4242)
        process.wait.side_effect = [
            subprocess.TimeoutExpired(["tool"], 7),
            -signal.SIGKILL,
        ]
        monkeypatch.setattr(gdb_runner, "_PROCESS_GROUPS_SUPPORTED", True)
        monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))
        killpg = Mock()
        monkeypatch.setattr(os, "killpg", killpg)

        with pytest.raises(subprocess.TimeoutExpired):
            _run_bounded(["tool"], 7)

        killpg.assert_called_once_with(4242, signal.SIGKILL)
        process.kill.assert_not_called()
        assert process.wait.call_args_list == [call(timeout=7), call(timeout=5)]

    def test_timeout_without_process_groups_kills_direct_child(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fallback cleanup kills and reaps only the direct child."""
        process = MagicMock(pid=4242)
        process.wait.side_effect = [
            subprocess.TimeoutExpired(["tool"], 7),
            -1,
        ]
        popen = Mock(return_value=process)
        monkeypatch.setattr(gdb_runner, "_PROCESS_GROUPS_SUPPORTED", False)
        monkeypatch.setattr(subprocess, "Popen", popen)

        with pytest.raises(subprocess.TimeoutExpired):
            _run_bounded(["tool"], 7)

        assert popen.call_args.kwargs["start_new_session"] is False
        process.kill.assert_called_once()
        assert process.wait.call_args_list == [call(timeout=7), call(timeout=5)]

    def test_second_reap_timeout_attempts_nonblocking_final_reap(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stalled reap is retried once without extending timeout cleanup."""
        process = MagicMock(pid=4242)
        process.wait.side_effect = [
            subprocess.TimeoutExpired(["tool"], 7),
            subprocess.TimeoutExpired(["tool"], 5),
            subprocess.TimeoutExpired(["tool"], 0),
        ]
        monkeypatch.setattr(gdb_runner, "_PROCESS_GROUPS_SUPPORTED", False)
        monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))

        with pytest.raises(RuntimeError, match="termination was not confirmed"):
            _run_bounded(["tool"], 7)

        assert process.kill.call_count == 2
        assert process.wait.call_args_list == [call(timeout=7), call(timeout=5), call(timeout=0)]

    def test_permission_error_during_group_kill_is_not_suppressed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A genuine termination failure is surfaced instead of reporting 124."""
        process = MagicMock(pid=4242)
        process.wait.side_effect = subprocess.TimeoutExpired(["tool"], 7)
        monkeypatch.setattr(gdb_runner, "_PROCESS_GROUPS_SUPPORTED", True)
        monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))
        monkeypatch.setattr(os, "killpg", Mock(side_effect=PermissionError("denied")))

        with pytest.raises(PermissionError, match="denied"):
            _run_bounded(["tool"], 7)

    @pytest.mark.parametrize("process_groups", [True, False])
    def test_keyboard_interrupt_cleans_up_before_reraising(
        self,
        process_groups: bool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both POSIX-group and direct-child paths clean up on Ctrl-C."""
        process = MagicMock(pid=4242)
        process.wait.side_effect = [KeyboardInterrupt, -signal.SIGKILL]
        monkeypatch.setattr(gdb_runner, "_PROCESS_GROUPS_SUPPORTED", process_groups)
        monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))
        killpg = Mock()
        monkeypatch.setattr(os, "killpg", killpg)

        with pytest.raises(KeyboardInterrupt):
            _run_bounded(["tool"], 7)

        if process_groups:
            killpg.assert_called_once_with(4242, signal.SIGKILL)
            process.kill.assert_not_called()
        else:
            process.kill.assert_called_once()
        assert process.wait.call_args_list == [call(timeout=7), call(timeout=5)]

    @pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
    def test_timeout_kills_real_descendant(self, tmp_path: Path) -> None:
        """A timed-out parent does not leave its process-group descendant alive."""
        pid_file = tmp_path / "child.pid"
        child_code = "import time; time.sleep(60)"
        parent_code = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', sys.argv[3]])\n"
            "with open(sys.argv[1], 'w') as pid_file:\n"
            "    pid_file.write(str(child.pid))\n"
            "    pid_file.flush()\n"
            "time.sleep(60)\n"
        )

        with pytest.raises(subprocess.TimeoutExpired):
            _run_bounded(
                [
                    sys.executable,
                    "-c",
                    parent_code,
                    str(pid_file),
                    "parent",
                    child_code,
                ],
                1,
            )

        deadline = time.monotonic() + 5
        while not pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.is_file()
        child_pid = int(pid_file.read_text())

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            # A killed descendant may remain briefly as a zombie until its
            # reaper collects it. Confirm that state portably: read the
            # kernel's /proc state directly instead of relying on procps
            # userland, which minimal CI containers do not ship. A platform
            # without /proc keeps requiring a positive ProcessLookupError.
            proc_stat = Path(f"/proc/{child_pid}/stat")
            if proc_stat.exists():
                try:
                    state = (
                        proc_stat.read_text(encoding="utf-8")
                        .rsplit(")", 1)[1]
                        .split()[0]
                    )
                except (OSError, IndexError):
                    state = None
                if state == "Z":
                    break
            time.sleep(0.01)
        else:
            pytest.fail(f"descendant process {child_pid} survived timeout cleanup")

    @_requires_gdb
    @pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
    def test_gdb_timeout_kills_forking_inferior_descendant(self, tmp_path: Path) -> None:
        """Timeout cleanup kills a descendant in GDB's inferior process group."""
        pid_file = tmp_path / "gdb-child.pid"
        child_code = "import time; time.sleep(60)"
        inferior_code = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', sys.argv[2]])\n"
            "with open(sys.argv[1], 'w') as pid_file:\n"
            "    pid_file.write(str(child.pid))\n"
            "    pid_file.flush()\n"
            "time.sleep(60)\n"
        )

        with pytest.raises(subprocess.TimeoutExpired):
            run_under_gdb(
                str(tmp_path / "cores"),
                sys.executable,
                ["-c", inferior_code, str(pid_file), child_code],
                timeout=3,
            )

        assert pid_file.is_file(), "GDB did not start the forking inferior"
        child_pid = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            status = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(child_pid)],
                check=False,
                capture_output=True,
                text=True,
                timeout=1,
            )
            if status.returncode == 0 and status.stdout.strip().startswith("Z"):
                break
            time.sleep(0.01)
        else:
            pytest.fail(f"GDB inferior descendant {child_pid} survived timeout cleanup")


class TestExecutionTimeoutParser:
    """Tests for the bounded execution timeout CLI option."""

    @pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("86400", 86400)])
    def test_accepts_inclusive_timeout_bounds(self, raw: str, expected: int) -> None:
        """The parser accepts both inclusive timeout endpoints."""
        args = _build_parser().parse_args(["--timeout", raw, "cores", "tool"])
        assert args.timeout == expected

    @pytest.mark.parametrize("raw", ["0", "-1", "86401", "not-an-int"])
    def test_rejects_invalid_timeout(self, raw: str) -> None:
        """The parser rejects invalid timeout values."""
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["--timeout", raw, "cores", "tool"])

    def test_default_timeout_is_documented_value(self) -> None:
        """The parser defaults to the documented two-hour limit."""
        args = _build_parser().parse_args(["cores", "tool"])
        assert args.timeout == _EXECUTION_TIMEOUT_SECONDS

    def test_timeout_after_command_is_remainder(self) -> None:
        """The remainder-consuming command position requires timeout first."""
        args = _build_parser().parse_args(["cores", "tool", "--timeout", "7"])
        assert args.timeout == _EXECUTION_TIMEOUT_SECONDS
        assert args.command_args == ["--timeout", "7"]


class TestValidateGdbCmdPrefix:
    """Tests for GDB_CMD_PREFIX whitelist validation."""

    @pytest.mark.parametrize("raw", [None, "", "   ", "\t\n  "])
    def test_empty_input_returns_empty_list(self, raw: str | None) -> None:
        """Empty, None, or whitespace-only input returns an empty list."""
        assert _validate_gdb_cmd_prefix(raw) == []

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("uv run --", ["uv", "run", "--"]),
            ("/usr/bin/env", ["/usr/bin/env"]),
            ("env FOO=bar baz", ["env", "FOO=bar", "baz"]),
            ("nice", ["nice"]),
            ("direnv exec . --", ["direnv", "exec", ".", "--"]),
        ],
    )
    def test_accepts_safe_prefixes(self, raw: str, expected: list[str]) -> None:
        """Safe prefixes are validated and returned as token lists."""
        assert _validate_gdb_cmd_prefix(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "--init-eval-command=run",
            "-ex",
            "uv --bad",
            "rm; rm -rf /",
            "foo|bar",
            "foo&bar",
            "foo&&bar",
            "$(echo hi)",
            "`id`",
            "foo>out",
            "foo<in",
            "foo*",
            "foo?",
            "foo'bar",
            'foo"bar',
            "foo#bar",
            "foo!bar",
            "foo;bar",
        ],
    )
    def test_rejects_unsafe_prefixes(self, raw: str) -> None:
        """Unsafe prefixes raise ValueError with a descriptive message.

        After issue #756 the value is tokenized with ``shlex.split`` before the
        per-token whitelist runs. Unbalanced-quote cases (e.g. ``foo'bar``) are
        rejected by shlex itself (re-raised with a ``GDB_CMD_PREFIX`` message);
        the remaining cases survive tokenization but carry shell metacharacters
        outside the whitelist.
        """
        with pytest.raises(ValueError, match="GDB_CMD_PREFIX"):
            _validate_gdb_cmd_prefix(raw)


class TestRunUnderGdbPrefixValidation:
    """run_under_gdb surfaces the prefix-validation error to callers."""

    def test_unsafe_prefix_raises_before_subprocess(self, tmp_path: Path) -> None:
        """Hoisted validation fires before resolve_command for unsafe prefix."""
        with pytest.raises(ValueError, match="GDB_CMD_PREFIX"):
            run_under_gdb(
                str(tmp_path / "cores"),
                _UNRESOLVABLE_CMD,
                [],
                gdb_cmd_prefix="--init-eval-command=run",
            )

    def test_safe_prefix_does_not_raise(self, tmp_path: Path) -> None:
        """Safe prefix passes validation; unresolvable command still returns 127."""
        rc = run_under_gdb(
            str(tmp_path / "cores"),
            _UNRESOLVABLE_CMD,
            [],
            gdb_cmd_prefix="uv run --",
        )
        assert rc == 127


class TestMainPrefixValidation:
    """main() converts validation errors into a clean CLI error + exit 2."""

    def test_main_returns_2_on_unsafe_env_var(self, monkeypatch, capsys, tmp_path: Path) -> None:
        """main() returns 2 and prints ERROR to stderr for invalid GDB_CMD_PREFIX."""
        monkeypatch.delenv("RUN_UNDER_GDB", raising=False)
        monkeypatch.setenv("GDB_CMD_PREFIX", "--init-eval-command=run")
        rc = main([str(tmp_path / "cores"), _UNRESOLVABLE_CMD])
        captured = capsys.readouterr()
        assert rc == 2
        assert "[run-under-gdb] ERROR:" in captured.err
        assert "GDB_CMD_PREFIX" in captured.err

    def test_main_json_envelope_on_unsafe_env_var(
        self, monkeypatch, capsys, tmp_path: Path
    ) -> None:
        """main() emits a JSON status envelope with status != ok on invalid prefix."""
        import json

        monkeypatch.delenv("RUN_UNDER_GDB", raising=False)
        monkeypatch.setenv("GDB_CMD_PREFIX", "--init-eval-command=run")
        rc = main(["--json", str(tmp_path / "cores"), _UNRESOLVABLE_CMD])
        assert rc == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] != "ok"
        assert "GDB_CMD_PREFIX" in payload["message"]

    def test_main_safe_env_var_unchanged(self, monkeypatch, tmp_path: Path) -> None:
        """Safe prefix + unresolvable command: validation passes, returns 127."""
        monkeypatch.delenv("RUN_UNDER_GDB", raising=False)
        monkeypatch.setenv("GDB_CMD_PREFIX", "uv run --")
        rc = main([str(tmp_path / "cores"), _UNRESOLVABLE_CMD])
        assert rc == 127
