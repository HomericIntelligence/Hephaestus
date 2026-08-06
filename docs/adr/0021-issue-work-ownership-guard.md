# ADR-0021: Ref-backed issue work-ownership guard

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
JSON in the per-issue ref
`refs/heads/hephaestus/issue-guards/issue-<N>`.

An automation run reads labels, refuses a live guard, creates an acquiring
record, installs it with a non-forced ref operation, and reads it back before
adding the label. It then records an active child and confirms both the
record identity and label before dispatching work. Every issue mutation uses
the target-bound guarded GitHub proxy, which re-confirms the claim immediately
before the mutation. Claims carry repository, issue, claim UUID, and run UUID
across worker boundaries.

Active claims use a four-hour lease. Renewal is owner-only and extends the
lease through a ref child. Normal completion writes releasing/released
children, removes only the visible guard label, confirms the plan labels are
unchanged, and writes a terminal record. Failure paths attempt the same
owner-only release; an ambiguous or expired claim remains for recovery.

Recovery is a separate operator-only command. It requires a distinct recovery
credential, an allowlisted actor, the expected claim UUID and ref OID, an
explicit reason, and the lease plus grace window to have elapsed. Recovery
uses non-forced compare-and-swap children and preserves all plan labels,
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

- GitHub receives one orthogonal label and one auditable ref history per issue.
- All issue-bearing worker jobs must be bound to a matching guard credential;
  repository-wide label provisioning remains outside an issue guard.
- A crashed process can leave visible, recoverable state. Operators must use
  the [issue guard recovery runbook](../runbooks/issue-work-guard-recovery.md)
  rather than removing the label manually.
- Plan routing remains label-authoritative, and automation never removes or
  replaces `state:plan-blocked`.
