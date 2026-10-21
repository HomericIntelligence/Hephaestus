# ADR-0057: Protected rebase before conditional merge

- Status: Accepted
- Date: 2026-10-17
- Tracks: #2455

## Context

Parallel implementation pull requests (PRs) share one protected base branch.
The first protected merge moves the base forward. The reviewed head of every
other ready PR is then stale, and GitHub reports its merge state as `BEHIND`.
ADR-0052 merge admission requires the exact reviewed head and completed
required checks. A request issued against a stale head races the server-side
strict-update protection: GitHub rejects the request, or worse, the response
is lost after a successful merge.

Before this decision, the loop classified `BEHIND` as a retryable readiness
wait. The PR waited for a state that could not return on its own, and a
repeated merge request after a lost response had no durable record of the
first attempt. A retried request could then merge a head the loop no longer
owns, or reject a recovery that was still safe.

## Decision

The loop repairs a stale reviewed head with a durable, ordinary-Git rebase in
the PR writer workspace. It never force-pushes, never bypasses branch
protection, and never arms auto-merge. One fresh review of the rebased head
is the only path to new merge authority.

### Merge barriers

The merge-wait cycle classifies readiness twice: once after the effective
policy is proven (barrier 1) and once after the final admission re-read
(barrier 2). A `BEHIND` classification at either barrier routes the item to a
protected rebase, and no merge request is issued. The classification needs a
proven rebase target built from the extended readiness fields `baseRefOid`,
`headRefName`, `headRepository`, and `headRepositoryOwner`. A missing or
malformed field is terminal (`protected_rebase_target_unverified`), and a
head repository outside the pull-request repository is terminal
(`protected_rebase_fork_head`).

### Durable protected-merge store

One store per repository under the repository build directory holds two
families of records:

- **Merge-attempt claims.** The claim key binds the repository, PR number,
  reviewed head, base, proof generation, mergeable state, and readiness
  status. A granted claim permits exactly one conditional merge request for
  that fingerprint. Conclusive responses (200, 409, 405) are recorded, and an
  identical re-claim is reconcile-only; a non-ambiguous record yields
  `merge_attempt_conflict`. A lost, malformed, or unclassified response marks
  the claim permanently ambiguous, and an identical re-claim yields
  `merge_attempt_ambiguous`. Both outcomes fail back to a fresh review.
- **Rebase cycles.** One PR may claim at most eight durable cycles. A
  repeated observation of the same head and base replays the existing record
  without consuming a cycle.

The claim guards only the direct conditional merge. A required merge queue
serializes admission requests server-side, and its removal reconciliation
stays free to re-admit under an unchanged fingerprint.

### Writer capability provenance

The coordinator registers one opaque, process-local capability for each
coordinator-proven implementation writer worktree. Only a newly created or a
direct absent-only-reserved allocation receives one; adopted and reused
allocations do not. The record binds the exact live work item, repository,
issue, branch, and resolved writer path, and the terminal sink removes it.
The implementation stage obtains the capability by an exact-identity lookup,
and the queue worker revalidates every bound field before any mutation. A
capability is never minted from job-supplied identity data.

### Protected rebase cycle

The `protected_rebase` Git operation claims one durable cycle, fetches the
protected base and branch, and proves both remote tips still equal the
claimed head and base. Drift fails the cycle durably. The worker replays the
reviewed commits onto the fetched base with signed commits and a format-1
bridge metadata record, proves head and base ancestry locally, and records
the cycle as prepared before any push. Publication is one ordinary
`git push` of the built bridge. A rejected push leaves a durable failed
cycle. A published cycle replays as an idempotent success. A success clears
every merge-wait proof payload the stale base had produced.

## Alternatives considered

- **Keep retrying the conditional merge on `BEHIND`.** Rejected: the request
  races strict-update protection, and a lost response leaves an unowned
  server-side effect.
- **Force-push the rebased branch.** Rejected: repository policy forbids
  force pushes, and the writer workspace must behave as an ordinary author.
- **Arm auto-merge on a stale head.** Rejected: the loop never arms
  auto-merge; the server-side decision would not use the reviewed-head proof.
- **Hold the deduplication record in memory.** Rejected: a coordinator
  restart would lose the claim and could issue a second request after a lost
  response.
- **Mint the writer capability at the stage or worker.** Rejected: identity
  there comes from job fields, not from a coordinator-proven allocation
  receipt.

## Consequences

A stale ready PR costs at most eight bounded cycles and one fresh review. It
never consumes a merge request against a stale base. The durable store holds
the claim and cycle bounds across coordinator restarts. On each store access
by the merge-wait cycle (pipeline_github_jobs.py) and the protected-rebase
worker (worker_pool.py), `reconcile_on_open` is called explicitly to verify
non-terminal cycles against the remote head and validate bridge metadata
before upgrading a prepared cycle to published. The merge-queue path
is unchanged: it uses no durable claim, and a re-admission after a proven
queue removal costs no cycle. If the store cannot open, the merge cycle stops
with `protected_merge_state_unavailable` and the item takes the ordinary
non-GO review path.
