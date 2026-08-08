# ADR-0016: Bounded operational merge-readiness wait

- Status: Accepted
- Date: 2026-07-25
- Tracks: #2445
- Supersedes: ADR-0015

## Context

ADR-0015 bounded every merge-wait retry with the conditional-merge request
budget. Normal required checks commonly take minutes, so five attempts with a
short backoff exhausts before GitHub can accept the first safe conditional
request. Increasing that request budget would create unnecessary merge
mutations and would not make CI status an authorization fact.

The queue already has a timer heap and a read-only operational readiness
lookup. It needs a bounded wait that preserves the reviewed-head admission
boundary and does not inherit an old head's deadline after fresh review.

## Decision

1. Before a conditional merge request, `merge_wait` observes GitHub's
   operational readiness. `CLEAN`, `HAS_HOOKS`, and `UNSTABLE` each permit
   consideration of a request only when GitHub also reports `MERGEABLE`.
   `CLEAN` is the ordinary ready state. `HAS_HOOKS` and `UNSTABLE` can reflect
   optional or externally supplied checks that do not independently revoke the
   reviewed-head admission proof, so the server-protected conditional request
   remains the authoritative classification. `BEHIND`, `BLOCKED`, and
   `UNKNOWN` park on the timer heap; `CONFLICTING` or `DIRTY` (and a
   conflicting mergeability result) fail closed, as do unsupported readiness
   combinations.
2. Readiness is never merge authority. Immediately before every conditional
   request, the queue again verifies the open `main` PR, active reviewed head,
   exclusive implementation-state label, unresolved-thread state, and server
   conversation-resolution protection.
3. A readiness wait has a 15-minute monotonic deadline and 5-second
   exponential cadence capped at 60 seconds. The deadline is keyed to the
   current reviewed-head proof and resets only when fresh review establishes a
   new proof, including for an unchanged head.
4. Readiness polling makes no conditional merge request and does not consume
   the merge-attempt budget. That budget still bounds actual conditional
   requests and transport-ambiguity retries. If it is already exhausted, the
   stage fails closed rather than waiting.
5. A declined conditional request re-enters the readiness wait before any
   later request. The stage records a fingerprint of the reviewed head, proof
   generation, mergeability, and merge-state status. While that fingerprint is
   unchanged after a `405`, each timer wake parks without another conditional
   request; a changed fingerprint, or fresh review that creates a new proof
   generation, may permit a later request within its bounded wait window.
   Conflicting, incomplete, externally armed, closed, or deadline-expired
   state remains fail-closed.

## Alternatives considered

- **Raise the merge-attempt budget.** Rejected: it creates more mutation
  opportunities without representing normal CI duration.
- **Authorize from check status.** Rejected: readiness remains operational
  observation; label, exact-head, thread, and protection admission remain the
  authorization boundary.
- **Use GitHub auto-merge or a merge queue.** Rejected: they delegate merge
  ownership beyond the current reviewed-head proof.
- **Carry wait state across head changes.** Rejected: a fresh review proof
  deserves a fresh bounded wait and must not inherit a stale deadline.

## Consequences

- A single queue run can wait through ordinary minute-scale CI without
  repeatedly issuing conditional merge requests.
- Normal merging remains a direct SHA-conditional REST operation; no queue
  stage enables auto-merge, joins a merge queue, or bypasses protection.
- Operators get a finite timeout for truly stalled readiness while a new
  reviewed head receives an independent operational-wait window.
