"""Agent markdown file discovery and loading primitives.

Provides a generic :class:`AgentInfo` data class and functions for discovering
and loading agent markdown files from a directory.  No hardcoded path
assumptions — callers supply the agents directory.

Usage::

    from hephaestus.agents.loader import load_all_agents, AgentInfo

    agents = load_all_agents(Path(".claude/agents"))
    for agent in agents:
        print(agent.name, agent.level, agent.get_tools_list())
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hephaestus.agents.frontmatter import extract_frontmatter_raw

try:
    import yaml as _yaml
except ModuleNotFoundError:
    _yaml = None  # type: ignore[assignment]


class AgentInfo:
    """Metadata container for a single agent markdown file.

    Attributes:
        file_path: Path to the agent markdown file.
        name: Agent name from frontmatter.
        description: Agent description from frontmatter.
        tools: Comma-separated tools string from frontmatter.
        model: Model assignment (e.g. ``"sonnet"``, ``"opus"``, ``"haiku"``).
        level: Inferred or explicit agent level (0 = orchestrator, 5 = junior).
        raw_frontmatter: The original frontmatter dict.

    """

    def __init__(self, file_path: Path, frontmatter: dict[str, Any]) -> None:
        """Initialise from path and parsed frontmatter dict.

        Args:
            file_path: Path to the markdown file.
            frontmatter: Parsed YAML frontmatter dictionary.

        """
        self.file_path = file_path
        self.raw_frontmatter = frontmatter
        self.name: str = frontmatter.get("name", "unknown")
        self.description: str = frontmatter.get("description", "No description")
        self.tools: str = frontmatter.get("tools", "")
        self.model: str = frontmatter.get("model", "unknown")
        self.level: int = self._infer_level(frontmatter)

    def _infer_level(self, frontmatter: dict[str, Any]) -> int:
        """Infer agent level from frontmatter or fall back to name heuristics.

        Level hierarchy (0 = highest, 5 = lowest):

        - 0: Meta-orchestrator
        - 1: Section orchestrators
        - 2: Design agents
        - 3: Component specialists
        - 4: Senior engineers
        - 5: Junior engineers

        Args:
            frontmatter: Parsed frontmatter dictionary.

        Returns:
            Integer level (0–5).

        """
        if "level" in frontmatter:
            return int(frontmatter["level"])

        name = self.name.lower()
        if "chief-architect" in name:
            return 0
        if "orchestrator" in name:
            return 1
        if "design" in name:
            return 2
        if "specialist" in name:
            return 3
        if "junior" in name:
            return 5
        if "senior" in name or "engineer" in name:
            return 4
        return 3  # Default to middle level

    def get_tools_list(self) -> list[str]:
        """Return individual tool names from the comma-separated tools string.

        Returns:
            List of stripped tool name strings.

        """
        if not self.tools:
            return []
        return [t.strip() for t in self.tools.split(",") if t.strip()]

    def __repr__(self) -> str:
        """Return a concise string representation."""
        return f"AgentInfo(level={self.level}, name={self.name!r})"


# ---------------------------------------------------------------------------
# Discovery and loading
# ---------------------------------------------------------------------------


def find_agent_files(agents_dir: Path) -> list[Path]:
    """Return a sorted list of ``*.md`` files in *agents_dir*.

    Args:
        agents_dir: Directory to search.

    Returns:
        Sorted list of ``.md`` file paths.

    """
    return sorted(agents_dir.glob("*.md"))


def load_agent(file_path: Path) -> AgentInfo | None:
    """Load a single agent from a markdown file.

    Args:
        file_path: Path to the agent markdown file.

    Returns:
        :class:`AgentInfo` on success, ``None`` if the file cannot be read,
        has no frontmatter, or has invalid YAML.

    """
    if _yaml is None:
        return None

    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError:
        return None

    frontmatter_text = extract_frontmatter_raw(content)
    if frontmatter_text is None:
        return None

    try:
        frontmatter = _yaml.safe_load(frontmatter_text)
    except _yaml.YAMLError:
        return None

    if not isinstance(frontmatter, dict):
        return None

    return AgentInfo(file_path, frontmatter)


def load_all_agents(agents_dir: Path) -> list[AgentInfo]:
    """Load all agent configurations from *agents_dir*.

    Args:
        agents_dir: Directory containing agent markdown files.

    Returns:
        List of :class:`AgentInfo` objects for successfully loaded agents.

    """
    agents = []
    for file_path in find_agent_files(agents_dir):
        agent = load_agent(file_path)
        if agent is not None:
            agents.append(agent)
    return agents
