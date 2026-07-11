# Model Context Protocol (MCP) Configuration

Hephaestus ships a project-scoped `.mcp.json` at the repository root
with an empty `mcpServers` map. No MCP servers are configured yet — the file
exists so the configuration surface is explicit and version-controlled for the
whole team, per the Claude Code project-scope convention.

## Why the ecosystem references but does not use MCP

The HomericIntelligence ecosystem integrates through mechanisms that are *not*
MCP servers:

- **Claude Code plugin marketplaces** — e.g. the Mnemosyne marketplace the
  `learn` skill writes to (see `AGENTS.md`). A marketplace is a plugin source,
  not an MCP server.
- **NATS JetStream** — event-driven workflows in `hephaestus/nats/`.
- **HTTP REST** — Agamemnon agent-management and Hermes message routing.

None of these is wired through MCP, which is why `mcpServers` is empty today.

## Startup behaviour

Adding a server here is safe even if its endpoint is unreachable. MCP startup
is non-blocking by default: unreachable servers connect in the background, and
Claude Code prompts for approval before first use of any project-scoped server.
Only a server marked `alwaysLoad: true` blocks startup, and only up to a
5-second connect timeout.

## Adding a server

Add an entry under `mcpServers` in `.mcp.json`. A stdio server uses `command`
plus `args`; an HTTP server uses `type: "http"` and `url`. Example entry:

```json
{
  "mcpServers": {
    "example": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-example"],
      "env": {}
    }
  }
}
```

Commit the change so every team member gets the same server. Run
`claude mcp list` to confirm the server is picked up (project-scoped servers
awaiting approval show as `⏸ Pending approval`).
