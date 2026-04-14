"""Tests for hephaestus.discovery.blocks."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.discovery.blocks import discover_blocks, extract_blocks

_SAMPLE_MD = "Line 1\nLine 2\nLine 3\nLine 4\nLine 5\n"
_BLOCK_DEFS = [("B01", 1, 2, "B01-first.md"), ("B02", 4, 5, "B02-last.md")]


class TestDiscoverBlocks:
    """Tests for discover_blocks()."""

    def test_returns_provided_defs(self, tmp_path: Path) -> None:
        f = tmp_path / "CLAUDE.md"
        f.write_text(_SAMPLE_MD)
        result = discover_blocks(f, _BLOCK_DEFS)
        assert result == _BLOCK_DEFS

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            discover_blocks(tmp_path / "nonexistent.md", _BLOCK_DEFS)

    def test_no_defs_raises_value_error(self, tmp_path: Path) -> None:
        f = tmp_path / "CLAUDE.md"
        f.write_text(_SAMPLE_MD)
        with pytest.raises(ValueError, match="block_defs must be provided"):
            discover_blocks(f, None)


class TestExtractBlocks:
    """Tests for extract_blocks()."""

    def test_creates_output_dir(self, tmp_path: Path) -> None:
        src = tmp_path / "CLAUDE.md"
        src.write_text(_SAMPLE_MD)
        out = tmp_path / "blocks"
        extract_blocks(src, out, _BLOCK_DEFS)
        assert out.is_dir()

    def test_creates_block_files(self, tmp_path: Path) -> None:
        src = tmp_path / "CLAUDE.md"
        src.write_text(_SAMPLE_MD)
        out = tmp_path / "blocks"
        created = extract_blocks(src, out, _BLOCK_DEFS)
        assert len(created) == 2
        for path in created:
            assert path.exists()

    def test_block_content_correct(self, tmp_path: Path) -> None:
        src = tmp_path / "CLAUDE.md"
        src.write_text(_SAMPLE_MD)
        out = tmp_path / "blocks"
        extract_blocks(src, out, _BLOCK_DEFS)
        b01 = (out / "B01-first.md").read_text()
        assert b01 == "Line 1\nLine 2\n"
        b02 = (out / "B02-last.md").read_text()
        assert b02 == "Line 4\nLine 5\n"

    def test_returns_paths(self, tmp_path: Path) -> None:
        src = tmp_path / "CLAUDE.md"
        src.write_text(_SAMPLE_MD)
        out = tmp_path / "blocks"
        result = extract_blocks(src, out, _BLOCK_DEFS)
        assert all(isinstance(p, Path) for p in result)

    def test_no_defs_raises(self, tmp_path: Path) -> None:
        src = tmp_path / "CLAUDE.md"
        src.write_text(_SAMPLE_MD)
        with pytest.raises(ValueError):
            extract_blocks(src, tmp_path / "out", None)
