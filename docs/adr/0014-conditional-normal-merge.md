# ADR-0014: Conditional normal merge after loop-owned review

- Status: Accepted
- Date: 2026-07-25
- Tracks: #2419
- Supersedes: ADR-0012
- Clarifies: ADR-0009

## Context

ADR-0012 established that `pr_review` is the loop's sole implementation
approval authority and that the current process carries its reviewed-head
proof. Its temporary merge-wait policy deliberately stood by while #2419 was
reviewed. ADR-0009 describes an earlier native auto-merge arming protocol.

Neither a read-then-disable sequence nor a durable local arming record can
prove ownership of GitHub's mutable native auto-merge request. A later actor
can replace that request between reads, so containment could disable another
actor's arm. GitHub does, however, offer a normal merge endpoint conditional
on the PR head SHA. That condition can make the merge mutation itself prove
that the reviewed head did not change before the request was accepted.

## Decision

1. `merge_wait` is the only queue stage permitted to request a merge. It first
   reads final admission facts: the PR is open against `main`, has no native
   auto-merge request, has an exclusive `state:implementation-go` label, and
   has the exact in-process reviewed-head proof for its current head.
2. After final admission it makes at most one repository-scoped REST request:
   `PUT /repos/{owner}/{repo}/pulls/{number}/merge`, with only the reviewed
   `sha` and `merge_method=squash`. It never invokes `gh pr merge`, native
   auto-merge, a merge queue, an administrator bypass, or a retrying mutation
   helper.
3. A `200` response succeeds only after a fresh lifecycle read reports
   `MERGED`. A `409` is reconciled with a fresh state read; head drift returns
   to review without label mutation, while an externally armed request blocks
   without mutation. A `405` performs a fresh readiness read: `CONFLICTING`
   and `DIRTY` are terminal, while explicitly retryable readiness states may
   use a bounded timer retry.
4. A transport ambiguity is never assumed to have failed. `merge_wait` first
   re-reads lifecycle state. It succeeds if the PR is merged, and retries only
   when the PR remains open on the same reviewed head, is unarmed, retains the
   exclusive GO label, and has budget remaining. The retry is timer-delayed;
   unreadable or changed state is terminal or returns to review as appropriate.
5. The reviewed-head proof is process-local. A restart cannot reconstruct it
   from labels or a work item, so merge-wait returns the item to review with
   zero label writes. No queue stage creates, disables, adopts, or polls
   native auto-merge.

## Alternatives considered

- **Continue native auto-merge arming and containment.** Rejected: GitHub
  exposes no conditional ownership token for a disable, so an external arm can
  be modified accidentally.
- **Use `gh pr merge --auto` or a merge queue.** Rejected: both delegate the
  final merge to a mutable asynchronous mechanism rather than binding it to
  this process's reviewed SHA.
- **Retry uncertain merge mutations immediately.** Rejected: a transport
  failure can conceal a successful merge; immediate retry risks duplicate or
  unauthorized mutation.
- **Treat a durable GO label as restart-safe proof.** Rejected: labels are not
  bound to a session or exact review snapshot.

## Consequences

- Merge authority remains local to one reviewed process and one exact SHA.
- The only queue merge write is auditable as a single conditional REST PUT
  with a fixed squash method and no privilege escalation.
- Ambiguous network outcomes are slower when the PR is still eligible, but
  bounded delayed reconciliation prevents duplicate in-step requests.
- Native auto-merge remains an external-ownership boundary. Operators retain
  normal GitHub branch protection and manual merge authority independent of
  the loop.
- Regression tests cover 200 confirmation, transport ambiguity, unreadable
  state, restart, exhaustion, 409 external ownership, conflicting readiness,
  and timer re-entry/attempt counting.
