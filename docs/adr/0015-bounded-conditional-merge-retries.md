# ADR-0015: Bounded conditional merge retries

- Status: Accepted
- Date: 2026-07-25
- Tracks: #2419
- Supersedes: ADR-0014

## Context

ADR-0014 replaced native auto-merge arming with a SHA-conditional ordinary
REST squash merge. It correctly made each individual merge mutation
conditional, but its prose described the whole `merge_wait` stage as a
one-request operation while the stage has a bounded retry budget. That leaves
the active contract ambiguous: retryable readiness and unresolved transport
outcomes must be reconciled without turning a single adapter invocation into a
retrying mutation helper.

The stage already carries a current-process reviewed-head proof and a per-item
merge budget whose default is five. A later request remains safe only when it
is independently admitted from fresh live GitHub facts; a durable label cannot
reconstruct this process-local proof after restart.

## Decision

1. `merge_wait` may make a bounded sequence of individual SHA-conditional
   ordinary REST squash-merge requests. The default per-item budget is five.
2. Every individual request has fresh admission of the same active-run
   reviewed-head proof, an open PR against `main`, no native auto-merge
   request, and the exclusive loop-owned `state:implementation-go` label.
3. The direct GitHub adapter makes one request per call and never retries.
   Only classified retryable HTTP 405 readiness and unresolved transport
   ambiguity may timer-park a later stage attempt, after fresh reconciliation
   and only while budget remains.
4. The stage never invokes `gh pr merge`, creates or manages native auto-merge,
   manages a merge queue, or uses an administrator or queue bypass. Existing
   native auto-merge is external ownership and stops loop merge mutation.
5. A restart has no durable reviewed-head proof and therefore returns the item
   to review without trying to reconstruct authority from labels or prior
   attempts.

## Alternatives considered

- **Describe the entire stage as one request.** Rejected: the bounded retry
  lifecycle makes that claim false and obscures the fresh-admission boundary.
- **Retry inside the direct adapter.** Rejected: it would hide multiple
  mutations in one call and could issue a later request without a fresh stage
  admission.
- **Use native auto-merge, `gh pr merge`, a merge queue, or a bypass.**
  Rejected: each delegates or elevates merge authority beyond the active
  reviewed-head proof.
- **Persist the reviewed-head proof across restarts.** Rejected: local or label
  state cannot prove ownership of a later mutable GitHub condition.

## Consequences

- Operators can distinguish the stage-level bounded sequence from the direct
  adapter's single conditional request.
- Every later request is auditable with its own current proof and live
  admission facts.
- Retryable readiness and transport ambiguity may delay completion, but never
  authorize a hidden retry, auto-merge mutation, merge-queue action, or bypass.
- ADR-0014 remains immutable historical context; this ADR is the active
  contract for bounded conditional merge retries.
