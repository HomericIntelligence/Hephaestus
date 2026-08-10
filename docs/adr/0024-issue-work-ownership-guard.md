# ADR-0024: Ref-backed issue work-ownership guard

- Status: Accepted
- Date: 2026-08-06
- Tracks: #2404

## Context

Multiple automation processes can discover the same eligible issue between
fresh label reads. Labels are durable workflow state, but a shared label does
not identify which process won a concurrent claim. Releasing such a label
without an ownership check could clear a live run or alter the independent
`state:plan-blocked` operator latch.

## Decision

Use `state:in-progress` as an orthogonal, visible contention label. It is not
part of the plan-state or implementation-state tuples and never authorizes a
stage transition. The authoritative ownership record is canonical version-one
JSON in the body of a no-tree-change commit on the exact implementation branch
for the issue. The commit uses a stable Conventional Commit subject and machine
DCO trailer so the guard's own audit history satisfies the repository's PR
policy. The commit is created and verified with the operator's configured Git
signing identity, then published through an exact-head `--force-with-lease`
compare-and-swap. GitHub's verification result is read back before the
transition is accepted. Readers also accept the legacy raw-JSON commit form so
existing guards remain recoverable. The branch is carried in the guard
credential, so the guard and production writer share one ref:
`refs/heads/<implementation-branch>`. A branch tip may contain ordinary
implementation commits; readers inspect branch-only history to find the newest
guard record, while every guard child still advances that same branch with a
server-enforced exact-head compare-and-swap update.

Before the first guard exists, the reader bounds discovery to commits unique
to the implementation branch through the GitHub comparison API. It never
walks the repository's shared default-branch history one commit at a time.

An automation run reads labels, refuses a live guard, creates an acquiring
record, installs it with an exact-head leased ref operation, and reads it back
before adding the label. A signing failure, a rejected lease, or an unverified
GitHub signature fails closed. It then records an active child and confirms
both the record identity and label before dispatching work. Every issue
mutation uses the target-bound guarded GitHub proxy, which re-confirms the
claim immediately before the mutation. Claims carry repository, issue, branch,
claim UUID, and run UUID across worker boundaries.

Active claims use a four-hour lease. Renewal is owner-only and extends the
lease through a ref child. Normal completion writes releasing/released
children, removes only the visible guard label, confirms the plan labels are
unchanged, and writes a terminal record. Failure paths attempt the same
owner-only release; an ambiguous or expired claim remains for recovery.

Recovery is a separate operator-only command. It requires a distinct recovery
credential, an allowlisted actor, the expected claim UUID and ref OID, an
explicit reason, and the lease plus grace window to have elapsed. Recovery
uses signed exact-head compare-and-swap children and preserves all plan labels,
including `state:plan-blocked`. The normal automation path rejects the
recovery credential if it is present.

## Alternatives considered

- **The label alone.** Rejected: concurrent label additions do not establish
  durable winner identity, and a later process cannot safely decide which run
  may remove it.
- **A local process lock.** Rejected: it does not protect across hosts or
  survive a process restart.
- **Silently clearing old labels.** Rejected: a stale-looking label can still
  represent a live run; only explicit, auditable operator recovery may clear a
  claim after its lease and grace period.

## Consequences

- GitHub receives one orthogonal label and one auditable history on the
  implementation branch; no guard-only ref or branch is created.
- All issue-bearing worker jobs must be bound to a matching guard credential;
  repository-wide label provisioning remains outside an issue guard.
- A crashed process can leave visible, recoverable state. Operators must use
  the [issue guard recovery runbook](../runbooks/issue-work-guard-recovery.md)
  rather than removing the label manually.
- Plan routing remains label-authoritative, and automation never removes or
  replaces `state:plan-blocked`.
