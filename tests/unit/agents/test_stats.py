"""Tests for hephaestus.agents.stats."""

from __future__ import annotations

from pathlib import Path

from hephaestus.agents.loader import AgentInfo
from hephaestus.agents.stats import (
    _extract_delegation_targets,
    _extract_skill_refs,
    collect_agent_stats,
    format_stats_json,
    format_stats_text,
)

_VALID_FM = (
    "---\nname: test-agent\ndescription: A test\n"
    "tools: Read,Write,Edit\nmodel: sonnet\n---\n# Body\n"
)
_ORCHESTRATOR_FM = (
    "---\nname: orchestrator-main\ndescription: Orchestrates\ntools: Read\nmodel: opus\n---\n"
)
_CHIEF_FM = "---\nname: chief-architect\ndescription: Chief\ntools: Read\nmodel: opus\n---\n"


def _make_agent(path: Path, content: str, frontmatter: dict) -> AgentInfo:
    path.write_text(content)
    return AgentInfo(path, frontmatter)


class TestExtractDelegationTargets:
    """Tests for _extract_delegation_targets()."""

    def test_extracts_links(self, tmp_path: Path) -> None:
        content = "---\nname: x\n---\nSee [specialist](./specialist-agent.md).\n"
        agent = _make_agent(tmp_path / "a.md", content, {"name": "x"})
        targets = _extract_delegation_targets(agent)
        assert len(targets) == 1
        assert targets[0]["target"] == "specialist-agent"
        assert targets[0]["description"] == "specialist"

    def test_no_links(self, tmp_path: Path) -> None:
        agent = _make_agent(tmp_path / "a.md", _VALID_FM, {"name": "test-agent"})
        assert _extract_delegation_targets(agent) == []

    def test_multiple_links(self, tmp_path: Path) -> None:
        content = "---\nname: x\n---\nDelegates to [alpha](./alpha.md) and [beta](./beta.md).\n"
        agent = _make_agent(tmp_path / "a.md", content, {"name": "x"})
        targets = _extract_delegation_targets(agent)
        assert len(targets) == 2
        names = {t["target"] for t in targets}
        assert names == {"alpha", "beta"}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        agent = AgentInfo(tmp_path / "nonexistent.md", {"name": "x"})
        assert _extract_delegation_targets(agent) == []


class TestExtractSkillRefs:
    """Tests for _extract_skill_refs()."""

    def test_extracts_skill_names(self, tmp_path: Path) -> None:
        content = "---\nname: x\n---\nUse the `commit` skill to commit changes.\n"
        agent = _make_agent(tmp_path / "a.md", content, {"name": "x"})
        skills = _extract_skill_refs(agent)
        assert "commit" in skills

    def test_no_skills_returns_empty(self, tmp_path: Path) -> None:
        agent = _make_agent(tmp_path / "a.md", _VALID_FM, {"name": "test-agent"})
        assert _extract_skill_refs(agent) == []

    def test_deduplicates(self, tmp_path: Path) -> None:
        content = "---\nname: x\n---\nUse `commit` skill here and `commit` skill there.\n"
        agent = _make_agent(tmp_path / "a.md", content, {"name": "x"})
        skills = _extract_skill_refs(agent)
        assert skills.count("commit") == 1

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        agent = AgentInfo(tmp_path / "nonexistent.md", {"name": "x"})
        assert _extract_skill_refs(agent) == []


