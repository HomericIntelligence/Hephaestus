# ADR-0012: Loop-owned Athena PR-review approval

- Status: Accepted
- Date: 2026-07-17
- Tracks: #2053, #2269
- Retires: #2268, #2278 (CI/lease-dependent approval policies)
- Supersedes: ADR-0009 and ADR-0010

## Context

ADR-0009 and ADR-0010 made a separate GitHub Actions workflow, status context,
and authenticated review artifact part of the automation loop's approval
protocol. That protocol crosses an ownership boundary: the loop cannot control
or depend on CI/CD scheduling, configuration, or status publication. This ADR
records the current topology alongside the immutable pipeline history in
ADR-0006; it does not amend that record.

The loop already has an independent read-only review stage and a durable,
loop-owned `state:implementation-go` label. The required review is
`$athena:pr-review`; it belongs in that stage rather than in GitHub Actions.

## Decision

1. Remove the strict-review-proof workflow, status context, artifact, lease,
   and all label/review-triggered workflow automation.
2. Keep `strict_review` as the internal stage name, but make it invoke
   `$athena:pr-review` in a clean, read-only worktree for the current PR head.
   After its current-head GO read-back, the stage applies the loop-owned
   `state:implementation-go` label itself. It neither publishes a GitHub
   artifact nor reads CI/CD state.
3. `merge_wait` is the sole automatic armer and consumes the loop-owned label.
   A restart re-reads that label and the live PR head; the head is operational
   arm/recovery metadata only, never a post-label invalidation or additional
   authorization requirement. Normal GitHub branch protection and explicit
   operator authority remain independent of this loop decision.

## Alternatives considered

- **Keep a CI/CD proof workflow.** Rejected: it creates an external scheduling
  and authority dependency the loop does not own.
- **Publish a GitHub artifact from the loop.** Rejected: it duplicates loop
  state and turns a review handoff into a cross-system authorization protocol.
- **Remove the independent PR review.** Rejected: `$athena:pr-review` remains
  the required quality review before the loop can apply its label.

## Consequences

- A restart before the label is applied reruns review; a restart after the
  label is applied resumes through merge-wait without an external proof.
- Exactly one active loop leader owns strict review for a repository globally.
  Cooperating processes on that leader host use a local PR ownership lock;
  multi-host concurrent leaders are unsupported without a separately
  authorized loop-owned coordinator.
- CI/CD continues to validate code and protect branches, but the automation
  loop never reads, changes, or relies on it.
- The retired policy no longer appears as an active contract.
