# ADR-0011: MCP is optional project-scoped agent tooling

- Status: Accepted
- Date: 2026-07-16
- Tracks: #2163

## Context

Hephaestus ships a project-scoped `.mcp.json` at the repository root whose
`mcpServers` map is empty. An empty configuration file carries no inherent
meaning: a reader cannot tell whether MCP support is incomplete, deprecated,
forbidden, or simply unused. Without a recorded capability boundary, a future
contributor could reasonably add an MCP server that becomes a load-bearing
runtime dependency, or delete `.mcp.json` on the assumption that an empty file
is dead configuration.

The HomericIntelligence ecosystem already maintains purpose-specific
integration contracts — Claude Code plugin marketplaces and skills for agent
capabilities, NATS JetStream for asynchronous events, and HTTP REST for service
APIs. None of these is wired through MCP, so the empty `mcpServers` map is a
deliberate posture rather than an unfinished setup. That posture needs an
explicit architectural record so the empty configuration and its alternatives
are maintained together.

## Decision

1. MCP is optional project-scoped agent tooling. A supported agent client may
   read `.mcp.json` to discover explicitly approved tools; it is not a
   Hephaestus Python API, runtime service surface, plugin distribution
   mechanism, or ecosystem message transport.
2. `.mcp.json` remains version-controlled with an empty `mcpServers` object.
   The empty object is the accepted default, not incomplete configuration, and
   `.mcp.json` is not deleted.
3. Hephaestus package imports, tests, CLIs, automation, and production
   workflows must not depend on an MCP server being configured or reachable.
   Authentication material is supplied at runtime and must never be committed
   to `.mcp.json`.
4. The maintained integration contracts remain independent of MCP: plugin
   marketplaces and skills (including Mnemosyne) for agent capabilities, NATS
   JetStream (`hephaestus.nats`) for asynchronous events, and HTTP REST
   (including Agamemnon and Hermes) for service APIs.
5. A pull request that adds an entry under `mcpServers` must name the owning
   component and use case, declare least-privilege capabilities and the data
   boundary, supply authentication at runtime without committed secrets, and
   document startup, timeout, and unavailable-server behaviour. It must also
   update `docs/mcp.md` and the posture assertions in
   `tests/unit/docs/test_mcp_config.py`.

## Alternatives considered

- **Delete `.mcp.json`.** Rejected: the empty file makes the configuration
  surface explicit and version-controlled for the whole team, and its absence
  would be indistinguishable from an oversight.
- **Add a speculative MCP server now.** Rejected: no current use case requires
  one, and a server added without an owner or contract risks becoming an
  undocumented runtime dependency.
- **Replace the ecosystem transports with MCP.** Rejected: plugin
  marketplaces, NATS JetStream, and HTTP REST are established, purpose-specific
  contracts; folding them into MCP would add a transport dependency without
  removing the existing ones.

## Consequences

- The empty `.mcp.json` has a documented meaning: optional, project-scoped, and
  non-load-bearing. Contributors can neither silently delete it nor let an MCP
  server become mandatory.
- `docs/mcp.md`, `AGENTS.md`, and `tests/unit/docs/test_mcp_config.py` state and
  enforce the same boundary; drift fails the test suite.
- Making MCP required for package operation, automation startup, or a
  production workflow requires a superseding ADR.
