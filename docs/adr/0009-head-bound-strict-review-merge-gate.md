# ADR-0009: Head-bound strict review controls queue-owned merge eligibility

- Status: Accepted
- Date: 2026-07-16
- Tracks: #2055

## Context

An implementation-review label alone cannot prove that the exact commit about
to merge received an independent review. The temporary #2054 policy therefore
disabled every automatic armer, but it also prevented the queue from completing
otherwise eligible work.

PR heads can change while review, CI, or merge operations are in flight.
Any merge authority must consequently be bound to an authenticated artifact for
the current head, and must fail closed if it cannot revoke stale eligibility.

## Decision

1. Add `strict_review` between `pr_review` and `ci`. Its agent job is
   read-only, independent, and uses a fresh per-head/per-attempt session.
2. A strict GO publishes and reads back a byte-bounded, digest-verified,
   automation-authored artifact for the exact PR head before applying
   `state:implementation-go`. A NOGO routes to a real implementation pass.
3. CI and every merge-wait transition revalidate the current-head strict GO.
   Head drift revokes the label only after a confirmed-unarmed read before
   returning to strict review.
4. **Historical (superseded below):** `MergeWaitStage` was the sole automatic
   armer. Its prepare → arm → confirm protocol is no longer active because
   persistent auto-merge ownership cannot be contained safely.

## Supersession note (2026-07-25)

The historical persistent-auto-merge containment described by this ADR is
superseded for the queue pipeline by issue #2419. `merge_wait` now makes one
ordinary GitHub REST squash merge conditional on the process-local reviewed
SHA, after verifying an open `main` PR with an exclusive approval label and no
auto-merge request. The conditional SHA is the linearization point; any
ambiguous outcome is reconciled from lifecycle state before a bounded retry.

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
