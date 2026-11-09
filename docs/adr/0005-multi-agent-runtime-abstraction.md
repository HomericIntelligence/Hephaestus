# ADR-0005: Multi-agent (Claude/Codex/Pi) runtime abstraction

- Status: Accepted
- Date: 2026-06-30
- Tracks: #1452
- Amended: 2026-11-09 for #3525

## Context

The automation pipeline drives more than one agent runtime. Contrary to the
"dual-agent" framing in the originating audit, the tracked runtime supports
**five** providers: `AgentName =
Literal["claude", "codex", "pi", "opencode", "opencode2"]`
(`hephaestus/agents/runtime.py:94`), with `AGENT_CHOICES` enumerating them
(`hephaestus/agents/runtime.py:99`).

Without a shared abstraction, every automation module that shells out to an
agent would branch on the agent type (`if is_codex(...): ...`) and duplicate
provider-specific model selection, timeout resolution, and 429/5xx retry logic.
That is a DRY/SOLID violation that grows with each pipeline stage.

## Decision

Centralize agent selection behind a runtime abstraction rather than branching
per module:

1. The provider set is a single `Literal` type, `AgentName`
   (`hephaestus/agents/runtime.py:94`), with predicate helpers
   `is_codex` (`hephaestus/agents/runtime.py:1040`), `is_pi`
   (`hephaestus/agents/runtime.py:1054`), `is_opencode`
   (`hephaestus/agents/runtime.py:1059`), and `is_opencode2`
   (`hephaestus/agents/runtime.py:1064`) so callers test capability through
   one named function rather than open-coded string comparisons.
2. Automation modules select an agent and resolve its model/timeout/retry
   behavior through this shared layer instead of importing
   `claude_invoke`/`claude_models`/`claude_timeouts` directly.
3. OpenCode v1 and OpenCode 2 select models and effort variants through the
   same runtime boundary.

The tracked, load-bearing artifacts for this decision are `AgentName`,
`AGENT_CHOICES`, and the `is_codex`/`is_pi` predicates in
`hephaestus/agents/runtime.py`. A higher-level `AgentInvoker` facade
(`hephaestus/agents/invoker.py`) is the intended unified entry point that
composes these primitives; it is illustrative of the direction rather than the
canonical anchor for this ADR.

## Alternatives considered

- **Per-module `if is_codex(...)` branching.** Rejected on DRY/SOLID grounds:
  retry, model-resolution, and timeout logic would be duplicated across every
  pipeline stage.
- **A separate concrete class per agent.** Rejected on YAGNI grounds:
  phase-based configuration plus the `AgentName` literal already covers the
  variation; a class hierarchy adds structure with no current payoff.

## Consequences

- Automation modules never import `claude_invoke`/`claude_models`/
  `claude_timeouts` directly; they go through the runtime abstraction.
- Adding a fourth provider means extending `AgentName`/`AGENT_CHOICES` and the
  predicate helpers in one place, not editing every pipeline stage.
- `is_codex`/`is_pi` provide a single, testable seam for provider-specific
  behavior.
