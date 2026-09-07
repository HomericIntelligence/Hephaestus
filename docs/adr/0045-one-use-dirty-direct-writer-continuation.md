# ADR-0045: One-use dirty direct-writer continuation

- Status: Draft
- Date: 2026-09-07
- Tracks: #3008
- Supersedes: ADR-0028 (dirty direct-writer admission only)

## Context

An interrupted direct implementation can leave an owned worktree with pending
changes and no open PR. A restart must preserve that work. A new reservation or
a reset can separate the pending content from its original branch and receipt.

## Decision

The host can claim one continuation of an owned dirty direct writer. It keeps
the original branch, reservation base, and deterministic implementation lane.
A version 2 receipt binds the claim to the repository, issue, workspace,
generation, HEAD, approved plan revision, plan and review content, allowed
paths, and a bounded content snapshot. Version 1 receipts retain their format
and clean-workspace rules.

The host reads the current actor-owned plan and review with complete PR
absence evidence. The issue must be open with exclusive `state:plan-go` and
without skip or blocked labels. The worker independently checks the frozen
job inputs. Under the source lane lock, the manager compares those inputs
with the armed claim, checks the physical workspace and content, and writes
the consumed claim before it permits the provider turn.

The library accepts a dirty workspace only with an active process-local
permit for that exact binding. Serialized claim data does not grant access.
The manager revokes the permit when the lease ends, including copies retained
in another context. The source manager does not read GitHub. A plan change
between claim and launch is detectable when the frozen job inputs change;
this contract does not promise immediate detection of a remote change during
that interval.

The continuation has one provider turn without retry. Native Codex retains
workspace and approved-plan scope checks. An explicitly selected adapter must
satisfy its admission contract and cannot fall back to native execution. See
[ADR-0043](0043-optional-codex-adapter-until-production-ready.md). The prompt
does not authorize commit, push, reservation, or GitHub writes.

Publication holds the repository and Git locks, then the source lane lock.
It checks fresh complete PR absence and current plan evidence before staging
and before push. It checks exact paths and content, creates a signed DCO
commit for the captured tree and parent, then uses the original remote SHA
as the push lease. A failed commit helper cannot advance the source receipt
from an uncertain physical HEAD. The host preserves that HEAD and the prior
consumed receipt for investigation.

PR creation is strict. It cannot adopt a concurrent PR. An uncertain create
result causes one fresh read and terminal preservation, without another
create attempt. The host clears the continuation obligation only after a
confirmed push, successful strict creation, and exact PR head readback.
Failures and no-change results retain the worktree, receipt, and reservation.

## Alternatives considered

- Reset the dirty worktree. Rejected because this discards pending work.
- Create another reservation. Rejected because the new branch does not identify
  the original pending content and ownership receipt.
- Retry a failed provider turn. Rejected because the first turn can change
  content before it fails. A retry would reuse consumed authority.
- Accept serialized claim data as permission. Rejected because a copied claim
  cannot prove that the source lane lease is active.

## Consequences

A restart can continue eligible dirty direct work once. It cannot silently
reset work, repeat a consumed turn, expand file scope, or reuse a PR that
appears during publication. Manual reconciliation is required when the
physical workspace and the durable receipt no longer agree.

Planning keeps the detached default-branch assignment from ADR-0040.
The two-lane workspace limit and other ADR-0028 contracts remain in effect.
Existing event schemas remain unchanged.
