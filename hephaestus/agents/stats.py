"""Agent statistics aggregation and reporting.

Analyzes agent markdown files and generates statistics about the agent
hierarchy: counts by level, tool usage frequencies, skill references,
and delegation links between agents.

Usage::

    from hephaestus.agents.stats import collect_agent_stats, format_stats_text

    agents = load_all_agents(Path(".claude/agents"))
    stats = collect_agent_stats(agents)
    print(format_stats_text(stats))
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from hephaestus.agents.loader import AgentInfo, load_all_agents

# ---------------------------------------------------------------------------
# Stats computation
# ---------------------------------------------------------------------------


def _extract_delegation_targets(agent: AgentInfo) -> list[dict[str, str]]:
    """Extract ``[text](./file.md)`` style delegation links from agent content.

    Args:
        agent: Loaded :class:`~hephaestus.agents.loader.AgentInfo`.

    Returns:
        List of ``{"target": stem, "description": link_text}`` dicts.

    """
    try:
        content = agent.file_path.read_text(encoding="utf-8")
    except OSError:
        return []
    links = re.findall(r"\[([^\]]+)\]\(\./([^)]+\.md)\)", content)
    return [
        {"target": link_file.replace(".md", ""), "description": text} for text, link_file in links
    ]


def _extract_skill_refs(agent: AgentInfo) -> list[str]:
    r"""Extract backtick-quoted skill name references from agent content.

    Matches patterns like ``\`skill-name\` skill``.

    Args:
        agent: Loaded :class:`~hephaestus.agents.loader.AgentInfo`.

    Returns:
        Deduplicated list of skill name strings.

    """
    try:
        content = agent.file_path.read_text(encoding="utf-8")
    except OSError:
        return []
    matches = re.findall(r"`([a-z0-9-]+)`\s+skill", content.lower())
    return list(set(matches))


def collect_agent_stats(agents: list[AgentInfo]) -> dict[str, Any]:
    """Aggregate statistics across a list of agents.

    Args:
        agents: Loaded agent objects (from :func:`~hephaestus.agents.loader.load_all_agents`).

    Returns:
        Stats dict with keys:

        - ``"total_agents"`` — total count
        - ``"by_level"`` — ``{level: [names]}``
        - ``"by_tool"`` — ``{tool: [agent_names]}``
        - ``"tool_frequency"`` — ``{tool: count}``
        - ``"by_skill"`` — ``{skill: [agent_names]}``
        - ``"skill_frequency"`` — ``{skill: count}``
        - ``"delegation_graph"`` — ``{agent_name: [{target, description}]}``
        - ``"agents_without_level"`` — names where level defaulted

    """
    stats: dict[str, Any] = {
        "total_agents": len(agents),
        "by_level": defaultdict(list),
        "by_tool": defaultdict(list),
        "by_skill": defaultdict(list),
        "delegation_graph": defaultdict(list),
        "tool_frequency": defaultdict(int),
        "skill_frequency": defaultdict(int),
        "agents_without_level": [],
    }

    for agent in agents:
        # Level grouping
        if "level" in agent.raw_frontmatter:
            stats["by_level"][agent.level].append(agent.name)
        else:
            stats["agents_without_level"].append(agent.name)

        # Tool usage
        for tool in agent.get_tools_list():
            stats["by_tool"][tool].append(agent.name)
            stats["tool_frequency"][tool] += 1

        # Skill references
        for skill in _extract_skill_refs(agent):
            stats["by_skill"][skill].append(agent.name)
            stats["skill_frequency"][skill] += 1

        # Delegation graph
        stats["delegation_graph"][agent.name].extend(_extract_delegation_targets(agent))

    return stats


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def format_stats_text(stats: dict[str, Any]) -> str:
    """Render stats as a plain text report.

    Args:
        stats: Dict produced by :func:`collect_agent_stats`.

    Returns:
        Multi-line text report string.

    """
    sep = "=" * 60
    lines: list[str] = [sep, "Agent System Statistics Report", sep, ""]

    lines += ["OVERVIEW", "-" * 60, f"Total Agents: {stats['total_agents']}", ""]

    lines += ["AGENTS BY LEVEL", "-" * 60]
    for level in sorted(stats["by_level"]):
        names = sorted(stats["by_level"][level])
        lines.append(f"  Level {level}: {len(names)} agent(s)")
    if stats["agents_without_level"]:
        lines.append(f"  No level: {len(stats['agents_without_level'])} agent(s)")
    lines.append("")

    lines += ["TOP TOOLS (by frequency)", "-" * 60]
    top_tools = sorted(stats["tool_frequency"].items(), key=lambda x: -x[1])[:10]
    for tool, count in top_tools:
        lines.append(f"  {tool}: {count}")
    lines.append("")

    lines += ["TOP SKILLS (by reference count)", "-" * 60]
    top_skills = sorted(stats["skill_frequency"].items(), key=lambda x: -x[1])[:10]
    if top_skills:
        for skill, count in top_skills:
            lines.append(f"  {skill}: {count}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append(sep)
    return "\n".join(lines)


def format_stats_json(stats: dict[str, Any]) -> str:
    """Render stats as a JSON string.

    Args:
        stats: Dict produced by :func:`collect_agent_stats`.

    Returns:
        JSON-formatted string.

    """
    import json

    # Convert defaultdicts to plain dicts for serialisation
    serialisable: dict[str, Any] = {
        "total_agents": stats["total_agents"],
        "by_level": {str(k): v for k, v in stats["by_level"].items()},
        "tool_frequency": dict(stats["tool_frequency"]),
        "skill_frequency": dict(stats["skill_frequency"]),
        "agents_without_level": stats["agents_without_level"],
    }
    return json.dumps(serialisable, indent=2)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """CLI entry point: generate agent statistics report.

    Returns:
        Exit code: 0 for success, 1 on error.

    """
    parser = argparse.ArgumentParser(
        description="Generate statistics for agent markdown files",
        epilog="Example: %(prog)s --agents-dir .claude/agents --format text",
    )
    parser.add_argument(
        "--agents-dir",
        type=Path,
        default=None,
        help="Path to agents directory (default: <repo-root>/.claude/agents)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write output to file instead of stdout",
    )

    args = parser.parse_args()

    if args.agents_dir is not None:
        agents_dir = args.agents_dir
    else:
        from hephaestus.utils.helpers import get_repo_root

        agents_dir = get_repo_root() / ".claude" / "agents"

    if not agents_dir.is_dir():
        print(f"ERROR: agents directory not found: {agents_dir}", file=sys.stderr)
        return 1

    agents = load_all_agents(agents_dir)
    if not agents:
        print(f"No agent files found in {agents_dir}", file=sys.stderr)
        return 1

    stats = collect_agent_stats(agents)

    output = format_stats_json(stats) if args.format == "json" else format_stats_text(stats)

    if args.output:
        args.output.write_text(output, encoding="utf-8")
        print(f"Written to {args.output}")
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
