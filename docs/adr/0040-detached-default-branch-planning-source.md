# ADR-0040: Detached default-branch source for planning

- Status: Accepted
- Date: 2026-09-06
- Tracks: #2998
- Supersedes: ADR-0028 (planning lane assignment only)

## Context

Planning can start after an earlier implementation attempt. The earlier
attempt can leave `auto-<#>-impl` dirty and preserved. If planning reuses that
lane, source preparation stops before the advise job. A forced replan must keep
the writer workspace unchanged and use the captured default-branch source.

## Decision

Use the detached `auto-<#>-review` lane for requirements recovery, advice, plan
creation, plan review, and plan amendment. Bind each job to
`_synced_default_branch_sha`, with `_direct_scope_base_sha` as the fallback when
the synchronized revision is absent. Do not use implementation, cleanup, or
pull-request revisions for planning source reads.

Keep the two-lane limit from ADR-0028. The `auto-<#>-impl` lane remains the
source for implementation, remediation, and writer recovery. Its dirty-state
preservation and fail-closed behavior remain unchanged. Do not add a planning
worktree or clean an implementation lane as part of planning.

## Alternatives considered

- Reuse the implementation lane for every stage. Rejected because a dirty
  preserved writer workspace must not block planning or be changed by it.
- Add a third planning lane. Rejected because the detached review lane already
  provides an isolated source and the two-lane limit is sufficient.
- Reset or remove the dirty implementation lane before planning. Rejected
  because this can discard recoverable writer work.
- Use the newest available implementation or pull-request revision. Rejected
  because forced replanning must use the captured default-branch source.

## Consequences

Forced replanning can reach advice and plan review when the legacy writer lane
is dirty. Planning reads are detached, clean, and bound to one captured
revision. Existing clean review workspaces can be reused at their deterministic
path. Implementation recovery keeps its existing dirty-workspace failure path.
The review lane is shared by planning and review jobs, so its preparation
remains bounded where the planning stage requires a deadline.
