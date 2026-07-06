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

## Recover

The loop is idempotent per issue: the coordinator re-seeds from GitHub labels,
PR state, and local worktrees on startup, so re-running resumes from the
last-known durable state. There is no persisted queue snapshot — the label and
PR/worktree state are the checkpoint.

```bash
hephaestus-automation-loop --pipeline --issues <N> --loops <K> --repos <REPO>
```

`--pipeline` is optional because the pipeline is the default, but keeping it in
recovery commands makes the selected path visible in logs. If the pipeline
itself is the suspected cause, use the rollback hatch:

```bash
hephaestus-automation-loop --legacy-loop --issues <N> --loops <K> --repos <REPO>
```

The shared checkout is reset between turns, so any uncommitted in-flight edit
from the crashed turn is discarded; this is by design. Issue work happens in
`build/.worktrees/issue-<N>`, which is the recoverable worktree state.

## When `state:skip` applies

`state:skip` is the only label that takes an issue out of the loop entirely. It
is operator-applied, applied when the review loop exhausts its budget without a
GO, or applied to epics before exclusion from the issue queue. A crash alone
does **not** apply `state:skip`; re-running the loop is the correct first
response to a crash. Apply `state:skip` yourself only when an issue is genuinely
stuck after repeated attempts (for a stuck-but-green PR, see the
[CI-driver stall runbook](ci-driver-stall.md)).

## See also

- [Corrupted worktree state](corrupted-worktree.md)
- [CI-driver stall (green-but-BLOCKED)](ci-driver-stall.md)
- [Claude quota exhausted (429)](claude-quota-exhausted.md)
- Stage → module → console-script mapping: [`../../AGENTS.md`](../../AGENTS.md)
