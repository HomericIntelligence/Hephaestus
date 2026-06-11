#!/usr/bin/env python3
"""Tests for the kernel pipe-mode core_pattern handler."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from hephaestus.forensics.coredump_handler import (
    DEFAULT_MAX_BYTES,
    main,
    resolve_target_dir,
    write_core,
)


class _FakeStdin:
    """Minimal non-TTY stdin stand-in: ``isatty()`` is False, ``buffer`` reads bytes."""

    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)

    def isatty(self) -> bool:
        return False


class TestResolveTargetDir:
    """Tests for resolve_target_dir."""

    def test_first_existing_candidate_wins(self, tmp_path: Path) -> None:
        """The first candidate that already exists is selected."""
        first = tmp_path / "first"
        second = tmp_path / "second"
        second.mkdir()
        # `first` does not exist; `second` does — `second` should win.
        result = resolve_target_dir([str(first), str(second)])
        assert result == second

    def test_creates_last_candidate_when_none_exist(self, tmp_path: Path) -> None:
        """When no candidate exists, the last one is created and returned."""
        a = tmp_path / "a"
        b = tmp_path / "b" / "nested"
        result = resolve_target_dir([str(a), str(b)])
        assert result == b
        assert b.is_dir()

    def test_skips_empty_entries(self, tmp_path: Path) -> None:
        """Empty strings in the candidate list are ignored."""
        target = tmp_path / "real"
        result = resolve_target_dir(["", str(target), ""])
        assert result == target
        assert target.is_dir()

    def test_raises_on_no_usable_candidates(self) -> None:
        """An all-empty candidate list raises ValueError."""
        with pytest.raises(ValueError, match="no candidate target directories"):
            resolve_target_dir(["", ""])


class TestWriteCore:
    """Tests for write_core."""

    def test_writes_core_with_expected_filename(self, tmp_path: Path) -> None:
        """The core file is named core.<pid>.<exe>.<time>.sig<signal>."""
        cores = tmp_path / "cores"
        cores.mkdir()
        stream = io.BytesIO(b"ELF-core-bytes")
        out = write_core(
            stream,
            pid="1234",
            exe="myproc",
            crash_time="1700000000",
            signal="11",
            target_dir=cores,
        )
        assert out == cores / "core.1234.myproc.1700000000.sig11"
        assert out.read_bytes() == b"ELF-core-bytes"

    def test_respects_max_bytes_cap(self, tmp_path: Path) -> None:
        """Input beyond max_bytes is discarded so the disk cannot fill."""
        cores = tmp_path / "cores"
        cores.mkdir()
        stream = io.BytesIO(b"x" * 10_000)
        out = write_core(
            stream,
            pid="1",
            exe="p",
            crash_time="0",
            signal="6",
            target_dir=cores,
            max_bytes=4096,
        )
        assert out.stat().st_size == 4096

    def test_logs_capture_to_handler_log(self, tmp_path: Path) -> None:
        """A successful capture appends a line to handler.log next to cores/."""
        cores = tmp_path / "cores"
        cores.mkdir()
        write_core(
            io.BytesIO(b"core"),
            pid="9",
            exe="thing",
            crash_time="42",
            signal="4",
            target_dir=cores,
        )
        log = tmp_path / "handler.log"
        assert log.is_file()
        contents = log.read_text(encoding="utf-8")
        assert "wrote" in contents
        assert "signal=4" in contents
        assert "exe=thing" in contents

    def test_default_max_bytes_is_4_gib(self) -> None:
        """The documented default cap is 4 GiB."""
        assert DEFAULT_MAX_BYTES == 4 * 1024 * 1024 * 1024


class TestMainTargetDir:
    """Tests for the --target-dir CLI option and its precedence in main()."""

    def test_target_dir_option_directs_the_core_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--target-dir places the core file in the given directory."""
        cores = tmp_path / "explicit" / "cores"
        monkeypatch.setattr("sys.stdin", _FakeStdin(b"ELF"))
        rc = main(["--target-dir", str(cores), "7", "proc", "100", "11"])
        assert rc == 0
        assert (cores / "core.7.proc.100.sig11").read_bytes() == b"ELF"

    def test_target_dir_overrides_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--target-dir wins over COREDUMP_TARGET_DIRS."""
        env_dir = tmp_path / "from-env"
        env_dir.mkdir()
        cli_dir = tmp_path / "from-cli"
        monkeypatch.setenv("COREDUMP_TARGET_DIRS", str(env_dir))
        monkeypatch.setattr("sys.stdin", _FakeStdin(b"core"))
        rc = main(["--target-dir", str(cli_dir), "1", "p", "0", "6"])
        assert rc == 0
        assert (cli_dir / "core.1.p.0.sig6").is_file()
        # The env-var directory must NOT have received the core.
        assert not list(env_dir.iterdir())

    def test_env_var_used_when_no_target_dir_option(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --target-dir, COREDUMP_TARGET_DIRS is honored."""
        env_dir = tmp_path / "env-cores"
        monkeypatch.setenv("COREDUMP_TARGET_DIRS", str(env_dir))
        monkeypatch.setattr("sys.stdin", _FakeStdin(b"x"))
        rc = main(["2", "q", "0", "4"])
        assert rc == 0
        assert (env_dir / "core.2.q.0.sig4").is_file()

    def test_tty_stdin_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A TTY stdin is refused with exit code 1 (would otherwise block)."""

        class _TtyStdin:
            buffer = io.BytesIO(b"")

            def isatty(self) -> bool:
                return True

        monkeypatch.setattr("sys.stdin", _TtyStdin())
        assert main(["1", "p", "0", "6"]) == 1

    def test_tty_stdin_json_envelope(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A TTY stdin with --json emits an error envelope."""
        import json

        class _TtyStdin:
            buffer = io.BytesIO(b"")

            def isatty(self) -> bool:
                return True

        monkeypatch.setattr("sys.stdin", _TtyStdin())
        assert main(["--json", "1", "p", "0", "6"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert "TTY" in payload["message"]

    def test_success_json_envelope(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Successful write emits an ok envelope with the core file path."""
        import json

        cores = tmp_path / "cores"
        monkeypatch.setattr("sys.stdin", _FakeStdin(b"ELFXX"))
        rc = main(["--json", "--target-dir", str(cores), "7", "proc", "100", "11"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["message"] == "core written"
        assert payload["path"].endswith("core.7.proc.100.sig11")

    def test_max_bytes_env_var_respected_via_main(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """COREDUMP_MAX_BYTES is parsed by main()."""
        cores = tmp_path / "cores"
        monkeypatch.setenv("COREDUMP_MAX_BYTES", "3")
        monkeypatch.setattr("sys.stdin", _FakeStdin(b"ABCDEFG"))
        rc = main(["--target-dir", str(cores), "1", "p", "0", "6"])
        assert rc == 0
        # Cap of 3 bytes should clip output to 3 bytes.
        written = (cores / "core.1.p.0.sig6").read_bytes()
        assert len(written) == 3


class TestParseMaxBytes:
    """Unit tests for ``_parse_max_bytes``."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1024", 1024),
            ("  42  ", 42),
            ("4G", 4 * 1024**3),
            ("500M", 500 * 1024**2),
            ("2GiB", 2 * 1024**3),
            ("1k", 1024),
            ("1KB", 1024),
        ],
    )
    def test_valid_inputs(self, raw: str, expected: int) -> None:
        from hephaestus.forensics.coredump_handler import _parse_max_bytes

        assert _parse_max_bytes(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "abc", "4X", "-1", "0", "4G500M", "1.5G", "4 G B"],
    )
    def test_invalid_inputs_return_none(self, raw: str) -> None:
        from hephaestus.forensics.coredump_handler import _parse_max_bytes

        assert _parse_max_bytes(raw) is None


