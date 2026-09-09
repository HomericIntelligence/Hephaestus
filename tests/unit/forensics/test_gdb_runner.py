#!/usr/bin/env python3
"""Tests for the run-under-gdb command wrapper."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hephaestus.forensics import gdb_runner
from hephaestus.forensics.gdb_runner import (
    _parse_execution_timeout,
    _read_process_group,
    _run_bounded,
    _terminate_and_reap,
    _terminate_process,
    _unlink_best_effort,
    _validate_execution_timeout,
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


class TestGdbCmdPrefixParsing:
    """Regression tests for gdb-prefix shell-quote parsing (issue #756)."""

    @staticmethod
    def _capture_argv(monkeypatch) -> list[list[str]]:
        captured: list[list[str]] = []

        def fake_run(argv, timeout, *, additional_process_group_file=None):
            del timeout, additional_process_group_file
            captured.append(list(argv))
            return 0

        monkeypatch.setattr("hephaestus.forensics.gdb_runner._run_bounded", fake_run)
        return captured

    def test_prefix_none_yields_no_prefix_tokens(self, monkeypatch, tmp_path) -> None:
        captured = self._capture_argv(monkeypatch)
        run_under_gdb(str(tmp_path / "cores"), "sh", ["-c", "true"], gdb_cmd_prefix=None)
        assert captured, "_run_bounded was not invoked"
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

    def test_direct_option_bypasses_gdb(self, monkeypatch) -> None:
        """--direct executes the command directly and returns its code."""
        monkeypatch.setenv("RUN_UNDER_GDB", "1")
        rc = main(["--direct", "/tmp/unused-core-dir", "sh", "-c", "exit 0"])
        assert rc == 0

    def test_direct_option_propagates_nonzero(self, monkeypatch) -> None:
        """--direct propagates the command's non-zero exit code."""
        monkeypatch.setenv("RUN_UNDER_GDB", "1")
        rc = main(["--direct", "/tmp/unused-core-dir", "sh", "-c", "exit 3"])
        assert rc == 3

    def test_direct_option_json_envelope(self, monkeypatch, capsys) -> None:
        """--direct with --json emits a status envelope."""
        import json

        monkeypatch.setenv("RUN_UNDER_GDB", "1")
        rc = main(["--json", "--direct", "/tmp/unused-core-dir", "sh", "-c", "exit 0"])
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


class TestValidateGdbCmdPrefix:
    """Tests for explicit gdb command-prefix whitelist validation."""

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
        rejected by shlex itself (re-raised with an option-named message);
        the remaining cases survive tokenization but carry shell metacharacters
        outside the whitelist.
        """
        with pytest.raises(ValueError, match="gdb-cmd-prefix"):
            _validate_gdb_cmd_prefix(raw)


class TestRunUnderGdbPrefixValidation:
    """run_under_gdb surfaces the prefix-validation error to callers."""

    def test_unsafe_prefix_raises_before_subprocess(self, tmp_path: Path) -> None:
        """Hoisted validation fires before resolve_command for unsafe prefix."""
        with pytest.raises(ValueError, match="gdb-cmd-prefix"):
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

    def test_main_returns_2_on_unsafe_prefix_option(self, capsys, tmp_path: Path) -> None:
        """main() returns 2 and prints ERROR for an invalid prefix option."""
        rc = main(
            [
                "--gdb-cmd-prefix=--init-eval-command=run",
                str(tmp_path / "cores"),
                _UNRESOLVABLE_CMD,
            ]
        )
        captured = capsys.readouterr()
        assert rc == 2
        assert "[run-under-gdb] ERROR:" in captured.err
        assert "gdb-cmd-prefix" in captured.err

    def test_main_json_envelope_on_unsafe_prefix_option(self, capsys, tmp_path: Path) -> None:
        """main() emits a JSON status envelope with status != ok on invalid prefix."""
        import json

        rc = main(
            [
                "--json",
                "--gdb-cmd-prefix=--init-eval-command=run",
                str(tmp_path / "cores"),
                _UNRESOLVABLE_CMD,
            ]
        )
        assert rc == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] != "ok"
        assert "gdb-cmd-prefix" in payload["message"]

    def test_main_safe_prefix_option_unchanged(self, monkeypatch, tmp_path: Path) -> None:
        """The explicit prefix wins while a poison environment value is ignored."""
        monkeypatch.setenv("GDB_CMD_PREFIX", "--init-eval-command=poison")
        rc = main(
            [
                "--gdb-cmd-prefix",
                "uv run --",
                str(tmp_path / "cores"),
                _UNRESOLVABLE_CMD,
            ]
        )
        assert rc == 127


class TestTimeoutAndCleanup:
    """Tests for timeout parsing and bounded process cleanup."""

    @pytest.mark.parametrize("timeout", [1, 7_200, 86_400])
    def test_validate_execution_timeout_accepts_bounds(self, timeout: int) -> None:
        """The execution timeout accepts both limits and an interior value."""
        assert _validate_execution_timeout(timeout) == timeout

    @pytest.mark.parametrize("timeout", [0, -1, 86_401])
    def test_validate_execution_timeout_rejects_out_of_range(self, timeout: int) -> None:
        """The execution timeout rejects values outside the documented range."""
        with pytest.raises(ValueError, match="between 1 and 86400"):
            _validate_execution_timeout(timeout)

    @pytest.mark.parametrize(
        ("raw", "message"),
        [("text", "integer"), ("0", "between 1 and 86400"), ("86401", "between 1 and 86400")],
    )
    def test_parse_execution_timeout_translates_cli_errors(self, raw: str, message: str) -> None:
        """The argument parser receives a stable error for type and range failures."""
        with pytest.raises(argparse.ArgumentTypeError, match=message):
            _parse_execution_timeout(raw)

    def test_read_process_group_handles_absent_missing_and_nonpositive(
        self, tmp_path: Path
    ) -> None:
        """Process-group recovery returns only a recorded positive integer."""
        path = tmp_path / "pgid"
        assert _read_process_group(None) is None
        assert _read_process_group(path) is None
        path.write_text("0\n", encoding="utf-8")
        assert _read_process_group(path) is None
        path.write_text("42\n", encoding="utf-8")
        assert _read_process_group(path) == 42

    def test_unlink_best_effort_removes_files_and_suppresses_errors(self, tmp_path: Path) -> None:
        """Artifact cleanup removes normal paths and does not mask an unlink failure."""
        path = tmp_path / "artifact"
        path.write_text("data", encoding="utf-8")
        broken = MagicMock(spec=Path)
        broken.unlink.side_effect = OSError("busy")
        _unlink_best_effort(path, broken)
        assert not path.exists()
        broken.unlink.assert_called_once_with(missing_ok=True)

    def test_terminate_process_kills_additional_and_primary_groups(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """POSIX cleanup kills the recorded inferior group and the wrapper group."""
        pgid_file = tmp_path / "pgid"
        pgid_file.write_text("99", encoding="utf-8")
        process = MagicMock(pid=41)
        killpg = MagicMock()
        monkeypatch.setattr(gdb_runner, "_PROCESS_GROUPS_SUPPORTED", True)
        monkeypatch.setattr("hephaestus.forensics.gdb_runner.os.killpg", killpg)
        _terminate_process(process, additional_process_group_file=pgid_file)
        assert [call.args[0] for call in killpg.call_args_list] == [99, 41]
        process.kill.assert_not_called()

    def test_terminate_process_falls_back_when_primary_group_is_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing primary group makes cleanup kill the direct child."""
        process = MagicMock(pid=41)
        monkeypatch.setattr(gdb_runner, "_PROCESS_GROUPS_SUPPORTED", True)
        monkeypatch.setattr(
            "hephaestus.forensics.gdb_runner.os.killpg",
            MagicMock(side_effect=ProcessLookupError),
        )
        _terminate_process(process)
        process.kill.assert_called_once_with()

    def test_terminate_process_uses_direct_child_without_process_groups(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Platforms without process groups terminate the direct child."""
        process = MagicMock(pid=41)
        monkeypatch.setattr(gdb_runner, "_PROCESS_GROUPS_SUPPORTED", False)
        _terminate_process(process)
        process.kill.assert_called_once_with()

    def test_terminate_and_reap_returns_after_first_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cleanup returns when the first bounded reap confirms termination."""
        process = MagicMock()
        terminate = MagicMock()
        monkeypatch.setattr(gdb_runner, "_terminate_process", terminate)
        _terminate_and_reap(process)
        terminate.assert_called_once_with(process, additional_process_group_file=None)
        process.wait.assert_called_once_with(timeout=5)

    def test_terminate_and_reap_retries_after_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cleanup terminates again and performs a final nonblocking reap."""
        process = MagicMock()
        process.wait.side_effect = [subprocess.TimeoutExpired(["cmd"], 5), 0]
        terminate = MagicMock()
        monkeypatch.setattr(gdb_runner, "_terminate_process", terminate)
        _terminate_and_reap(process)
        assert terminate.call_count == 2
        assert [
            call.kwargs["additional_process_group_file"] for call in terminate.call_args_list
        ] == [
            None,
            None,
        ]
        assert [call.kwargs["timeout"] for call in process.wait.call_args_list] == [5, 0]

    def test_terminate_and_reap_reports_unconfirmed_cleanup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two reap timeouts produce an explicit cleanup failure with the final cause."""
        process = MagicMock()
        first = subprocess.TimeoutExpired(["cmd"], 5)
        final = subprocess.TimeoutExpired(["cmd"], 0)
        process.wait.side_effect = [first, final]
        monkeypatch.setattr(gdb_runner, "_terminate_process", MagicMock())
        with pytest.raises(RuntimeError, match="termination was not confirmed") as raised:
            _terminate_and_reap(process)
        assert raised.value.__cause__ is final


class TestBoundedExecution:
    """Tests for the subprocess boundary used by direct and gdb execution."""

    def test_run_bounded_returns_child_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A normally completed child returns its exact status."""
        process = MagicMock()
        process.wait.return_value = 7
        popen = MagicMock(return_value=process)
        monkeypatch.setattr("hephaestus.forensics.gdb_runner.subprocess.Popen", popen)
        monkeypatch.setattr(gdb_runner, "read_approved_parent_env", lambda: {"PATH": "/bin"})
        assert _run_bounded(["tool"], 9) == 7
        assert popen.call_args.kwargs["env"] == {"PATH": "/bin"}
        process.wait.assert_called_once_with(timeout=9)

    @pytest.mark.parametrize(
        "failure",
        [subprocess.TimeoutExpired(["tool"], 9), KeyboardInterrupt()],
        ids=("timeout", "keyboard-interrupt"),
    )
    def test_run_bounded_cleans_up_before_propagation(
        self, monkeypatch: pytest.MonkeyPatch, failure: BaseException
    ) -> None:
        """A timeout or keyboard interrupt terminates and reaps before propagation."""
        process = MagicMock()
        process.wait.side_effect = failure
        monkeypatch.setattr(
            "hephaestus.forensics.gdb_runner.subprocess.Popen", MagicMock(return_value=process)
        )
        reap = MagicMock()
        monkeypatch.setattr(gdb_runner, "_terminate_and_reap", reap)
        with pytest.raises(type(failure)):
            _run_bounded(["tool"], 9)
        reap.assert_called_once_with(process, additional_process_group_file=None)


@pytest.mark.parametrize(("recorded", "expected"), [("17", 17), ("bad", 5), (None, 5)])
def test_run_under_gdb_prefers_valid_recorded_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    recorded: str | None,
    expected: int,
) -> None:
    """The gdb wrapper uses a valid recorded status and otherwise uses gdb status."""
    monkeypatch.setattr("hephaestus.forensics.gdb_runner.time.time", lambda: 123.0)
    monkeypatch.setattr(gdb_runner, "resolve_command", lambda command: "/bin/tool")

    def fake_run(
        command: list[str],
        timeout: int,
        *,
        additional_process_group_file: Path | None = None,
    ) -> int:
        """Create the optional gdb exit receipt before returning."""
        del command, timeout, additional_process_group_file
        if recorded is not None:
            (tmp_path / "cores" / "exit-123.code").write_text(recorded, encoding="utf-8")
        return 5

    monkeypatch.setattr(gdb_runner, "_run_bounded", fake_run)
    assert run_under_gdb(str(tmp_path / "cores"), "tool", ["arg"]) == expected
    assert not (tmp_path / "cores" / "exit-123.code").exists()


@pytest.mark.parametrize("as_json", [False, True])
def test_main_translates_execution_timeout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    as_json: bool,
) -> None:
    """The CLI returns 124 and optionally emits JSON after an execution timeout."""
    monkeypatch.setattr(
        gdb_runner,
        "run_under_gdb",
        MagicMock(side_effect=subprocess.TimeoutExpired(["gdb"], 1)),
    )
    argv = [str(tmp_path), "tool"]
    if as_json:
        argv.insert(0, "--json")
    assert main(argv) == 124
    captured = capsys.readouterr()
    assert "command timed out" in captured.err
    if as_json:
        assert '"exit_code": 124' in captured.out
