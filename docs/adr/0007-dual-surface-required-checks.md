# ADR-0007: Dual-surface required checks

- Status: Accepted
- Date: 2026-07-10
- Tracks: #2054
- Supersedes: ADR-0004

## Context

ADR-0004 established `required-checks-gate` as an aggregate fan-in job so
workflow membership cannot silently drift from the merge gate. The live
repository configuration now applies two required-status-check surfaces to
`main`: classic branch protection requires the aggregate gate and two Python
3.12 test contexts, while the active repository ruleset requires nine direct
workflow contexts. Treating the ruleset as the only enforcement surface would
misdescribe the actual merge contract.

## Decision

Preserve the aggregate gate and document the dual required-check contract:

1. `required-checks-gate` remains a classic branch-protection required context
   and fans in every non-advisory gating job.
2. The two Python 3.12 matrix contexts remain classic branch-protection
   required contexts.
3. The active ruleset's nine direct contexts remain independently required.
4. The policy runbook validates the exact classic and direct ruleset inventories
   before changing GitHub configuration.

This ADR supersedes ADR-0004's claim that adding a gating job never needs a
GitHub-side policy decision. Adding it to the aggregate gate makes it block via
classic branch protection; adding a separate direct ruleset context remains an
explicit GitHub configuration decision.

## Alternatives considered

- **Rewrite ADR-0004.** Rejected: accepted ADRs are immutable historical
  decisions. A successor preserves the record of the original aggregate-gate
  rationale while documenting the current contract.
- **Treat the ruleset as the only enforcement surface.** Rejected: classic
  branch protection currently requires `required-checks-gate` and two matrix
  contexts, so this would be operationally false.
- **Remove the aggregate gate.** Rejected: it prevents workflow membership from
  drifting out of classic branch-protection enforcement.

## Consequences

- Merge documentation and automation must account for both classic branch
  protection and direct ruleset contexts.
- Adding a gating job requires adding it to the aggregate gate; a direct
  ruleset change is required only when the job needs an independently visible
  direct ruleset context.
- The runbook aborts if either live context inventory differs from this
  decision, forcing a deliberate policy review rather than an unsafe rewrite.
