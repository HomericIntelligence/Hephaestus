"""Enforce the MCP configuration and integration posture (#1186, #2163)."""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_CONFIG = REPO_ROOT / ".mcp.json"
MCP_DOC = REPO_ROOT / "docs" / "mcp.md"
MCP_ADR = REPO_ROOT / "docs" / "adr" / "0011-mcp-integration-posture.md"

REQUIRED_POSTURE_TEXT = (
    "## Capability boundary",
    "## Alternative integration contracts",
    "## Runtime and failure boundary",
    "## Adding a server",
    "optional project-scoped agent tooling",
    "must not depend on an MCP server",
)
ALTERNATIVE_INTEGRATIONS = (
    "Plugin marketplaces",
    "NATS JetStream",
    "HTTP REST",
)


def test_mcp_json_exists_and_is_intentionally_empty() -> None:
    """The explicit project configuration must reflect the accepted posture."""
    assert MCP_CONFIG.exists(), ".mcp.json must exist and be version-controlled"
    data = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
    assert data.get("mcpServers") == {}, (
        "mcpServers must remain empty until an approved server proposal "
        "updates the posture documentation and tests"
    )


def test_declared_mcp_servers_are_well_formed() -> None:
    """Every future declared server must have a command or url."""
    data = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
    for name, cfg in data["mcpServers"].items():
        assert isinstance(cfg, dict), f"server {name!r} must be an object"
        has_command = isinstance(cfg.get("command"), str)
        has_url = isinstance(cfg.get("url"), str)
        assert has_command or has_url, f"MCP server {name!r} must declare a 'command' or 'url'"


def test_mcp_posture_is_documented_and_indexed() -> None:
    """The guide, agent map, and ADR must preserve the same MCP boundary."""
    assert MCP_DOC.exists()
    assert MCP_ADR.exists()

    doc = MCP_DOC.read_text(encoding="utf-8")
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    adr = MCP_ADR.read_text(encoding="utf-8")
    docs_index = (REPO_ROOT / "docs" / "index.md").read_text(encoding="utf-8")

    for marker in REQUIRED_POSTURE_TEXT:
        assert marker in doc, f"docs/mcp.md missing posture marker: {marker!r}"
    for integration in ALTERNATIVE_INTEGRATIONS:
        assert integration in doc
        assert integration in agents

    assert "- Tracks: #2163" in adr
    assert "ADR-0011" in doc
    assert "ADR-0011" in agents
    assert "[MCP Integration Posture](mcp.md)" in docs_index
