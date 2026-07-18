# ADR-0006: Queue-based in-process automation pipeline

- Status: Accepted
- Date: 2026-07-04
- Tracks: #1809

## Context

The `hephaestus-automation-loop` runs as a subprocess-per-phase tree:
`loop_runner` (repo pool → issue pool) shells out to a *whole child CLI* per
phase (`plan`→`planner.py`, `implement`→`implementer.py`,
`drive-green`→`ci_driver.py` via `_resolve_phase_bin`), and each child spawns
its OWN `ThreadPoolExecutor`s. That multiplies threads and processes,
fragments per-item state across child processes and per-domain JSON files,
makes SIGINT lossy (`subprocess.run` returns *normally* when SIGINT hits the
process group, so a child can look "complete"), and hides the pipeline shape:
phases are processes, not inspectable functions. There is no architecture
design doc; the closest anchors are
[ADR-0001](0001-automation-library-boundary.md) and the ASCII state diagram
in `hephaestus/automation/state_labels.py:9-37`.

## Decision

Remap the loop into a single-coordinator, queue-based state machine:

1. **One coordinator event loop** (the process main thread) owns eight
   in-memory stage queues (`repo → planning → plan_review → implementation →
   pr_review → strict_review → merge_wait → finished`) and performs ONLY arg parsing +
   queue seeding, draining, validation/logging, GitHub API mutation, and
   routing. It never launches agent workflows or build/test commands.
2. **One worker pool** runs ALL agent invocations, build/test subprocesses,
   and git/network operations.
3. **A declarative per-stage failure-routing table** (single location; default
   fail target = previous queue) drives regressions between stages.
4. **Durable GitHub write precedes every queue push** — labels/PR state are the
   journal; queues are in-memory and reconstructed at startup, so an interrupt
   leaves items RESUMABLE, never FAILED.
5. **The existing CLIs become scoped seeders** over a trimmed queue set
   (`hephaestus-plan-issues` = planning→plan_review, etc.).

## Alternatives considered

- **(a) Keep subprocess-per-phase behind a queue façade.** Rejected: a façade
  over the child-process tree does not fix lossy interrupts or the fragmented,
  un-inspectable observability that motivate this change.
- **(b) An asyncio coordinator.** Rejected: the entire I/O surface is *blocking*
  subprocesses (`gh`, `claude`, git plumbing). Threads are the honest primitive
  for wrapping blocking subprocess calls; asyncio would demand
  executor-offloading everywhere for no gain.
- **(c) A persistent on-disk queue (SQLite / journal file).** Rejected: GitHub
  (labels + PR state) is already the journal. A second on-disk journal invites
  divergence between the two sources of truth; reconstruction-from-GitHub keeps
  one authority.
- **(d) One queue with typed messages.** Rejected: eight per-stage queues give
  free introspection (depth per stage), fairness (round-robin admission), and
  explicit per-stage admission points that a single typed-message queue would
  have to re-derive.

## Consequences

- **+ Resumability**: durable-write-before-push + GitHub-as-journal means
  restart = re-run; interrupts park items resumable at their stage.
- **+ Single observability domain**: one process, inspectable queue depths and
  in-flight registry, instead of a child-process tree.
- **+ Testable stages**: each stage is a small state machine of pure-ish steps,
  unit-testable against fake GitHub / fake worker pool.
- **− gh-call concentration**: all `gh` mutations run in one process, so the
  rate budget and any module-level `github_api` caches become process-global
  and must be audited/locked for thread safety.
- **− curses UI needs an adapter**: the TUI is child-process-scoped today; the
  pipeline ships plain logging first, UI adapter as follow-up.
- **− process-global model-cap registry**: `claude_invoke`'s 429 model-cap
  registry becomes shared across worker threads (a lock + log line; the cap is
  account-level so this is likely desirable, but it is a behavior change).
