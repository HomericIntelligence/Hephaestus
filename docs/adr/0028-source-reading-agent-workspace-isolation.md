# ADR-0028: Deterministic source-reading agent workspace isolation

- Status: Accepted
- Date: 2026-08-13
- Tracks: #2764

## Context

Planning and plan review could run from the reusable repository checkout before
an implementation worktree existed. Ambient untracked files, local instruction
files, and secrets could therefore affect analysis, and the provider's source
could differ from the captured remote revision. Review retries also allocated
generation-suffixed worktree paths, multiplying branches and recovery state.

## Decision

Treat the reusable checkout as a Git control plane, never an agent working
directory. Define provider-neutral `source`, `session-only`, and `external`
workspace bindings. A source binding includes the repository-qualified owner,
item, lane, canonical path, exact revision, receipt generation, and detached
state. Validate the binding immediately before execution and hold its
cross-process lane lock for the complete provider invocation.

Own exactly two deterministic source worktrees per item:
`auto-<#>-impl` for writer-side reads and writes, and `auto-<#>-review` for
read-only reviewer work. Reuse each path across phases, retries, and review
rounds. Rebind a clean lane in place when its captured revision changes. The
review lane is detached and never creates or publishes a branch. Repository
identity remains part of the durable ownership key even though the visible
path stays concise.

Use `auto-<#>-guard` only as a stable, compare-and-swap control-plane ref. It
does not own a worktree. Preserve dirty state and refuse cleanup until every
durable source-reading obligation for the lane is terminal. Bind resumable
provider sessions to the same repository, lane, path, revision, and generation
with a strict session receipt.

## Alternatives considered

- Sanitize and continue using the reusable checkout. Rejected because it does
  not establish immutable revision binding or cross-task isolation.
- Create a worktree per call, retry, or review round. Rejected because it
  causes unbounded path and branch growth and complicates recovery.
- Give reviewers a local or remote review branch. Rejected because review is
  read-only and branch publication would add an unnecessary mutation surface.
- Use only an in-memory ownership map. Rejected because it cannot coordinate
  processes or recover safely after restart.

## Consequences

Source-reading jobs fail closed when their receipt, path, revision, cleanliness,
or detached state changes. Retries have stable filesystem identity and at most
two item worktrees. A lane rebind invalidates resumable sessions from an older
generation. Cleanup may retain more state after failures, but it never destroys
the only recoverable writer state or races a source reader.