class TestMaxBytesEnvFallback:
    """``main()`` env-var error handling."""

    def test_malformed_env_falls_back_and_logs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Malformed COREDUMP_MAX_BYTES logs to handler.log and uses default."""
        cores = tmp_path / "cores"
        monkeypatch.setenv("COREDUMP_MAX_BYTES", "4X")
        monkeypatch.setattr("sys.stdin", _FakeStdin(b"ELF-bytes"))
        rc = main(["--target-dir", str(cores), "1", "p", "0", "6"])
        assert rc == 0
        # Core is still written (default cap is generous).
        assert (cores / "core.1.p.0.sig6").read_bytes() == b"ELF-bytes"
        # Failure is recorded in handler.log next to the cores dir.
        log_text = (tmp_path / "handler.log").read_text(encoding="utf-8")
        assert "COREDUMP_MAX_BYTES=" in log_text
        assert "'4X'" in log_text
        assert "falling back to default" in log_text

    def test_suffix_env_var_parsed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """COREDUMP_MAX_BYTES='4K' caps at 4096 bytes."""
        cores = tmp_path / "cores"
        monkeypatch.setenv("COREDUMP_MAX_BYTES", "4K")
        monkeypatch.setattr("sys.stdin", _FakeStdin(b"A" * 8192))
        rc = main(["--target-dir", str(cores), "1", "p", "0", "6"])
        assert rc == 0
        written = (cores / "core.1.p.0.sig6").read_bytes()
        assert len(written) == 4096

    def test_whitespace_only_env_uses_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Whitespace-only COREDUMP_MAX_BYTES is treated as unset, no warning."""
        cores = tmp_path / "cores"
        monkeypatch.setenv("COREDUMP_MAX_BYTES", "   ")
        monkeypatch.setattr("sys.stdin", _FakeStdin(b"X"))
        rc = main(["--target-dir", str(cores), "1", "p", "0", "6"])
        assert rc == 0
        log_path = tmp_path / "handler.log"
        if log_path.exists():
            assert "COREDUMP_MAX_BYTES" not in log_path.read_text(encoding="utf-8")
