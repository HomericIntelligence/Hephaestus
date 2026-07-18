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

The loop already has a PR-review stage and a durable, loop-owned
`state:implementation-go` label. The required review is `$athena:pr-review`;
it belongs in that stage rather than in GitHub Actions.

## Decision

1. Remove the former proof workflow, status context, artifact, lease,
   and all label/review-triggered workflow automation.
2. Remove the separate reviewer stage, its worktree/evidence/guard
   machinery, and its tests. `pr_review` invokes `$athena:pr-review` with its
   normal default behavior when available, otherwise performs the inline
   fallback. It posts inline findings and a final total grade with GO/NOGO;
   a GO applies the loop-owned `state:implementation-go` label. It neither
   publishes a GitHub artifact nor changes CI/CD state; normal review may
   collect CI/CD evidence but does not use it as authorization.
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
- **Remove PR review.** Rejected: `$athena:pr-review` remains the required
  quality review before the loop can apply its label.

## Consequences

- A restart before the label is applied reruns review; a restart after the
  label is applied resumes through merge-wait without an external proof.
- CI/CD continues to validate code and protect branches. Normal review may
  collect its evidence, but the automation loop does not change it or use it
  as authorization.
- The retired policy no longer appears as an active contract.
