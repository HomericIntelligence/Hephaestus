# Model Context Protocol (MCP) Configuration

Hephaestus ships a project-scoped `.mcp.json` at the repository root
with an empty `mcpServers` map. No MCP servers are configured yet — the file
exists so the configuration surface is explicit and version-controlled for the
whole team, per the Claude Code project-scope convention.

## Capability boundary

Hephaestus does not provide an MCP client, server, or bridge. The
project-scoped `.mcp.json` is only a host configuration surface for
developer-facing tools; it is not an ecosystem service-discovery mechanism or
a runtime integration API. Keeping `mcpServers` empty is therefore a deliberate
default, not a missing connection.

Add an MCP server only when an interactive agent host needs a standard tool
interface that the contracts below cannot provide. The server must be scoped to
the project, use the least-privileged tool and credential access, and be
documented in this file with its purpose and operator. Application or
service-to-service integrations remain outside `.mcp.json`.

## Why the ecosystem references but does not use MCP

The HomericIntelligence ecosystem integrates through mechanisms that are *not*
MCP servers:

- **Claude Code plugin marketplaces** — e.g. the Mnemosyne marketplace the
  `learn` skill writes to (see `AGENTS.md`). A marketplace is a plugin source,
  not an MCP server.
- **NATS JetStream** — event-driven workflows in `hephaestus/nats/`.
- **HTTP REST** — Agamemnon agent-management and Hermes message routing.

None of these is wired through MCP, which is why `mcpServers` is empty today.

## Alternative integration contracts

Use the contract that matches the integration's runtime role rather than adding
an MCP server by default:

- **Plugin marketplace contract** — distribute reusable agent capabilities as
  versioned plugins and skills. The marketplace owns discovery and installation;
  a plugin is not a runtime RPC endpoint.
- **NATS contract** — use `hephaestus.nats` for asynchronous event delivery.
  The producing and consuming services own the subject and payload contract,
  while this repository provides the shared subscriber configuration.
- **HTTP REST contract** — use a service-owned, documented HTTP API for
  synchronous request/response integrations. The owning service defines its
  authentication, request schema, and compatibility policy.

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
