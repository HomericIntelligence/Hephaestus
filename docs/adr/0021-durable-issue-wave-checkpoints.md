# ADR-0021: Durable merge-checkpointed issue waves

- Status: Accepted
- Date: 2026-08-06
- Tracks: #2453

## Context

The queue pipeline intentionally reconstructs work from GitHub and does not
persist its in-memory queues. That is insufficient for the requested staged
rollout: a later process must know the exact issues selected by the previous
wave and must prove that their loop-owned pull requests were merged normally
before selecting more work. Per-issue arming records are best-effort and do
not provide a repository-wide compare-and-swap boundary.

## Decision

Each repository may have one checkpoint at
`build/.issue_implementer/issue-wave-checkpoint.json`. It records the ordered
wave selections, the synchronized-main revision used to select them, terminal
outcomes, and immutable merge receipts containing the issue, PR, reviewed head,
and merge commit. A sibling lock serializes readers and writers; checkpoint
updates use an atomic owner-only write and a generation compare-and-swap.

Wave admission is performed after synchronized checkout and before label setup.
The only selectors are 1, 2, 4, 8, and all eligible issues. A later wave needs
fresh issue/PR facts and read-only Git ancestry proof against the current
`main`. Failed, externally changed, unmerged, or receipt-less work blocks
advancement. After the final all-eligible wave is verified, the checkpoint is
marked completed and later unbounded invocations are audit-only. Explicit
`--issues` and `--prs` remain identifier-based recovery scopes.

The store rejects symlinked or non-regular state paths, repository mismatches,
malformed identifiers, broad permissions, stale leases, and unsupported phase
transitions. Git ancestry remains in the existing worker boundary; the store
never executes GitHub or shell commands.

## Alternatives considered

- Persisting queue snapshots: rejected because GitHub remains the journal and
  queue contents contain transient agent/worktree state.
- Reusing `ArmingStateStore`: rejected because it is per-issue and explicitly
  best-effort, while wave admission must fail closed across processes.
- Treating `--loops` as a wave size: rejected because it would make a process
  restart or a changed loop count bypass the durable rollout sequence.

## Consequences

The rollout gains a small local recovery artifact and requires normal merge
receipts plus a synchronized checkout before each advancement. State backup and
restore must include the checkpoint directory. Removing or restoring a
checkpoint is an operator recovery action and can change which wave is admitted;
there is no implicit reset or recycle path.
