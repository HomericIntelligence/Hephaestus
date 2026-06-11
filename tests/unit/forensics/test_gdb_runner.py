#!/usr/bin/env python3
"""Tests for the run-under-gdb command wrapper."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hephaestus.forensics.gdb_runner import (
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
    """Regression tests for GDB_CMD_PREFIX shell-quote parsing (issue #756)."""

    @staticmethod
    def _capture_argv(monkeypatch) -> list[list[str]]:
        captured: list[list[str]] = []

        def fake_run(argv, check=False):
            captured.append(list(argv))

            class _R:
                returncode = 0

            return _R()

        monkeypatch.setattr("hephaestus.forensics.gdb_runner.subprocess.run", fake_run)
        return captured

    def test_prefix_none_yields_no_prefix_tokens(self, monkeypatch, tmp_path) -> None:
        captured = self._capture_argv(monkeypatch)
        run_under_gdb(str(tmp_path / "cores"), "sh", ["-c", "true"], gdb_cmd_prefix=None)
        assert captured, "subprocess.run was not invoked"
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
            gdb_cmd_prefix="pixi run --",
        )
        argv = captured[0]
        assert argv[:3] == ["pixi", "run", "--"]
        assert argv[3] == "gdb"

    def test_single_quoted_path_with_spaces_stays_one_token(self, monkeypatch, tmp_path) -> None:
        """Regression for issue #756: '/path with space/pixi' must be ONE token."""
        captured = self._capture_argv(monkeypatch)
        run_under_gdb(
            str(tmp_path / "cores"),
            "sh",
            ["-c", "true"],
            gdb_cmd_prefix="'/path with space/pixi' run --",
        )
        argv = captured[0]
        assert argv[:3] == ["/path with space/pixi", "run", "--"]
        assert argv[3] == "gdb"

    def test_double_quoted_path_with_spaces_stays_one_token(self, monkeypatch, tmp_path) -> None:
        captured = self._capture_argv(monkeypatch)
        run_under_gdb(
            str(tmp_path / "cores"),
            "sh",
            ["-c", "true"],
            gdb_cmd_prefix='"/abs path/to/pixi" run --',
        )
        argv = captured[0]
        assert argv[:3] == ["/abs path/to/pixi", "run", "--"]

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


class TestValidateGdbCmdPrefix:
    """Tests for GDB_CMD_PREFIX whitelist validation."""

    @pytest.mark.parametrize("raw", [None, "", "   ", "\t\n  "])
    def test_empty_input_returns_empty_list(self, raw: str | None) -> None:
        """Empty, None, or whitespace-only input returns an empty list."""
        assert _validate_gdb_cmd_prefix(raw) == []

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("pixi run --", ["pixi", "run", "--"]),
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
            "pixi --bad",
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
            gdb_cmd_prefix="pixi run --",
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
        monkeypatch.setenv("GDB_CMD_PREFIX", "pixi run --")
        rc = main([str(tmp_path / "cores"), _UNRESOLVABLE_CMD])
        assert rc == 127
