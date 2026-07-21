# ADR-0005: Multi-agent (Claude/Codex/Pi) runtime abstraction

- Status: Accepted
- Date: 2026-06-30
- Tracks: #1452

## Context

The automation pipeline drives more than one agent runtime. Contrary to the
"dual-agent" framing in the originating audit, the tracked runtime supports
**three** providers: `AgentName = Literal["claude", "codex", "pi"]`
(`hephaestus/agents/runtime.py:23`), with `AGENT_CHOICES` enumerating them.

Without a shared abstraction, every automation module that shells out to an
agent would branch on the agent type (`if is_codex(...): ...`) and duplicate
provider-specific model selection, timeout resolution, and 429/5xx retry logic.
That is a DRY/SOLID violation that grows with each pipeline stage.

The migration is not yet complete. The provider-neutral direct-runner entry
points in `hephaestus/agents/runtime.py` execute Codex and Pi, while Claude
session invocation, response parsing and validation, and failure diagnostics
remain in `hephaestus.automation.claude_invoke`. `claude_models` and
`claude_timeouts` remain compatibility shims over
`hephaestus.automation.agent_config`.

## Decision

Centralize agent selection behind a runtime abstraction rather than branching
per module:

1. The provider set is a single `Literal` type, `AgentName`
   (`hephaestus/agents/runtime.py:23`), with predicate helpers
   `is_codex` (`hephaestus/agents/runtime.py:205`) and `is_pi` so callers test
   capability through one named function rather than open-coded string
   comparisons.
2. New automation call sites must use `hephaestus.agents.runtime` for provider
   selection and provider-neutral execution. Existing automation compatibility
   seams may retain the frozen direct imports of `claude_invoke` for Claude
   invocation, response parsing or validation, and formatting Claude-specific
   failure diagnostics, and of `claude_models` for its `agent_config`
   compatibility exports. No direct `claude_timeouts` consumer is approved.
   These imports are migration debt: no new consumer/module pair may be added,
   and migrated pairs must be removed from source and the regression baseline
   together.

The tracked, load-bearing artifacts for this decision are `AgentName`,
`AGENT_CHOICES`, the `is_codex`/`is_pi` predicates, and the direct-runner
functions in `hephaestus/agents/runtime.py`, together with provider
configuration in `hephaestus/automation/agent_config.py`. A unified
`AgentInvoker` facade is not currently present; completing that facade and
removing the frozen legacy imports remain future migration work.

## Alternatives considered

- **Per-module `if is_codex(...)` branching.** Rejected on DRY/SOLID grounds:
  retry, model-resolution, and timeout logic would be duplicated across every
  pipeline stage.
- **A separate concrete class per agent.** Rejected on YAGNI grounds:
  phase-based configuration plus the `AgentName` literal already covers the
  variation; a class hierarchy adds structure with no current payoff.

## Consequences

- The approved legacy baseline is 30 exact consumer/module pairs: 16 for
  `claude_invoke`, 14 for `claude_models`, and 0 for `claude_timeouts`.
  `tests/unit/validation/test_adr_0005_direct_import_policy.py` enforces exact
  equality and keeps every exception inside the automation product layer.
- New automation call sites go through the runtime abstraction; direct legacy
  imports cannot expand, and each completed migration removes its source import
  and baseline entry together.
- Adding a fourth provider means extending `AgentName`/`AGENT_CHOICES` and the
  predicate helpers in one place, not editing every pipeline stage.
- `is_codex`/`is_pi` provide a single, testable seam for provider-specific
  behavior.
