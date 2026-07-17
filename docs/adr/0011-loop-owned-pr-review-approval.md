# ADR-0011: Loop-owned Athena PR-review approval

- Status: Accepted
- Date: 2026-07-17
- Tracks: #2053, #2268, #2269, #2278
- Supersedes: ADR-0009, ADR-0010

## Context

ADRs 0009 and 0010 made a separate GitHub Actions workflow, status context,
and authenticated review artifact part of the automation loop's approval
protocol. That protocol crosses an ownership boundary: the loop cannot control
or depend on CI/CD scheduling, configuration, or status publication.

The loop already has an independent read-only review stage and a durable,
loop-owned `state:implementation-go` label. The required review is
`$athena:pr-review`; it belongs in that stage rather than in GitHub Actions.

## Decision

1. Remove the strict-review-proof workflow, status context, artifact, lease,
   and all label/review-triggered workflow automation.
2. Keep `strict_review` as the internal stage name, but make it invoke
   `$athena:pr-review` in a clean, read-only worktree for the current PR head.
   Its GO result is an in-memory handoff only; it neither publishes a GitHub
   artifact nor applies a label.
3. The loop's check-observation stage may observe green checks or `NO_CHECKS`.
   It must work when no CI workflow exists, and no external status can grant
   approval. After a current-head Athena review and that observation, the loop
   applies `state:implementation-go` itself.
4. `merge_wait` is the sole automatic armer and consumes only the loop-owned
   label. Normal GitHub branch protection and explicit operator authority
   remain independent of this loop decision.

## Alternatives considered

- **Keep a CI/CD proof workflow.** Rejected: it creates an external scheduling
  and authority dependency the loop does not own.
- **Publish a GitHub artifact from the loop.** Rejected: it duplicates loop
  state and turns a review handoff into a cross-system authorization protocol.
- **Remove the independent PR review.** Rejected: `$athena:pr-review` remains
  the required quality review before the loop can apply its label.

## Consequences

- A restart before the label is applied reruns review instead of recovering an
  external proof; this is the expected fail-safe behavior.
- CI/CD continues to validate code and protect branches, but does not start,
  approve, or arm the automation loop.
- ADRs 0009 and 0010 remain historical records only and no longer describe the
  active approval protocol.
