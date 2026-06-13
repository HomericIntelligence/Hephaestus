"""Enforce a valid, version-controlled MCP configuration (issue #1186)."""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_mcp_json_exists_and_is_valid() -> None:
    """The project-scoped .mcp.json must exist and parse as JSON."""
    config = REPO_ROOT / ".mcp.json"
    assert config.exists(), ".mcp.json must exist and be version-controlled"
    data = json.loads(config.read_text())
    assert isinstance(data.get("mcpServers"), dict), (
        ".mcp.json must contain a 'mcpServers' object (may be empty)"
    )


def test_declared_mcp_servers_are_well_formed() -> None:
    """Every declared server must have a command (stdio) or url (http/ws)."""
    data = json.loads((REPO_ROOT / ".mcp.json").read_text())
    for name, cfg in data["mcpServers"].items():
        assert isinstance(cfg, dict), f"server {name!r} must be an object"
        has_command = isinstance(cfg.get("command"), str)
        has_url = isinstance(cfg.get("url"), str)
        assert has_command or has_url, f"MCP server {name!r} must declare a 'command' or 'url'"


def test_mcp_posture_is_documented() -> None:
    """The MCP posture must be documented in docs/mcp.md and AGENTS.md."""
    doc = REPO_ROOT / "docs" / "mcp.md"
    assert doc.exists(), "docs/mcp.md must document the MCP posture"
    assert "Model Context Protocol" in doc.read_text()
    assert "MCP" in (REPO_ROOT / "AGENTS.md").read_text()
