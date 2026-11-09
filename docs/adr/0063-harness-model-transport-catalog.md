# ADR-0063: Harness and model-transport catalog

- Status: Proposed
- Date: 2026-11-09
- Tracks: #3504

## Context

The runtime layer executes five agent harnesses: Claude, Codex, Pi, OpenCode
v1, and OpenCode 2. Each harness sends prompts to a model transport, and each
transport defines its own effort vocabulary (for example the `MODEL[:EFFORT]`
selector forms). Today each harness call site owns its own model-string
rules, so a new harness or transport repeats that selection logic. ADR-0005
records the extension direction (extend `AgentName`/`AGENT_CHOICES` and the
predicate helpers in one place); it does not record how harnesses and model
transports relate. This ADR records that relation. It does not supersede
ADR-0044.

## Decision

1. The harness and the model transport are separate concepts. A harness is
   the agent runtime that the pipeline starts. A model transport is the model
   surface that a harness sends prompts to.
2. A catalog maps each harness to the transports it accepts, and to the
   effort vocabulary that each transport defines.
3. The catalog is data, not policy. ADR-0044 per-role tool and model
   resolution stays authoritative.
4. An unknown transport or variant produces a warning and a fallback to the
   harness default.

## Alternatives considered

- One flat provider list. Rejected: it cannot show which transports a
  harness accepts, and it mixes two concepts in one name.
- A per-call-site model table. Rejected: it duplicates the catalog at every
  call site and drifts from it.

## Consequences

- ADR-0044 stays in force. No resolution rule moves.
- The catalog lands with #3510.
- A new harness or transport extends the catalog in one place.
