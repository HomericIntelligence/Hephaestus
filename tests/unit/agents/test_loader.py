"""Tests for hephaestus.agents.loader."""

from __future__ import annotations

from pathlib import Path

from hephaestus.agents.loader import AgentInfo, find_agent_files, load_agent, load_all_agents

_VALID_FM = (
    "---\nname: test-agent\ndescription: A test\n"
    "tools: Read,Write,Edit\nmodel: sonnet\n---\n# Body\n"
)
_ORCHESTRATOR_FM = (
    "---\nname: orchestrator-main\ndescription: Orchestrates\ntools: Read\nmodel: opus\n---\n"
)
_CHIEF_FM = "---\nname: chief-architect\ndescription: Chief\ntools: Read\nmodel: opus\n---\n"
_JUNIOR_FM = "---\nname: junior-engineer\ndescription: Junior\ntools: Read\nmodel: haiku\n---\n"


def _write(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


class TestAgentInfo:
    """Tests for AgentInfo class."""

    def test_basic_attributes(self, tmp_path: Path) -> None:
        f = _write(tmp_path / "agent.md", _VALID_FM)
        fm = {
            "name": "test-agent",
            "description": "A test",
            "tools": "Read,Write",
            "model": "sonnet",
        }
        agent = AgentInfo(f, fm)
        assert agent.name == "test-agent"
        assert agent.model == "sonnet"
        assert agent.description == "A test"

    def test_get_tools_list(self, tmp_path: Path) -> None:
        f = _write(tmp_path / "agent.md", _VALID_FM)
        agent = AgentInfo(
            f,
            {"name": "x", "description": "x", "tools": "Read, Write, Edit", "model": "sonnet"},
        )
        tools = agent.get_tools_list()
        assert "Read" in tools
        assert "Write" in tools
        assert "Edit" in tools

    def test_empty_tools_list(self, tmp_path: Path) -> None:
        f = _write(tmp_path / "agent.md", "---\nname: x\n---\n")
        agent = AgentInfo(f, {"name": "x", "description": "", "tools": "", "model": ""})
        assert agent.get_tools_list() == []

    def test_level_from_frontmatter(self, tmp_path: Path) -> None:
        f = _write(tmp_path / "agent.md", "---\nname: x\nlevel: 2\n---\n")
        agent = AgentInfo(f, {"name": "x", "level": 2})
        assert agent.level == 2

    def test_chief_architect_level_0(self, tmp_path: Path) -> None:
        f = _write(tmp_path / "chief.md", _CHIEF_FM)
        agent = AgentInfo(f, {"name": "chief-architect"})
        assert agent.level == 0

    def test_orchestrator_level_1(self, tmp_path: Path) -> None:
        f = _write(tmp_path / "orch.md", _ORCHESTRATOR_FM)
        agent = AgentInfo(f, {"name": "orchestrator-main"})
        assert agent.level == 1

    def test_junior_level_5(self, tmp_path: Path) -> None:
        f = _write(tmp_path / "junior.md", _JUNIOR_FM)
        agent = AgentInfo(f, {"name": "junior-engineer"})
        assert agent.level == 5

    def test_repr(self, tmp_path: Path) -> None:
        f = _write(tmp_path / "agent.md", _VALID_FM)
        agent = AgentInfo(f, {"name": "test-agent"})
        assert "test-agent" in repr(agent)


class TestFindAgentFiles:
    """Tests for find_agent_files()."""

    def test_finds_md_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("# A")
        (tmp_path / "b.md").write_text("# B")
        (tmp_path / "not.txt").write_text("text")
        result = find_agent_files(tmp_path)
        names = [p.name for p in result]
        assert "a.md" in names
        assert "b.md" in names
        assert "not.txt" not in names

    def test_sorted_order(self, tmp_path: Path) -> None:
        for name in ["z.md", "a.md", "m.md"]:
            (tmp_path / name).write_text("# x")
        result = find_agent_files(tmp_path)
        names = [p.name for p in result]
        assert names == sorted(names)

    def test_empty_directory(self, tmp_path: Path) -> None:
        assert find_agent_files(tmp_path) == []


class TestLoadAgent:
    """Tests for load_agent()."""

    def test_valid_agent(self, tmp_path: Path) -> None:
        f = _write(tmp_path / "agent.md", _VALID_FM)
        agent = load_agent(f)
        assert agent is not None
        assert agent.name == "test-agent"

    def test_no_frontmatter_returns_none(self, tmp_path: Path) -> None:
        f = _write(tmp_path / "agent.md", "# No frontmatter\n")
        assert load_agent(f) is None

    def test_nonexistent_file_returns_none(self, tmp_path: Path) -> None:
        assert load_agent(tmp_path / "nonexistent.md") is None


class TestLoadAllAgents:
    """Tests for load_all_agents()."""

    def test_loads_multiple(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.md", _VALID_FM)
        _write(tmp_path / "b.md", _ORCHESTRATOR_FM)
        _write(tmp_path / "bad.md", "# No frontmatter")
        agents = load_all_agents(tmp_path)
        assert len(agents) == 2

    def test_empty_directory(self, tmp_path: Path) -> None:
        assert load_all_agents(tmp_path) == []
