"""Agent markdown file utilities: frontmatter parsing, loading, and statistics."""

from hephaestus.agents.frontmatter import (
    FRONTMATTER_PATTERN,
    check_agent_file,
    extract_frontmatter_full,
    extract_frontmatter_parsed,
    extract_frontmatter_raw,
    extract_frontmatter_with_lines,
    validate_agents_main,
    validate_frontmatter,
)
from hephaestus.agents.loader import (
    AgentInfo,
    find_agent_files,
    load_agent,
    load_all_agents,
)
from hephaestus.agents.stats import (
    collect_agent_stats,
    format_stats_json,
    format_stats_text,
)

__all__ = [
    "FRONTMATTER_PATTERN",
    "AgentInfo",
    "check_agent_file",
    "collect_agent_stats",
    "extract_frontmatter_full",
    "extract_frontmatter_parsed",
    "extract_frontmatter_raw",
    "extract_frontmatter_with_lines",
    "find_agent_files",
    "format_stats_json",
    "format_stats_text",
    "load_agent",
    "load_all_agents",
    "validate_agents_main",
    "validate_frontmatter",
]
