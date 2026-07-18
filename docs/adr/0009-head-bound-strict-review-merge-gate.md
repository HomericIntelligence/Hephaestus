# ADR-0009: Head-bound strict review controls queue-owned merge eligibility

- Status: Superseded by ADR-0012
- Date: 2026-07-16
- Tracks: #2055

## Context

> Historical record only. ADR-0012 retired the CI/CD artifact and proof
> protocol described below. The automation loop now invokes `$athena:pr-review`
> itself and uses its loop-owned `state:implementation-go` label as the sole
> merge authorization.

An implementation-review label alone cannot prove that the exact commit about
to merge received an independent review. The temporary #2054 policy therefore
disabled every automatic armer, but it also prevented the queue from completing
otherwise eligible work.

PR heads can change while review, CI, or auto-merge operations are in flight.
Any merge authority must consequently be bound to an authenticated artifact for
the current head, and must fail closed if it cannot revoke stale eligibility.

## Decision

1. Add `strict_review` between `pr_review` and `ci`. Its agent job is
   read-only, independent, and uses a fresh per-head/per-attempt session.
2. A strict GO publishes and reads back a byte-bounded, digest-verified,
   automation-authored artifact for the exact PR head before applying
   `state:implementation-go`. A NOGO routes to a real implementation pass.
3. CI and every merge-wait transition revalidate the current-head strict GO.
   Head drift revokes the label and auto-merge before returning to strict
   review.
4. `MergeWaitStage` is the sole automatic armer. It uses prepare → arm →
   confirm and contains any failed confirmation, head race, or arming-record
   failure by disabling auto-merge and verifying that containment.

## Alternatives considered

- **Trust `state:implementation-go` alone.** Rejected: labels are not
  commit-bound and can survive a head change.
- **Keep the permanent #2054 manual bootstrap.** Rejected: it is safe but
  cannot complete eligible queue work.
- **Let each stage arm after its own review.** Rejected: duplicated arming
  authority creates races and makes containment impossible to audit.

## Consequences

- The queue has nine stages and strict review is required before CI/merge
  eligibility.
- Automatic arming remains subject to branch protection and can never use an
  administrator bypass.
- Artifact, head-race, identity, byte-limit, and read-only-worker behavior are
  regression-tested as security boundaries.
