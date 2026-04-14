"""Agent discovery and organisation utilities.

Generic filesystem walker for discovering agent markdown files and classifying
them by hierarchy level.  Callers supply the directory; no paths are hardcoded.

Usage::

    from hephaestus.discovery.agents import discover_agents, organize_agents

    agents = discover_agents(Path(".claude/agents"))
    # {0: [Path(...chief.md)], 1: [Path(...orchestrator.md)], ...}
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path


def parse_agent_level(file_path: Path) -> int | None:
    """Extract the level from an agent markdown file's YAML frontmatter.

    Reads only the ``level:`` key from the frontmatter; does not invoke a full
    YAML parser so no extra dependency is required.

    Args:
        file_path: Path to agent markdown file.

    Returns:
        Integer level (0–5) if found, ``None`` otherwise.

    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^level:\s*(\d+)", content, re.MULTILINE)
    return int(match.group(1)) if match else None


def discover_agents(source_dir: Path) -> dict[int, list[Path]]:
    """Scan *source_dir* and classify agent files by hierarchy level.

    Only files with an explicit ``level:`` frontmatter key are included.
    Agents without a level are silently skipped.

    Args:
        source_dir: Directory containing agent ``*.md`` files.

    Returns:
        Dict mapping level (0–5) to a sorted list of agent file paths.

    """
    result: dict[int, list[Path]] = {i: [] for i in range(6)}
    for agent_file in sorted(source_dir.glob("*.md")):
        level = parse_agent_level(agent_file)
        if level is not None and 0 <= level <= 5:
            result[level].append(agent_file)
    return result


def organize_agents(source_dir: Path, dest_dir: Path) -> dict[int, list[str]]:
    """Copy agents from *source_dir* into *dest_dir*, organised by level.

    Creates ``L0``–``L5`` subdirectories under *dest_dir* and copies each
    agent file into the appropriate sub-directory.

    Args:
        source_dir: Directory containing source agent ``*.md`` files.
        dest_dir: Destination root directory; level subdirs are created here.

    Returns:
        Dict mapping level to list of copied filenames.

    """
    for level in range(6):
        (dest_dir / f"L{level}").mkdir(parents=True, exist_ok=True)

    agents_by_level = discover_agents(source_dir)
    stats: dict[int, list[str]] = {i: [] for i in range(6)}
    for level, agent_files in agents_by_level.items():
        for agent_file in agent_files:
            dest_path = dest_dir / f"L{level}" / agent_file.name
            shutil.copy2(agent_file, dest_path)
            stats[level].append(agent_file.name)
    return stats