class TestCollectAgentStats:
    """Tests for collect_agent_stats()."""

    def test_empty_list(self) -> None:
        stats = collect_agent_stats([])
        assert stats["total_agents"] == 0
        assert stats["agents_without_level"] == []

    def test_total_count(self, tmp_path: Path) -> None:
        agents = [
            _make_agent(tmp_path / "a.md", _VALID_FM, {"name": "a", "tools": "Read"}),
            _make_agent(
                tmp_path / "b.md", _ORCHESTRATOR_FM, {"name": "orchestrator-main", "tools": "Read"}
            ),
        ]
        stats = collect_agent_stats(agents)
        assert stats["total_agents"] == 2

    def test_by_level_grouping(self, tmp_path: Path) -> None:
        agents = [
            _make_agent(tmp_path / "c.md", _CHIEF_FM, {"name": "chief-architect", "level": 0}),
            _make_agent(
                tmp_path / "o.md", _ORCHESTRATOR_FM, {"name": "orchestrator-main", "level": 1}
            ),
        ]
        stats = collect_agent_stats(agents)
        assert "chief-architect" in stats["by_level"][0]
        assert "orchestrator-main" in stats["by_level"][1]

    def test_agents_without_level(self, tmp_path: Path) -> None:
        agent = _make_agent(tmp_path / "a.md", _VALID_FM, {"name": "test-agent"})
        stats = collect_agent_stats([agent])
        assert "test-agent" in stats["agents_without_level"]

    def test_agents_with_explicit_level_not_in_without_level(self, tmp_path: Path) -> None:
        agent = _make_agent(
            tmp_path / "a.md",
            "---\nname: x\nlevel: 3\n---\n",
            {"name": "x", "level": 3},
        )
        stats = collect_agent_stats([agent])
        assert "x" not in stats["agents_without_level"]
        assert "x" in stats["by_level"][3]

    def test_tool_frequency(self, tmp_path: Path) -> None:
        agents = [
            _make_agent(tmp_path / "a.md", _VALID_FM, {"name": "a", "tools": "Read,Write"}),
            _make_agent(tmp_path / "b.md", _ORCHESTRATOR_FM, {"name": "b", "tools": "Read"}),
        ]
        stats = collect_agent_stats(agents)
        assert stats["tool_frequency"]["Read"] == 2
        assert stats["tool_frequency"]["Write"] == 1

    def test_by_tool_entries(self, tmp_path: Path) -> None:
        agents = [
            _make_agent(tmp_path / "a.md", _VALID_FM, {"name": "a", "tools": "Read"}),
        ]
        stats = collect_agent_stats(agents)
        assert "a" in stats["by_tool"]["Read"]

    def test_skill_frequency(self, tmp_path: Path) -> None:
        content = "---\nname: a\n---\nUse the `commit` skill.\n"
        agent = _make_agent(tmp_path / "a.md", content, {"name": "a", "tools": ""})
        stats = collect_agent_stats([agent])
        assert stats["skill_frequency"].get("commit", 0) >= 1

    def test_delegation_graph(self, tmp_path: Path) -> None:
        content = "---\nname: a\n---\nDelegate to [worker](./worker.md).\n"
        agent = _make_agent(tmp_path / "a.md", content, {"name": "a", "tools": ""})
        stats = collect_agent_stats([agent])
        assert len(stats["delegation_graph"]["a"]) == 1
        assert stats["delegation_graph"]["a"][0]["target"] == "worker"


class TestFormatStatsText:
    """Tests for format_stats_text()."""

    def _make_stats(self, tmp_path: Path) -> dict:
        agents = [
            _make_agent(tmp_path / "c.md", _CHIEF_FM, {"name": "chief-architect"}),
            _make_agent(
                tmp_path / "a.md", _VALID_FM, {"name": "test-agent", "tools": "Read,Write"}
            ),
        ]
        return collect_agent_stats(agents)

    def test_contains_overview(self, tmp_path: Path) -> None:
        stats = self._make_stats(tmp_path)
        text = format_stats_text(stats)
        assert "Total Agents" in text
        assert "2" in text

    def test_contains_level_section(self, tmp_path: Path) -> None:
        stats = self._make_stats(tmp_path)
        text = format_stats_text(stats)
        assert "AGENTS BY LEVEL" in text

    def test_contains_tools_section(self, tmp_path: Path) -> None:
        stats = self._make_stats(tmp_path)
        text = format_stats_text(stats)
        assert "TOP TOOLS" in text

    def test_contains_skills_section(self, tmp_path: Path) -> None:
        stats = self._make_stats(tmp_path)
        text = format_stats_text(stats)
        assert "TOP SKILLS" in text

    def test_none_skills_placeholder(self, tmp_path: Path) -> None:
        agents = [_make_agent(tmp_path / "a.md", _VALID_FM, {"name": "a", "tools": ""})]
        stats = collect_agent_stats(agents)
        text = format_stats_text(stats)
        assert "(none)" in text

    def test_returns_string(self, tmp_path: Path) -> None:
        stats = self._make_stats(tmp_path)
        assert isinstance(format_stats_text(stats), str)


class TestFormatStatsJson:
    """Tests for format_stats_json()."""

    def test_valid_json(self, tmp_path: Path) -> None:
        import json

        agents = [_make_agent(tmp_path / "a.md", _VALID_FM, {"name": "a", "tools": "Read"})]
        stats = collect_agent_stats(agents)
        result = format_stats_json(stats)
        parsed = json.loads(result)
        assert "total_agents" in parsed

    def test_contains_tool_frequency(self, tmp_path: Path) -> None:
        import json

        agents = [_make_agent(tmp_path / "a.md", _VALID_FM, {"name": "a", "tools": "Read"})]
        stats = collect_agent_stats(agents)
        result = format_stats_json(stats)
        parsed = json.loads(result)
        assert "tool_frequency" in parsed

    def test_by_level_keys_are_strings(self, tmp_path: Path) -> None:
        import json

        agents = [_make_agent(tmp_path / "c.md", _CHIEF_FM, {"name": "chief-architect"})]
        stats = collect_agent_stats(agents)
        result = format_stats_json(stats)
        parsed = json.loads(result)
        for key in parsed["by_level"]:
            assert isinstance(key, str)

    def test_agents_without_level_included(self, tmp_path: Path) -> None:
        import json

        agents = [_make_agent(tmp_path / "a.md", _VALID_FM, {"name": "test-agent"})]
        stats = collect_agent_stats(agents)
        result = format_stats_json(stats)
        parsed = json.loads(result)
        assert "test-agent" in parsed["agents_without_level"]
