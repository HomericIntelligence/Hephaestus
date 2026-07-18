# ADR-0009: Retired external approval policy

- Status: Retired
- Date: 2026-07-16

## Context

The project previously documented an approval design outside the automation
loop's ownership boundary. That design was retired because the loop cannot
control external scheduling or state publication.

## Decision

The active approval policy is ADR-0011: `$athena:pr-review` runs inside the
loop and `strict_review` applies the loop-owned approval label after a
current-head GO.

## Alternatives considered

- **Keep external approval authority.** Rejected: it introduces a dependency
  the loop does not own.

## Consequences

- This retained ADR number exists only to preserve the historical ADR sequence.
- No active implementation may use this retired policy.
