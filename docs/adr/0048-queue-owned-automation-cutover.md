# ADR-0048: Queue-owned automation cutover

- Status: Accepted
- Date: 2026-09-09
- Tracks: Approved queue simplification plan, REL-01 through REL-14 and SIM-01 through SIM-07

## Context

The queue pipeline and older automation tools have overlapping execution and
recovery paths. Several layers retry the same operation. Duplicate state can
lose file reservations, retain worker permits, or confuse publication records
with current review authority. Compatibility branches also let tests use a
different execution path from production.

PR #3006 is merged. Its temporary host-verification bootstrap has completed
its purpose.

## Decision

The queue pipeline is the sole owner of automation. Keep six main stages:
`repo`, `planning`, `plan_review`, `implementation`, `pr_review`, and
`merge_wait`. Keep `learning` and `finished` in a separate auxiliary lane.
Both lanes have bounded completion channels and distinct worker pools.

The coordinator owns admission, routing, timers, permits, and completion.
Each submitted operation produces one result, including cancellation. Parked
work retains its durable recovery records but releases its in-memory permit.
The accepted work item owns its frozen file reservation through review and
merge. Dependency identity includes the repository.

Workers perform bounded operations. Source validation occurs under the
authoritative workspace lease. A single deadline and cancellation signal
cover lock waits, source checks, and execution. Local workspace failures do
not count as provider outages. After possible external effects, the queue
reconciles source and publication records before another attempt. Queue
GitHub operations use one transport attempt; coordinator timers schedule
retries. Manual utility defaults remain independent.

Planning advances only from a fresh current-plan read. Plan review retains
the accepted revision, fingerprint, verdict, and charged round. Publication
retries do not charge that round again. A stale plan returns to admission.
Conversation replacement uses the existing bounded error budget.

Recovered publication records do not grant current-process review authority.
After restart, the pipeline obtains fresh source, verification, and review
evidence before merge. Keep exact-head GitHub readbacks and the server merge
route required by ADR-0039. No queue stage changes native auto-merge.

Expose `PipelineConfig`, `PipelineScope`, `StageName`, and `run_pipeline`
through lazy package imports. Use one parser and configuration builder for
the full loop and its three scoped entry points. Use `--stages`,
`--merge-attempts`, and `--max-workers`. Removed options fail during parsing.
Remove standalone automation owners, aliases, and legacy persistence readers.
Keep current plan pointers, publication repair, wave checkpoints, learning
intents and claims, source ownership, reply journals, and verification records.
Keep the authenticated planning-marker identities in the current shared protocol.
Historical files and comments remain inert. Do not infer missing learning
intents from historical merges.

This decision supersedes the compatibility exceptions in ADR-0017 and the
bootstrap in ADR-0046. It supersedes the retired command and direct-provider
surfaces in ADR-0020. It also supersedes legacy reconstruction and session
interpretation in ADR-0027 and ADR-0031 where those readers conflict with the
current-record policy above. Their historical text remains unchanged.
The queue architecture in ADR-0006 and the auxiliary lane in ADR-0026 remain
in effect. The automation-to-library dependency direction is unchanged.

## Cutover

Before deployment, stop old coordinators and drain or park active work.
Preserve unresolved external effects, local commits, worktrees, and current
journals. Do not run old and new owners against the same state directory.
Restart with the retained queue entry point and inspect its recovery results.
Old command names, flags, and record formats have no conversion path.

Use separate commits for reliability repairs and deletion groups. A rollback
requires stopped coordinators and a review of any effects produced after the
cutover. Restoring old code alone does not establish that replay is safe.

## Alternatives considered

Keeping compatibility adapters would retain multiple owners and failure
paths. Splitting large files alone would leave those ownership problems.
A persistent queue database, event bus, or general recovery framework would
add another state owner. These alternatives were rejected.

## Consequences

Retained capabilities have one implementation path. Tests use the same pool,
lease, and result contracts as production. New and changed behavior tests run
natively on supported hosts. Required CI runs the full suites and coverage
gate, as specified in ADR-0047. Independent review covers the complete
migration diff before completion.

This is a breaking migration. Operators must change removed commands and
flags. Uncertain historical effects require inspection; they are not replayed
or converted automatically.
