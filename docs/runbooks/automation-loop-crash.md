# Runbook: Automation Loop Crashed Mid-Issue

Use this when the `hephaestus-automation-loop` process dies partway through
processing an issue, or a phase times out, and you need to resume safely.

## Symptoms

- The loop process exited unexpectedly (force-kill, OOM, terminal closed).
- The log shows one of these crash markers (emitted from
  `hephaestus/automation/loop_runner.py`):
  - `[{repo}] issue #{issue} pipeline crashed: {exc}` — a single issue's
    pipeline raised.
  - `[{repo}] runner crashed: {exc}` — the per-repo runner raised.
  - `[{repo}] phase {phase} TIMEOUT after {N}s` — a stage exceeded its timeout.
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

The loop is idempotent per issue: it re-discovers open issues with a fresh
`gh issue list` every loop iteration and re-reads each issue's `state:*` label,
so re-running resumes from the last-known label state. There is no per-issue
crash checkpoint — the label *is* the checkpoint.

```bash
hephaestus-automation-loop --issues <N> --loops <K> --repos <REPO>
```

The shared checkout is reset between turns, so any uncommitted in-flight edit
from the crashed turn is discarded; this is by design — the loop runner owns
worktree isolation and resets the shared checkout each turn.

## When `state:skip` applies

`state:skip` is the only label that takes an issue out of the loop entirely. It
is **operator-applied**, or **auto-applied** when a PR is genuinely stuck after
the merge budget (`--max-merge-attempts`) is exhausted. A crash alone does
**not** apply `state:skip`;
re-running the loop is the correct first response to a crash. Apply
`state:skip` yourself only when an issue is genuinely stuck after repeated
attempts (for a stuck-but-green PR, see the
[CI-driver stall runbook](ci-driver-stall.md)).

## See also

- [Corrupted worktree state](corrupted-worktree.md)
- [CI-driver stall (green-but-BLOCKED)](ci-driver-stall.md)
- [Claude quota exhausted (429)](claude-quota-exhausted.md)
- Stage → module → console-script mapping: [`../../AGENTS.md`](../../AGENTS.md)
