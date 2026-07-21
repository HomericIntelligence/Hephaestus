# ADR-0014: AGENTS.md is the canonical agent contract

- Status: Accepted
- Date: 2026-07-21
- Tracks: #2338
- Supersedes: the active agent-contract location assumptions in ADR-0001 and ADR-0013

## Context

ADR-0001 and ADR-0013 are Accepted historical records. They refer to
`CLAUDE.md`, which was the agent-contract location when those decisions were
made. The repository now maintains its authoritative agent instructions in
`AGENTS.md`; `CLAUDE.md` is only a compatibility pointer.

Changing either accepted record would obscure its decision-time context and
violate the append-only ADR policy. The current location must therefore be
recorded separately and linked to the historical decisions it updates.

## Decision

1. `AGENTS.md` is the sole authoritative agent contract for the repository.
2. `CLAUDE.md` remains an exact compatibility pointer to `AGENTS.md` and does
   not define independent policy.
3. Active documentation, automation, and validation references use `AGENTS.md`.
   ADR-0001 and ADR-0013 retain their original `CLAUDE.md` wording as immutable
   historical context; this ADR supersedes only their agent-contract location
   assumptions.

## Alternatives considered

- **Rewrite ADR-0001 and ADR-0013.** Rejected: Accepted ADRs are immutable,
  and edits would erase the decision-time record.
- **Keep two authoritative contracts.** Rejected: duplicated policy would
  drift and make the authoritative source ambiguous.
- **Remove `CLAUDE.md` outright.** Rejected: existing integrations may still
  look for it; a minimal pointer preserves compatibility without duplicate
  policy.

## Consequences

- Editorial changes to `AGENTS.md` remain unconstrained except for the
  compatibility-pointer behavior.
- New active references to the agent contract name `AGENTS.md`.
- The historical mentions in ADR-0001 and ADR-0013 remain valid and auditable;
  this ADR supplies their current-location successor.
