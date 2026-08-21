# Runbook: Automation Loop Crashed Mid-Issue

Use this when the `hephaestus-automation-loop` process dies partway through
processing an issue, or a phase times out, and you need to resume safely.

## Symptoms

- The loop process exited unexpectedly (force-kill, OOM, terminal closed).
- The log shows one of these crash markers (emitted from
  the default pipeline coordinator):
  - `Path: pipeline` — confirms the queue-based coordinator path was selected.
  - `pipeline run failed` — the coordinator hit a fatal top-level exception.
  - `on_job_done poisoned item ...` or `poisoned item ...` — a single item
    raised inside stage completion or stepping and was routed to failed.
  - `RESUMABLE at <stage>` / `resumable at <stage>` in `=== Pipeline summary ===`
    — the run was interrupted and left the item safe to resume.
  - `error="timeout"` or phase-specific timeout text — under the pipeline,
    `--phase-timeout` bounds agent jobs, not whole phase subprocesses.
- An issue is left in an intermediate `state:*` label (see the
  [runbooks index](index.md) state-label table).

## Diagnose

1. Read the current label state of the affected issue — phases are
   driven entirely by the `state:*` label, so the label tells you where the
   pipeline was:

   ```bash
   gh issue view <N> --json labels --jq '.labels[].name'
   ```

2. Check whether an in-progress worktree was left on disk for that issue:

   ```bash
   git -C <repo> worktree list
   ls -la <repo>/build/.worktrees/issue-<N>
   ```

   A leftover worktree is expected after a force-kill — the loop keeps
   worktrees inside the repo precisely so an interrupted run survives on disk
   for the next invocation to resume or surface. If the worktree is dirty or
   suspect, recover it with the
   [corrupted-worktree runbook](corrupted-worktree.md) before re-running.

## Pipeline recovery semantics

For queue-pipeline recovery, distinguish interrupts from fatal coordinator
failures:

- A first `SIGINT`, `SIGTERM`, or `SIGHUP` starts graceful shutdown. The
  coordinator stops admitting new work and drains in-flight jobs for the
  configured grace window (`30s` by default). An interrupted run exits with
  exit code 130.
- A second signal, or expiry of the grace window, forces immediate worker-pool
  teardown and synthesizes interrupted results for any remaining in-flight
  jobs.
- Interrupted, queued, and timer-parked items are reported as `RESUMABLE at
  <stage>` in `=== Pipeline summary ===`; they are never FAILED by the
  interrupt path and do not run through normal stage success/failure routing.
- Restart reconstructs the in-memory queues from the durable journal: GitHub
  labels, PR state, local worktrees, and `build/.automation-state/learning-intent-*.json`.
  There is no persisted queue snapshot.
  Re-run the same scoped command to let seeding classify the issue back into
  the correct entry queue.
- To inspect journal reconstruction without launching work, run the scoped
  pipeline command with `--dry-run --loops 1 -v`.

Inspect learning records before rollback. `pending` can run again. A crash-left
`claimed` record becomes terminal `failed` with `outcome_unknown` and is not
submitted twice. `succeeded` and `failed` are terminal. Do not start an older
Hephaestus executable until every learning record is terminal or has been
parked and handled by the new version. Use `--no-learn` only to bypass new
learning; it does not make an older executable understand the new journal.

## Recover

The loop is idempotent per issue: the coordinator re-seeds from GitHub labels,
PR state, and local worktrees on startup, so re-running resumes from the
last-known durable state. There is no persisted queue snapshot — the label and
PR/worktree state are the checkpoint.

```bash
hephaestus-automation-loop --issues <N> --loops <K> --repos <REPO>
```

The queue pipeline is the only automation-loop implementation. There is no
separate rollback selector; use the scoped command above to reconstruct queue
state from GitHub labels, PR state, and local worktrees.

The shared checkout is reset between turns, so any uncommitted in-flight edit
from the crashed turn is discarded; this is by design. Issue work happens in
`build/.worktrees/issue-<N>`, which is the recoverable worktree state.

## Recovering staged issue waves

For a staged rollout, the repository-local checkpoint is
`build/.issue_implementer/issue-wave-checkpoint.json`. Run the selectors in
order; the next invocation is blocked until the prior wave's exact issues have
passing terminal outcomes. Implementation outcomes require loop-owned normal
merge receipts; independently reviewed tracker/obsolete outcomes instead require
their exact durable `state:skip` label set. A checkpointed non-code intent
automatically resumes explanation/label repair or terminal recording after a
crash only while its reviewed title, body, and repository-revision evidence
still matches. If the issue changed, the loop re-enters normal semantic review;
an authenticated Athena-finalized body advances as completed planning instead
of inheriting the stale skip decision. The checkpoint retains a retired intent
until any exact loop-owned `state:skip` is removed and freshly confirmed, so a
second crash resumes cleanup. An unrelated operator-applied `state:skip` has no
matching retired intent and remains authoritative:

```bash
hephaestus-automation-loop --repos <REPO> --issue-limit 1
hephaestus-automation-loop --repos <REPO> --issue-limit 2
hephaestus-automation-loop --repos <REPO> --issue-limit 4
hephaestus-automation-loop --repos <REPO> --issue-limit 8
hephaestus-automation-loop --repos <REPO>  # final all-eligible wave or audit
```

Re-running the same selector resumes the stored identifiers. `--loops` cannot
reseed a checkpointed source. A failed, unmerged, closed-without-merge,
blocked, or externally changed item must be recovered before the next selector
is accepted. Explicit `--issues` and `--prs` remain available for targeted
recovery and do not convert their identifiers into counts. Once the final wave
is verified, unbounded runs are audit-only; a bounded selector is rejected.

Do not hand-edit the checkpoint. Use the existing state backup/restore process
when the file is malformed or the repository checkout was restored.

## When `state:skip` applies

`state:skip` is the only label that takes an issue out of the loop entirely. It
is operator-applied or applied after independent planner/reviewer agreement that
an issue is a non-code tracker or obsolete request. A crash alone does **not**
apply `state:skip`; re-running the loop is the correct first response to a crash.
Apply `state:skip` yourself only when an issue is genuinely
stuck after repeated attempts (for a stuck-but-green PR, see the
[drive-green stall runbook](ci-driver-stall.md)).

## See also

- [Corrupted worktree state](corrupted-worktree.md)
- [Drive-green stall](ci-driver-stall.md)
- [Claude quota exhausted (429)](claude-quota-exhausted.md)
- Stage → module → console-script mapping: [`../../AGENTS.md`](../../AGENTS.md)
