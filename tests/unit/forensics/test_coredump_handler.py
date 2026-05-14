#!/usr/bin/env python3
"""Tests for the kernel pipe-mode core_pattern handler."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from hephaestus.forensics.coredump_handler import (
    DEFAULT_MAX_BYTES,
    resolve_target_dir,
    write_core,
)


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
