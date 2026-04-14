"""Tests for hephaestus.discovery.agents."""

from __future__ import annotations

from pathlib import Path

from hephaestus.discovery.agents import discover_agents, organize_agents, parse_agent_level

_FM_LEVEL_2 = "---\nname: design-agent\nlevel: 2\ntools: Read\nmodel: sonnet\n---\n"
_FM_NO_LEVEL = "---\nname: unknown-agent\ntools: Read\nmodel: sonnet\n---\n"


class TestParseAgentLevel:
    """Tests for parse_agent_level()."""

    def test_extracts_level(self, tmp_path: Path) -> None:
        f = tmp_path / "a.md"
        f.write_text(_FM_LEVEL_2)
        assert parse_agent_level(f) == 2

    def test_no_level_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "a.md"
        f.write_text(_FM_NO_LEVEL)
        assert parse_agent_level(f) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert parse_agent_level(tmp_path / "nonexistent.md") is None

    def test_level_zero(self, tmp_path: Path) -> None:
        f = tmp_path / "a.md"
        f.write_text("---\nname: chief\nlevel: 0\n---\n")
        assert parse_agent_level(f) == 0


class TestDiscoverAgents:
    """Tests for discover_agents()."""

    def test_classifies_by_level(self, tmp_path: Path) -> None:
        (tmp_path / "chief.md").write_text("---\nname: chief\nlevel: 0\n---\n")
        (tmp_path / "design.md").write_text(_FM_LEVEL_2)
        result = discover_agents(tmp_path)
        assert len(result[0]) == 1
        assert len(result[2]) == 1

    def test_agents_without_level_excluded(self, tmp_path: Path) -> None:
        (tmp_path / "unlevel.md").write_text(_FM_NO_LEVEL)
        result = discover_agents(tmp_path)
        assert all(len(v) == 0 for v in result.values())

    def test_empty_directory(self, tmp_path: Path) -> None:
        result = discover_agents(tmp_path)
        assert all(len(v) == 0 for v in result.values())
        assert set(result.keys()) == set(range(6))

    def test_out_of_range_level_excluded(self, tmp_path: Path) -> None:
        (tmp_path / "bad.md").write_text("---\nname: x\nlevel: 99\n---\n")
        result = discover_agents(tmp_path)
        assert all(len(v) == 0 for v in result.values())


class TestOrganizeAgents:
    """Tests for organize_agents()."""

    def test_creates_level_dirs(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        dst = tmp_path / "dst"
        (src / "a.md").write_text("---\nname: a\nlevel: 1\n---\n")
        organize_agents(src, dst)
        assert (dst / "L1").is_dir()

    def test_copies_agent_file(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        dst = tmp_path / "dst"
        (src / "a.md").write_text("---\nname: a\nlevel: 1\n---\n")
        result = organize_agents(src, dst)
        assert "a.md" in result[1]
        assert (dst / "L1" / "a.md").exists()

    def test_returns_stats(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        dst = tmp_path / "dst"
        result = organize_agents(src, dst)
        assert set(result.keys()) == set(range(6))
