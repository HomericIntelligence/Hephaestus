# Model Context Protocol (MCP) Configuration

Hephaestus keeps a project-scoped `.mcp.json` with an empty `mcpServers`
object. The empty object is an intentional posture, not incomplete setup.
Hephaestus does not currently ship or require an MCP server.

See [ADR-0011](adr/0011-mcp-integration-posture.md) for the architectural
decision.

## Capability boundary

MCP is limited to optional project-scoped agent tooling: a supported agent
client may use `.mcp.json` to discover explicitly approved tools. MCP is not a
Hephaestus Python API, runtime service surface, plugin distribution mechanism,
or ecosystem message transport.

Hephaestus package imports, tests, CLIs, automation, and production workflows
must not depend on an MCP server being configured or reachable. Authentication
material must be supplied at runtime and must not be committed to `.mcp.json`.

## Alternative integration contracts

| Integration need | Maintained contract |
| --- | --- |
| Agent skills, reusable workflows, and knowledge | Plugin marketplaces and skills, including Mnemosyne |
| Asynchronous events and workflow messages | `hephaestus.nats` and NATS JetStream |
| Service APIs and request/response operations | HTTP REST, including Agamemnon and Hermes |

These contracts remain independent of MCP. An agent invoking one of them does
not make that integration an MCP server.

## Runtime and failure boundary

An optional MCP server may fail without changing Hephaestus runtime behaviour.
Making MCP mandatory for package operation, automation startup, or a production
workflow requires a superseding ADR.

## Adding a server

A pull request that adds an entry under `mcpServers` must document:

1. The concrete use case and owning component.
2. The exposed tools, data boundary, and least-privilege controls.
3. Runtime authentication without committed secrets.
4. Startup, timeout, and unavailable-server behaviour.

The pull request must also update this document and the posture assertions in
`tests/unit/docs/test_mcp_config.py`.

A stdio server uses `command` plus `args`; an HTTP server uses `type: "http"`
and `url`. Example entry:

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
