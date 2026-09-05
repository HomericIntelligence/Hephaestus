# ADR-0037: Reviewed-head CI gate authorizes queue merge

- Status: Accepted
- Date: 2026-09-05
- Tracks: #2965
- Supersedes: ADR-0024

## Context

The queue owns a SHA-conditional normal merge. The `state:implementation-go`
label records the result of the automated structural review. The active run
also keeps a process-local proof for the exact reviewed head.

ADR-0024 added a second GitHub user and a marked native `APPROVED` review as a
separate merge authorization. That rule blocks the single-maintainer workflow
even when the structural review is valid and the required CI checks are green.
The second review is a queue rule. It is not a branch-protection requirement.

## Decision

Before each conditional merge request, `merge_wait` requires all of these
facts:

```text
state:implementation-go ────────► structural-review eligibility
current-process head proof ─────► reviewed checkout identity
exact-head status evidence ─────► current CI merge gate
                                 │
                                 ▼
                      SHA-conditional squash merge
```

1. The PR is open, targets `main`, has an explicitly absent
   `autoMergeRequest`, and has an exclusive `state:implementation-go` label.
2. The current PR head equals the process-local reviewed head.
3. No unresolved review thread exists. A stable effective-policy read combines
   classic branch protection with each applicable active ruleset. At least one
   enforcement source must require review-thread resolution without a bypass
   for the current repository actor. Classic administrator enforcement can
   supply this protection when an applicable ruleset has a repository-role
   bypass.
4. The effective required-check inventory is the union of the classic and
   ruleset inventories. Exact-head evidence includes Check Runs and commit
   statuses. A required Check Run must have `status=completed` and a conclusion
   of `success`, `neutral`, or `skipped`. A required commit status must have
   `state=success`. A commit status can satisfy only an unbound required context.
   A positive application binding requires a Check Run from that application.
   When both sources have the same required context, both sources must pass.
   Optional evidence does not grant or revoke merge authority. Empty, malformed,
   pending, failed, stale, or changing required evidence fails closed.
5. Each source uses complete, bounded, exact-head, stable double reads. Policy,
   Check Run, and commit-status pagination share one aggregate deadline and
   cancellation signal. The queue completes all mutable traversals, reads
   unresolved threads, and then repeats the open-head-label admission. It sends
   the conditional request immediately after that admission, with no intervening
   mutable read.
6. The queue sends one normal REST merge request with `sha=<reviewed head>` and
   `merge_method=squash`. The request is the only merge-state mutation owned by
   the queue. The queue does not enable or manage native auto-merge.

A second GitHub user and a marked native `APPROVED` review are not required for
queue merge. CI does not authorize `state:implementation-go`; it is a separate
gate after the structural review. Native review data can still support review
thread and scope processing.

## Alternatives considered

- **Keep the second-user marked review.** Rejected: it adds a queue-only human
  gate to a single-maintainer workflow and does not improve the exact-head
  SHA condition.
- **Use the GO label without required status evidence.** Rejected: a reviewed
  head can have missing, pending, or failed required validation.
- **Use PR check output without a commit binding.** Rejected: a mutable PR ref
  can move after the review. Required status evidence must identify the reviewed
  commit.
- **Enable native auto-merge after CI succeeds.** Rejected: native auto-merge
  has external ownership and is not conditional on the active process proof.

## Consequences

The single-maintainer workflow can merge a reviewed head with passing required
status evidence without a second GitHub user. Head drift, missing or failed
required status evidence, unresolved threads, untrusted PR state, and
incomplete reads still stop the merge path. A non-required failed Check Run can
produce an `UNSTABLE` merge state, but does not stop this gate. A process restart
still loses the process-local proof and sends the PR through fresh review. The existing
explicit operator-authorization implementation remains available only to
compatibility and review-data callers; it is not part of the queue merge
decision.
