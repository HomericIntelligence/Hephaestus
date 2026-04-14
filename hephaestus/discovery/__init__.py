"""Filesystem discovery utilities for agents, skills, and CLAUDE.md blocks.

Provides generic, path-agnostic functions for discovering and organizing
resources from codebases.  No hardcoded paths or project-specific names.

Usage::

    from hephaestus.discovery import discover_agents, discover_skills, extract_blocks

    agents = discover_agents(Path(".claude/agents"))
    skills = discover_skills(Path(".claude/skills"))
"""

from hephaestus.discovery.agents import discover_agents, organize_agents, parse_agent_level
from hephaestus.discovery.blocks import discover_blocks, extract_blocks
from hephaestus.discovery.skills import discover_skills, get_skill_category, organize_skills

__all__ = [
    "discover_agents",
    "discover_blocks",
    "discover_skills",
    "extract_blocks",
    "get_skill_category",
    "organize_agents",
    "organize_skills",
    "parse_agent_level",
]
