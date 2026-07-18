# ADR-0010: Retired external proof policy

- Status: Retired
- Date: 2026-07-16

## Context

The project previously documented an external proof mechanism for automation
approval. It was retired because it crossed the automation loop's ownership
boundary.

## Decision

The active approval policy is ADR-0011. The loop itself runs the required
review and applies its loop-owned approval label without external proof state.

## Alternatives considered

- **Keep external proof state.** Rejected: it adds an authority dependency the
  loop cannot operate or validate.

## Consequences

- This retained ADR number exists only to preserve the historical ADR sequence.
- No active implementation may use this retired policy.
